import os
from os import path
from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import text
from dotenv import load_dotenv
from config import Config

# override=False (the dotenv default) so an env var already set in the process
# - e.g. a test/admin script deliberately pointing DATABASE_URL at a throwaway
# database before importing this package - always wins over .env. With
# override=True this silently clobbered that kind of safety override and once
# sent a script's db.drop_all()/create_all() at the real production database.
load_dotenv(override=False)

db = SQLAlchemy()
mail = Mail()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
DB_NAME = "database.db"


def _run_migrations(db):
    """Add columns that were introduced after the initial schema.
    Each ALTER TABLE is wrapped in try/except - safe to run on every
    startup, no-op when the column already exists."""
    new_columns = [
        # company - extended profile fields
        "ALTER TABLE company ADD COLUMN description TEXT",
        "ALTER TABLE company ADD COLUMN industry VARCHAR(100)",
        "ALTER TABLE company ADD COLUMN size VARCHAR(50)",
        "ALTER TABLE company ADD COLUMN location VARCHAR(200)",
        # job - salary + interview config
        "ALTER TABLE job ADD COLUMN salary_usd INTEGER",
        "ALTER TABLE job ADD COLUMN employment_type VARCHAR(50)",
        "ALTER TABLE job ADD COLUMN interview_length VARCHAR(20) NOT NULL DEFAULT 'standard'",
        "ALTER TABLE job ADD COLUMN interview_focus TEXT",
        "ALTER TABLE job ADD COLUMN interview_questions TEXT",
        # application - payment + interview fields
        "ALTER TABLE application ADD COLUMN work_authorization TEXT",
        "ALTER TABLE application ADD COLUMN scheduled_meeting TEXT",
        "ALTER TABLE application ADD COLUMN match_score INTEGER",
        "ALTER TABLE application ADD COLUMN match_explanation TEXT",
        "ALTER TABLE application ADD COLUMN interview_transcript TEXT",
        "ALTER TABLE application ADD COLUMN interview_audio_log TEXT",
        "ALTER TABLE application ADD COLUMN interview_verdict VARCHAR(20)",
        "ALTER TABLE application ADD COLUMN fee_paid BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE application ADD COLUMN stripe_payment_link VARCHAR(500)",
        # user - verification + notifications
        "ALTER TABLE \"user\" ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE \"user\" ADD COLUMN notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE",
        # company - logo
        "ALTER TABLE company ADD COLUMN logo_filename VARCHAR(200)",
        "ALTER TABLE company ADD COLUMN logo_version INTEGER NOT NULL DEFAULT 0",
        # application - employer notes + withdrawal
        "ALTER TABLE application ADD COLUMN employer_notes TEXT",
        "ALTER TABLE application ADD COLUMN withdrawn BOOLEAN NOT NULL DEFAULT FALSE",
        # job - salary range
        "ALTER TABLE job ADD COLUMN salary_max_usd INTEGER",
        "ALTER TABLE job ADD COLUMN pay_type VARCHAR(20) NOT NULL DEFAULT 'yearly'",
        # application - rejection feedback sent to candidate
        "ALTER TABLE application ADD COLUMN candidate_feedback TEXT",
        # application - AI-generated skills gap roadmap
        "ALTER TABLE application ADD COLUMN skills_roadmap TEXT",
        # company - blind hiring mode
        "ALTER TABLE company ADD COLUMN blind_mode_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        # company - employer terms acceptance audit trail
        "ALTER TABLE company ADD COLUMN terms_accepted_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE company ADD COLUMN terms_accepted_ip VARCHAR(45)",
        "ALTER TABLE company ADD COLUMN terms_version VARCHAR(10)",
        # application - saved interview session for resume-later
        "ALTER TABLE application ADD COLUMN interview_saved_session TEXT",
        # user - admin flag
        "ALTER TABLE \"user\" ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE",
        # application - AI-generation likelihood score for candidate interview answers
        "ALTER TABLE application ADD COLUMN ai_likelihood TEXT",
        # user - supabase auth uid for admin API calls
        "ALTER TABLE \"user\" ADD COLUMN supabase_uid VARCHAR(200)",
        # candidate_profile - profile basics
        "ALTER TABLE candidate_profile ADD COLUMN headline VARCHAR(200)",
        "ALTER TABLE candidate_profile ADD COLUMN target_roles VARCHAR(300)",
        # candidate_profile - talent pool opt-in fields
        "ALTER TABLE candidate_profile ADD COLUMN open_to_work BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE candidate_profile ADD COLUMN skills TEXT",
        "ALTER TABLE candidate_profile ADD COLUMN work_preference VARCHAR(50)",
        "ALTER TABLE candidate_profile ADD COLUMN availability VARCHAR(50)",
        "ALTER TABLE candidate_profile ADD COLUMN years_experience VARCHAR(20)",
        "ALTER TABLE candidate_profile ADD COLUMN location VARCHAR(200)",
        # earn_task - domain gating for contractor network
        "ALTER TABLE earn_task ADD COLUMN required_domains TEXT",
        # earn_task - consensus scoring
        "ALTER TABLE earn_task ADD COLUMN min_responses INTEGER NOT NULL DEFAULT 3",
        "ALTER TABLE earn_task ADD COLUMN consensus_answer TEXT",
        # task_submission - per-submission agreement with consensus
        "ALTER TABLE task_submission ADD COLUMN agreement_score REAL",
        # contractor_profile - Stripe Connect payout flag
        "ALTER TABLE contractor_profile ADD COLUMN payout_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        # application - candidate must confirm a hire before the fee invoice fires
        "ALTER TABLE application ADD COLUMN candidate_confirmed_hire BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE application ADD COLUMN hire_confirmed_at TIMESTAMP WITH TIME ZONE",
        # user - werkzeug's scrypt password hashes run ~162 chars, longer than
        # the original 150-char cap, so every email/password signup was
        # failing with a StringDataRightTruncation error
        "ALTER TABLE \"user\" ALTER COLUMN password TYPE VARCHAR(255)",
        # user - link to a DataBuyer account (outside company subscribed to Earn task exports)
        "ALTER TABLE \"user\" ADD COLUMN data_buyer_id INTEGER REFERENCES data_buyer(id)",
        "ALTER TABLE \"user\" ADD COLUMN is_data_buyer_admin BOOLEAN NOT NULL DEFAULT FALSE",
        # company - work-email domain, so later employer signups from the same
        # company land in the same hub instead of each creating a new Company
        "ALTER TABLE company ADD COLUMN email_domain VARCHAR(150)",
        # company - typed e-signature on the Employer Platform Agreement
        "ALTER TABLE company ADD COLUMN terms_signature_name VARCHAR(200)",
        # application - actual hire compensation (fee basis), candidate dispute flag, evidence upload
        "ALTER TABLE application ADD COLUMN hire_salary_usd INTEGER",
        "ALTER TABLE application ADD COLUMN hire_pay_type VARCHAR(20)",
        "ALTER TABLE application ADD COLUMN hire_salary_disputed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE application ADD COLUMN hire_evidence_filename VARCHAR(200)",
        # application - recurring monthly billing for hourly hires, instead of one lump sum
        "ALTER TABLE application ADD COLUMN hire_billing_subscription_id VARCHAR(100)",
        "ALTER TABLE application ADD COLUMN hire_engagement_ended_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE application ADD COLUMN fee_reminder_sent_at TIMESTAMP WITH TIME ZONE",
        # job - currency the advertised salary is denominated in
        "ALTER TABLE job ADD COLUMN salary_currency VARCHAR(3) NOT NULL DEFAULT 'USD'",
        # user - referral program: own shareable code + who referred them
        "ALTER TABLE \"user\" ADD COLUMN referral_code VARCHAR(12)",
        "ALTER TABLE \"user\" ADD COLUMN referred_by_user_id INTEGER REFERENCES \"user\"(id)",
        # meeting_room - independent local-camera recordings captured client-side
        "ALTER TABLE meeting_room ADD COLUMN candidate_recording_path VARCHAR(300)",
        "ALTER TABLE meeting_room ADD COLUMN employer_recording_path VARCHAR(300)",
    ]
    for stmt in new_columns:
        try:
            with db.engine.connect() as conn:
                conn.execute(text(stmt))
                conn.commit()
        except Exception:
            pass

    _backfill_company_email_domains(db)
    _backfill_referral_codes(db)


def _backfill_referral_codes(db):
    """One-time backfill for users created before referral_code existed -
    ALTER TABLE ADD COLUMN leaves existing rows NULL since the model's
    default= only applies to new ORM inserts, not raw SQL. No-ops once every
    user already has a code."""
    import secrets
    from .models import User

    try:
        users = User.query.filter(User.referral_code.is_(None)).all()
        for user in users:
            user.referral_code = secrets.token_urlsafe(6)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_company_email_domains(db):
    """One-time backfill for companies created before email_domain existed -
    derives it from the owner's email so old companies also become joinable
    by domain. No-ops once every company already has a domain set."""
    from .models import Company, User, FREE_EMAIL_DOMAINS

    try:
        companies = Company.query.filter(Company.email_domain.is_(None)).all()
        for company in companies:
            owner = User.query.get(company.owner_user_id)
            if not owner or not owner.email or '@' not in owner.email:
                continue
            domain = owner.email.split('@')[-1].lower()
            if domain in FREE_EMAIL_DOMAINS:
                continue
            company.email_domain = domain
        db.session.commit()
    except Exception:
        db.session.rollback()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'ebsjkfbsdjkfbjkdfb')

    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        # Render/Heroku-style URLs sometimes use the old "postgres://" scheme,
        # which SQLAlchemy 1.4+ no longer accepts.
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        app.config['SQLALCHEMY_DATABASE_URI'] = database_url
        app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
            "pool_pre_ping": True,
            "connect_args": {"options": "-c default_transaction_read_only=off"},
        }
    else:
        #sqlite is stored at this location - local dev fallback only
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'

    app.config.from_object(Config)
    db.init_app(app)
    mail.init_app(app)
    limiter.init_app(app)

    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    audio_path = os.path.join(BASE_DIR, "..", "audio")
    upload_path = os.path.join(BASE_DIR, "..", "uploads")

    app.config["AUDIO_FOLDER"] = audio_path
    app.config["UPLOAD_FOLDER"] = upload_path

    os.makedirs(audio_path, exist_ok=True)
    os.makedirs(upload_path, exist_ok=True)

    from .models import User, Company, Job, CandidateProfile, Application, CompanyMember, CompanyInvite, EarnTask, TaskSubmission, SavedJob, ContractorProfile, MeetingRoom, Message, EmployerSubscription, MeetingProposal, DataBuyer, Feedback, FeedbackInsight, DeletedJob

    with app.app_context():
        db.create_all()
        _run_migrations(db)
        from community_stats import initialize_counter
        initialize_counter(db.engine)

    from .views import views
    from .auth import auth
    from .employer import employer
    from .candidate import candidate
    from .earn import earn
    from .admin import admin_bp
    from .data_market import data_market

    app.register_blueprint(views, url_prefix='/')
    app.register_blueprint(auth, url_prefix='/')
    app.register_blueprint(employer, url_prefix='/')
    app.register_blueprint(candidate, url_prefix='/')
    app.register_blueprint(earn, url_prefix='/')
    app.register_blueprint(admin_bp, url_prefix='/')
    app.register_blueprint(data_market, url_prefix='/')

    from datetime import datetime, timezone
    from flask_login import current_user as _cu

    @app.after_request
    def stash_referral_code(response):
        """?ref=CODE on any page load gets stashed in a cookie so it survives
        the multi-step signup flow (including the Google/Supabase OAuth
        redirect chain) - read back in auth.py when the User row is actually
        created. Doesn't overwrite an existing cookie, so the first referrer
        wins if someone clicks multiple referral links before signing up."""
        ref = request.args.get('ref')
        if ref and not request.cookies.get('ref_code'):
            response.set_cookie('ref_code', ref, max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax')
        return response

    @app.context_processor
    def inject_globals():
        if not _cu.is_authenticated:
            return {"unread_messages": 0}
        from .models import Message, Application, Job, Company, CompanyMember
        try:
            base_q = (
                db.session.query(db.func.count(Message.id))
                .join(Application, Application.id == Message.application_id)
                .filter(Message.read == False, Message.sender_user_id != _cu.id)
            )
            if _cu.role == 'employer':
                company = Company.query.filter_by(owner_user_id=_cu.id).first()
                if not company:
                    membership = CompanyMember.query.filter_by(user_id=_cu.id).first()
                    company = membership.company if membership else None
                count = (
                    base_q.join(Job, Job.id == Application.job_id)
                    .filter(Job.company_id == company.id)
                    .scalar() or 0
                ) if company else 0
            else:
                count = base_q.filter(Application.candidate_user_id == _cu.id).scalar() or 0
            return {"unread_messages": count}
        except Exception:
            db.session.rollback()
            return {"unread_messages": 0}

    import json as _json

    @app.template_filter('from_json')
    def from_json_filter(s):
        try:
            return _json.loads(s)
        except Exception:
            return []

    @app.template_filter('time_ago')
    def time_ago_filter(dt):
        if not dt:
            return ''
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now - dt
        s = int(diff.total_seconds())
        if s < 60:
            return 'just now'
        if s < 3600:
            m = s // 60
            return f"{m} minute{'s' if m != 1 else ''} ago"
        if s < 86400:
            h = s // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        d = s // 86400
        if d < 30:
            return f"{d} day{'s' if d != 1 else ''} ago"
        mo = d // 30
        if mo < 12:
            return f"{mo} month{'s' if mo != 1 else ''} ago"
        yr = d // 365
        return f"{yr} year{'s' if yr != 1 else ''} ago"

    @app.template_filter('salary_range')
    def salary_range_filter(job):
        if not job.salary_usd:
            return None
        from .models import SUPPORTED_CURRENCIES, DEFAULT_CURRENCY
        pay_type = getattr(job, 'pay_type', None) or 'yearly'
        currency = getattr(job, 'salary_currency', None) or DEFAULT_CURRENCY
        symbol = SUPPORTED_CURRENCIES.get(currency, SUPPORTED_CURRENCIES[DEFAULT_CURRENCY])['symbol']

        # Formatting: hourly stays as raw number; everything else uses K if >= 1000
        def fmt(n, force_raw=False):
            if force_raw or n < 1000:
                return f"{symbol}{n:,}"
            return f"{symbol}{round(n / 1000)}K"

        hourly = pay_type == 'hourly'
        lo = fmt(job.salary_usd, force_raw=hourly)
        hi = fmt(job.salary_max_usd, force_raw=hourly) if job.salary_max_usd and job.salary_max_usd > job.salary_usd else None

        suffixes = {
            'yearly':   '/yr',
            'hourly':   '/hr',
            'monthly':  '/mo',
            'stipend':  ' stipend',
            'contract': ' (project)',
        }
        suffix = suffixes.get(pay_type, '/yr')

        if hi:
            return f"{lo}–{hi}{suffix}"
        return f"{lo}{suffix}"


    login_manager = LoginManager()
    login_manager.login_view = 'auth.login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(id):
        return User.query.get(int(id))

    return app

#function checks if the database exists
def create_database(app):
    if not path.exists('website/' + DB_NAME):      
        print('Created Database!')

