# from dot is the same as from website since it imports from the current package
from . import db
from flask_login import UserMixin
from sqlalchemy.sql import func
import json as _json

COMPANY_SIZES = [
    "1–10",
    "11–50",
    "51–200",
    "201–500",
    "501–1,000",
    "1,000+",
]

INDUSTRIES = [
    "Technology",
    "Healthcare",
    "Finance & Banking",
    "Marketing & Advertising",
    "Sales",
    "Design & Creative",
    "Operations & Logistics",
    "Education",
    "Legal",
    "Real Estate",
    "Manufacturing",
    "Retail & E-commerce",
    "Media & Entertainment",
    "Non-profit",
    "Government",
    "Other",
]

JOB_CATEGORIES = [
    "Software",
    "Healthcare",
    "Finance",
    "Marketing",
    "Sales",
    "Design",
    "Operations",
    "Other",
]

JOB_LOCATIONS = [
    "Remote",
    "Remote - US Only",
    "Hybrid",
    "New York, NY",
    "San Francisco, CA",
    "Los Angeles, CA",
    "Chicago, IL",
    "Austin, TX",
    "Seattle, WA",
    "Boston, MA",
    "Denver, CO",
    "Atlanta, GA",
    "Miami, FL",
    "Washington, DC",
    "Toronto, Canada",
    "London, UK",
    "Other",
]

EARN_DOMAINS = [
    "Software",
    "Healthcare",
    "Legal",
    "Finance",
    "Marketing",
    "Sales",
    "Design",
    "Operations",
    "Education",
    "Science",
    "Other",
]

COUNTRIES = [
    "United States", "Canada", "United Kingdom", "Ireland", "Australia",
    "New Zealand", "Germany", "France", "Spain", "Italy", "Netherlands",
    "Portugal", "Poland", "Sweden", "Norway", "Switzerland", "India",
    "Pakistan", "Philippines", "Singapore", "Mexico", "Brazil", "Argentina",
    "South Africa", "Nigeria", "Kenya", "United Arab Emirates", "Other",
]

# Consumer/free webmail providers - employers must sign up with a company email instead.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "icloud.com", "protonmail.com", "proton.me", "mail.com",
    "gmx.com", "yandex.com", "zoho.com", "msn.com", "me.com",
}


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True)
    password = db.Column(db.String(255))  # werkzeug's scrypt hashes run ~162 chars, longer than the old 150 cap
    first_name = db.Column(db.String(150))
    role = db.Column(db.String(20), nullable=False, default="candidate")  # "candidate" | "employer"
    email_verified = db.Column(db.Boolean, nullable=False, default=True)
    notifications_enabled = db.Column(db.Boolean, nullable=False, default=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    supabase_uid = db.Column(db.String(200), unique=True)
    data_buyer_id = db.Column(db.Integer, db.ForeignKey("data_buyer.id"))
    is_data_buyer_admin = db.Column(db.Boolean, nullable=False, default=False)


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    # Domain of the founding employer's work email (e.g. "acme.com") - lets later
    # signups from the same company email land in this same hub instead of each
    # getting their own Company. Null for companies created before this existed,
    # or whose founder signed up via a free-webmail-flagged edge case.
    email_domain = db.Column(db.String(150))
    website = db.Column(db.String(300))
    description = db.Column(db.Text)
    industry = db.Column(db.String(100))
    size = db.Column(db.String(50))
    location = db.Column(db.String(200))
    logo_filename = db.Column(db.String(200))
    logo_version = db.Column(db.Integer, default=0)
    blind_mode_enabled = db.Column(db.Boolean, nullable=False, default=False)
    terms_accepted_at = db.Column(db.DateTime(timezone=True))
    terms_accepted_ip = db.Column(db.String(45))
    terms_version = db.Column(db.String(10))
    # Typed full legal name of the authorised rep who signed the Employer
    # Platform Agreement - an e-signature alongside the checkbox, so the
    # placement-fee obligation reads as an actually-signed contract.
    terms_signature_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())


class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(200))
    employment_type = db.Column(db.String(50))  # e.g. "Full-time", "Contract"
    category = db.Column(db.String(50), nullable=False, default="Other")  # see JOB_CATEGORIES
    status = db.Column(db.String(20), nullable=False, default="open")  # "open" | "closed"
    interview_length = db.Column(db.String(20), nullable=False, default="standard")  # short | standard | thorough
    interview_focus = db.Column(db.Text)  # optional employer instructions on what the AI should cover
    interview_questions = db.Column(db.Text)  # JSON list of employer-curated questions the AI should make sure to ask
    salary_usd = db.Column(db.Integer)
    salary_max_usd = db.Column(db.Integer)
    pay_type = db.Column(db.String(20), default='yearly')
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    company = db.relationship("Company", backref="jobs")

    _SECTION_LABELS = [
        ('about', 'About the Role'),
        ('requirements', 'Requirements'),
        ('preferred', 'Preferred Qualifications'),
        ('what_you_do', "What You'll Do"),
        ('contract_terms', 'Contract & Payment Terms'),
    ]

    @property
    def description_sections(self):
        """Return structured dict if description is JSON, else None."""
        try:
            data = _json.loads(self.description)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
        return None

    @property
    def description_text(self):
        """Return plain-text description suitable for AI calls."""
        sections = self.description_sections
        if sections:
            parts = []
            for key, label in self._SECTION_LABELS:
                val = (sections.get(key) or '').strip()
                if val:
                    parts.append(f"{label}:\n{val}")
            return '\n\n'.join(parts) if parts else self.description
        return self.description

    def to_snapshot_dict(self):
        """Every re-creatable field, for stashing in DeletedJob.snapshot."""
        return {
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "employment_type": self.employment_type,
            "category": self.category,
            "status": self.status,
            "interview_length": self.interview_length,
            "interview_focus": self.interview_focus,
            "interview_questions": self.interview_questions,
            "salary_usd": self.salary_usd,
            "salary_max_usd": self.salary_max_usd,
            "pay_type": self.pay_type,
        }


class DeletedJob(db.Model):
    """Snapshot of a Job taken right before it's deleted, so an accidental
    delete can be undone. company_id is deliberately not a ForeignKey - when
    a whole Company is deleted (admin.delete_company), its jobs are snapshotted
    here first but the Company row itself is gone by the time this survives it,
    so a hard FK would leave a dangling reference. Restoring only recreates the
    job listing itself; applications/messages/videos already cascade-deleted
    alongside it are not recoverable."""
    id = db.Column(db.Integer, primary_key=True)
    original_job_id = db.Column(db.Integer, nullable=False)
    company_id = db.Column(db.Integer, nullable=False)
    company_name = db.Column(db.String(200), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    snapshot = db.Column(db.Text, nullable=False)  # JSON of Job.to_snapshot_dict()
    applicant_count = db.Column(db.Integer, nullable=False, default=0)
    deleted_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    deleted_source = db.Column(db.String(20), nullable=False)  # "employer" | "admin"
    restored_job_id = db.Column(db.Integer)  # set once restored, so it can't be restored twice
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    deleted_by = db.relationship("User")


class CandidateProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    headline = db.Column(db.String(200))
    resume_text = db.Column(db.Text)
    linkedin_url = db.Column(db.String(300))
    target_roles = db.Column(db.String(300))
    # Talent pool fields
    open_to_work = db.Column(db.Boolean, nullable=False, default=False)
    skills = db.Column(db.Text)           # comma-separated
    work_preference = db.Column(db.String(50))   # Remote / Hybrid / On-site / Any
    availability = db.Column(db.String(50))      # Immediately / 2 weeks / 1 month / 3 months
    years_experience = db.Column(db.String(20))  # 0-1 / 1-3 / 3-5 / 5-10 / 10+
    location = db.Column(db.String(200))         # city / country they're based in

    user = db.relationship("User", backref=db.backref("candidate_profile", uselist=False))


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False)
    candidate_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    resume_text = db.Column(db.Text, nullable=False)
    work_authorization = db.Column(db.Text)  # JSON: DOB, residence, attestations, signature
    scheduled_meeting = db.Column(db.Text)  # JSON: type, datetime, location_or_url, notes
    match_score = db.Column(db.Integer)
    match_explanation = db.Column(db.Text)
    interview_transcript = db.Column(db.Text)
    interview_audio_log = db.Column(db.Text)  # JSON list of {role, text, audio_filename}
    interview_verdict = db.Column(db.String(20))  # "Strong Yes" | "Yes" | "No" | "Strong No"
    status = db.Column(db.String(20), nullable=False, default="applied")  # applied | interviewing | rejected | hired
    candidate_confirmed_hire = db.Column(db.Boolean, nullable=False, default=False)  # candidate must confirm before the fee invoice fires
    hire_confirmed_at = db.Column(db.DateTime(timezone=True))
    # Actual negotiated compensation at hire time, entered by the employer -
    # the placement fee is calculated from this, not the job posting's
    # advertised salary, which the employer has no incentive to keep
    # accurate once someone's actually hired.
    hire_salary_usd = db.Column(db.Integer)
    hire_pay_type = db.Column(db.String(20))  # yearly | hourly | monthly | stipend | contract
    hire_salary_disputed = db.Column(db.Boolean, nullable=False, default=False)  # candidate says the entered figure is wrong
    hire_evidence_filename = db.Column(db.String(200))  # optional offer-letter/screenshot upload
    fee_paid = db.Column(db.Boolean, nullable=False, default=False)
    stripe_payment_link = db.Column(db.String(500))  # Stripe Payment Link URL sent to employer on hire
    # Hourly hires bill monthly for as long as the engagement actually runs,
    # instead of one lump sum assuming a full 2,080-hour year up front -
    # stripe_payment_link above is a subscription-mode link in this case.
    hire_billing_subscription_id = db.Column(db.String(100))
    hire_engagement_ended_at = db.Column(db.DateTime(timezone=True))  # set when the employer stops billing
    employer_notes = db.Column(db.Text)  # private notes visible only to employer
    withdrawn = db.Column(db.Boolean, nullable=False, default=False)
    candidate_feedback = db.Column(db.Text)  # employer-approved AI feedback sent to candidate on rejection
    skills_roadmap = db.Column(db.Text)  # JSON: AI-generated skill gap action plan (generated when score < 65)
    interview_saved_session = db.Column(db.Text)  # JSON: partial history saved for later resumption
    ai_likelihood = db.Column(db.Text)  # JSON: {score, label, reasons} - AI-generation probability for candidate answers
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    job = db.relationship("Job", backref="applications")
    candidate = db.relationship("User", backref="applications")


class SavedJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())
    __table_args__ = (db.UniqueConstraint("user_id", "job_id"),)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(300), nullable=False)
    link = db.Column(db.String(300))
    read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())


class CompanyMember(db.Model):
    """Additional employer users who can manage a company's jobs and applicants."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    joined_at = db.Column(db.DateTime(timezone=True), default=func.now())

    company = db.relationship("Company", backref="members")
    user = db.relationship("User", backref="company_memberships")

    __table_args__ = (db.UniqueConstraint("company_id", "user_id"),)


class EarnTask(db.Model):
    """A task posted for professionals to complete and earn money."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    task_type = db.Column(db.String(30), nullable=False, default='ranking')  # ranking | rating | factcheck | writing
    domain = db.Column(db.String(100))  # e.g. "Legal", "Software", "Healthcare"
    pay_cents = db.Column(db.Integer, nullable=False, default=500)  # payment in cents
    estimated_minutes = db.Column(db.Integer, default=5)
    status = db.Column(db.String(20), nullable=False, default='open')  # open | closed
    required_domains = db.Column(db.Text)   # JSON list; None means open to all domains
    min_responses = db.Column(db.Integer, nullable=False, default=3)  # workers needed before consensus
    consensus_answer = db.Column(db.Text)   # JSON {answer, total, distribution} - set once min_responses hit
    # For ranking tasks: two AI responses to compare
    option_a = db.Column(db.Text)
    option_b = db.Column(db.Text)
    # For rating/factcheck tasks: single AI response
    content = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    submissions = db.relationship('TaskSubmission', backref='task', lazy=True)


class TaskSubmission(db.Model):
    """A professional's completed response to an EarnTask."""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('earn_task.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    response = db.Column(db.Text)        # chosen option / rating / written response
    explanation = db.Column(db.Text)     # optional explanation
    agreement_score = db.Column(db.Float)  # 0–1; set when consensus is computed for this task
    earnings_cents = db.Column(db.Integer, nullable=False, default=0)
    paid_out = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    user = db.relationship('User', backref='task_submissions')


class ContractorProfile(db.Model):
    """Tracks a user's expertise domains and earn stats for the AI training contractor network."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    domains = db.Column(db.Text, nullable=False)         # JSON list of EARN_DOMAINS values
    quality_score = db.Column(db.Float, nullable=False, default=1.0)   # 0–1; updated by consensus
    tasks_completed = db.Column(db.Integer, nullable=False, default=0)
    stripe_account_id = db.Column(db.String(100))        # Stripe Connect Express account ID
    payout_enabled = db.Column(db.Boolean, nullable=False, default=False)  # True once Stripe onboarding complete
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    user = db.relationship("User", backref=db.backref("contractor_profile", uselist=False))


class MeetingProposal(db.Model):
    """Employer proposes 2-3 time slots; candidate picks one and the room is created."""
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    employer_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    slots = db.Column(db.Text, nullable=False)        # JSON list of {date, time} dicts
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending | accepted | expired
    accepted_slot = db.Column(db.Text)               # JSON {date, time} chosen by candidate
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    application = db.relationship("Application", backref=db.backref("meeting_proposals", lazy=True))
    employer = db.relationship("User", foreign_keys=[employer_user_id])


PLAN_LIMITS = {
    'free':    {'jobs': 3,    'interviews_per_month': 20,  'talent_search': False},
    'starter': {'jobs': 10,   'interviews_per_month': 150, 'talent_search': True},
    'pro':     {'jobs': 9999, 'interviews_per_month': 9999, 'talent_search': True},
}


class Message(db.Model):
    """In-app message between employer and candidate on a specific application."""
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    sender_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    sender = db.relationship("User", foreign_keys=[sender_user_id])
    application = db.relationship("Application", backref=db.backref("messages", lazy=True, order_by="Message.created_at"))


class EmployerSubscription(db.Model):
    """Stripe subscription plan for an employer company."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False, unique=True)
    plan = db.Column(db.String(20), nullable=False, default='free')   # free | starter | pro
    stripe_customer_id = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default='active')  # active | canceled | past_due
    current_period_end = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    company = db.relationship("Company", backref=db.backref("subscription", uselist=False))


class MeetingRoom(db.Model):
    """An in-app video meeting room created via Daily.co for a specific application."""
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    employer_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    room_name = db.Column(db.String(200), nullable=False, unique=True)
    room_url = db.Column(db.String(500), nullable=False)
    scheduled_date = db.Column(db.String(20))   # YYYY-MM-DD
    scheduled_time = db.Column(db.String(10))   # HH:MM
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    application = db.relationship("Application", backref=db.backref("meeting_rooms", lazy=True))
    employer = db.relationship("User", foreign_keys=[employer_user_id])


class CompanyInvite(db.Model):
    """Pending invitation for someone to join a company as a team member."""
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("company.id"), nullable=False)
    invited_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    token = db.Column(db.String(100), nullable=False, unique=True)
    used = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    company = db.relationship("Company", backref="invites")


class DataBuyer(db.Model):
    """An outside company subscribed to download the Earn marketplace's
    graded task data (rankings, ratings, factchecks, writing) as training data."""
    __tablename__ = "data_buyer"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    contact_email = db.Column(db.String(150), nullable=False)
    stripe_customer_id = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    subscription_status = db.Column(db.String(20), nullable=False, default="inactive")  # inactive | active | canceled
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    members = db.relationship("User", backref="data_buyer", foreign_keys="User.data_buyer_id")


class Feedback(db.Model):
    """A rating + comment left by a candidate or employer about their
    experience, collected at key moments (interview completion, hire
    resolution) rather than through a generic always-on form."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # candidate | employer
    context = db.Column(db.String(30), nullable=False)  # interview_completed | application_resolved | hire_closed
    rating = db.Column(db.Integer, nullable=False)  # 1-5
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    user = db.relationship("User")
    application = db.relationship("Application", backref="feedback_entries")

    __table_args__ = (db.UniqueConstraint("user_id", "application_id", "context"),)


class FeedbackInsight(db.Model):
    """Cached AI-generated summary + actionable recommendations from analyzing
    Feedback rows - regenerated on demand by an admin, not on every page view,
    since it's a real OpenAI call and feedback doesn't change that fast."""
    id = db.Column(db.Integer, primary_key=True)
    summary = db.Column(db.Text, nullable=False)
    recommendations = db.Column(db.Text)  # JSON list of {title, detail}
    feedback_count = db.Column(db.Integer, nullable=False, default=0)
    generated_at = db.Column(db.DateTime(timezone=True), default=func.now())
