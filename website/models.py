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
    "Remote — US Only",
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

COUNTRIES = [
    "United States", "Canada", "United Kingdom", "Ireland", "Australia",
    "New Zealand", "Germany", "France", "Spain", "Italy", "Netherlands",
    "Portugal", "Poland", "Sweden", "Norway", "Switzerland", "India",
    "Pakistan", "Philippines", "Singapore", "Mexico", "Brazil", "Argentina",
    "South Africa", "Nigeria", "Kenya", "United Arab Emirates", "Other",
]

# Consumer/free webmail providers — employers must sign up with a company email instead.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "icloud.com", "protonmail.com", "proton.me", "mail.com",
    "gmx.com", "yandex.com", "zoho.com", "msn.com", "me.com",
}


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True)
    password = db.Column(db.String(150))
    first_name = db.Column(db.String(150))
    role = db.Column(db.String(20), nullable=False, default="candidate")  # "candidate" | "employer"
    email_verified = db.Column(db.Boolean, nullable=False, default=True)
    notifications_enabled = db.Column(db.Boolean, nullable=False, default=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    supabase_uid = db.Column(db.String(200), unique=True)


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(300))
    description = db.Column(db.Text)
    industry = db.Column(db.String(100))
    size = db.Column(db.String(50))
    location = db.Column(db.String(200))
    logo_filename = db.Column(db.String(200))
    logo_version = db.Column(db.Integer, default=0)
    blind_mode_enabled = db.Column(db.Boolean, nullable=False, default=False)
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
    fee_paid = db.Column(db.Boolean, nullable=False, default=False)
    stripe_payment_link = db.Column(db.String(500))  # Stripe Payment Link URL sent to employer on hire
    employer_notes = db.Column(db.Text)  # private notes visible only to employer
    withdrawn = db.Column(db.Boolean, nullable=False, default=False)
    candidate_feedback = db.Column(db.Text)  # employer-approved AI feedback sent to candidate on rejection
    skills_roadmap = db.Column(db.Text)  # JSON: AI-generated skill gap action plan (generated when score < 65)
    interview_saved_session = db.Column(db.Text)  # JSON: partial history saved for later resumption
    ai_likelihood = db.Column(db.Text)  # JSON: {score, label, reasons} — AI-generation probability for candidate answers
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
    earnings_cents = db.Column(db.Integer, nullable=False, default=0)
    paid_out = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    user = db.relationship('User', backref='task_submissions')


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
