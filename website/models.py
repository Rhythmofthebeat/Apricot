# from dot is the same as from website since it imports from the current package
from . import db
from flask_login import UserMixin
from sqlalchemy.sql import func

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
    email_verified = db.Column(db.Boolean, nullable=False, default=True)  # employers start False


class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    website = db.Column(db.String(300))
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
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    company = db.relationship("Company", backref="jobs")


class CandidateProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    headline = db.Column(db.String(200))
    resume_text = db.Column(db.Text)
    linkedin_url = db.Column(db.String(300))
    target_roles = db.Column(db.String(300))  # optional free text: roles/industries the candidate wants next


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
    created_at = db.Column(db.DateTime(timezone=True), default=func.now())

    job = db.relationship("Job", backref="applications")
    candidate = db.relationship("User", backref="applications")
