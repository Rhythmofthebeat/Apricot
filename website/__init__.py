import os
from os import path
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from sqlalchemy import text
from dotenv import load_dotenv
from config import Config

load_dotenv(override=True)

db = SQLAlchemy()
mail = Mail()
DB_NAME = "database.db"


def _run_migrations(db):
    """Add columns that were introduced after the initial schema.
    Each ALTER TABLE is wrapped in try/except — safe to run on every
    startup, no-op when the column already exists."""
    new_columns = [
        # company — extended profile fields
        "ALTER TABLE company ADD COLUMN description TEXT",
        "ALTER TABLE company ADD COLUMN industry VARCHAR(100)",
        "ALTER TABLE company ADD COLUMN size VARCHAR(50)",
        "ALTER TABLE company ADD COLUMN location VARCHAR(200)",
        # job — salary + interview config
        "ALTER TABLE job ADD COLUMN salary_usd INTEGER",
        "ALTER TABLE job ADD COLUMN employment_type VARCHAR(50)",
        "ALTER TABLE job ADD COLUMN interview_length VARCHAR(20) NOT NULL DEFAULT 'standard'",
        "ALTER TABLE job ADD COLUMN interview_focus TEXT",
        "ALTER TABLE job ADD COLUMN interview_questions TEXT",
        # application — payment + interview fields
        "ALTER TABLE application ADD COLUMN work_authorization TEXT",
        "ALTER TABLE application ADD COLUMN scheduled_meeting TEXT",
        "ALTER TABLE application ADD COLUMN match_score INTEGER",
        "ALTER TABLE application ADD COLUMN match_explanation TEXT",
        "ALTER TABLE application ADD COLUMN interview_transcript TEXT",
        "ALTER TABLE application ADD COLUMN interview_audio_log TEXT",
        "ALTER TABLE application ADD COLUMN interview_verdict VARCHAR(20)",
        "ALTER TABLE application ADD COLUMN fee_paid BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE application ADD COLUMN stripe_payment_link VARCHAR(500)",
        # user — verification + notifications
        "ALTER TABLE user ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE user ADD COLUMN notifications_enabled BOOLEAN NOT NULL DEFAULT 1",
        # company — logo
        "ALTER TABLE company ADD COLUMN logo_filename VARCHAR(200)",
        "ALTER TABLE company ADD COLUMN logo_version INTEGER NOT NULL DEFAULT 0",
        # application — employer notes + withdrawal
        "ALTER TABLE application ADD COLUMN employer_notes TEXT",
        "ALTER TABLE application ADD COLUMN withdrawn BOOLEAN NOT NULL DEFAULT 0",
        # job — salary range
        "ALTER TABLE job ADD COLUMN salary_max_usd INTEGER",
        "ALTER TABLE job ADD COLUMN pay_type VARCHAR(20) NOT NULL DEFAULT 'yearly'",
        # application — rejection feedback sent to candidate
        "ALTER TABLE application ADD COLUMN candidate_feedback TEXT",
        # application — AI-generated skills gap roadmap
        "ALTER TABLE application ADD COLUMN skills_roadmap TEXT",
        # company — blind hiring mode
        "ALTER TABLE company ADD COLUMN blind_mode_enabled BOOLEAN NOT NULL DEFAULT 0",
        # application — saved interview session for resume-later
        "ALTER TABLE application ADD COLUMN interview_saved_session TEXT",
        # user — admin flag
        "ALTER TABLE \"user\" ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE",
        # application — AI-generation likelihood score for candidate interview answers
        "ALTER TABLE application ADD COLUMN ai_likelihood TEXT",
        # user — supabase auth uid for admin API calls
        "ALTER TABLE \"user\" ADD COLUMN supabase_uid VARCHAR(200)",
        # candidate_profile — talent pool opt-in fields
        "ALTER TABLE candidate_profile ADD COLUMN open_to_work BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE candidate_profile ADD COLUMN skills TEXT",
        "ALTER TABLE candidate_profile ADD COLUMN work_preference VARCHAR(50)",
        "ALTER TABLE candidate_profile ADD COLUMN availability VARCHAR(50)",
        "ALTER TABLE candidate_profile ADD COLUMN years_experience VARCHAR(20)",
        "ALTER TABLE candidate_profile ADD COLUMN location VARCHAR(200)",
    ]
    with db.engine.connect() as conn:
        for stmt in new_columns:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

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
        #sqlite is stored at this location — local dev fallback only
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_NAME}'

    app.config.from_object(Config)
    db.init_app(app) # we are going to use this app
    mail.init_app(app)

    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    audio_path = os.path.join(BASE_DIR, "..", "audio")
    upload_path = os.path.join(BASE_DIR, "..", "uploads")

    app.config["AUDIO_FOLDER"] = os.path.join(os.path.dirname(__file__), "audio")
    app.config["AUDIO_FOLDER"] = audio_path

    app.config["AUDIO_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
    app.config["UPLOAD_FOLDER"] = upload_path

    
    os.makedirs(upload_path, exist_ok=True)

    from .models import User, Company, Job, CandidateProfile, Application, CompanyMember, CompanyInvite, EarnTask, TaskSubmission, SavedJob

    with app.app_context():
        db.create_all()
        _run_migrations(db)

    from .views import views
    from .auth import auth
    from .employer import employer
    from .candidate import candidate
    from .earn import earn
    from .admin import admin_bp

    app.register_blueprint(views, url_prefix='/')
    app.register_blueprint(auth, url_prefix='/')
    app.register_blueprint(employer, url_prefix='/')
    app.register_blueprint(candidate, url_prefix='/')
    app.register_blueprint(earn, url_prefix='/')
    app.register_blueprint(admin_bp, url_prefix='/')

    from datetime import datetime, timezone

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
        pay_type = getattr(job, 'pay_type', None) or 'yearly'

        # Formatting: hourly stays as raw number; everything else uses K if >= 1000
        def fmt(n, force_raw=False):
            if force_raw or n < 1000:
                return f"${n:,}"
            return f"${round(n / 1000)}K"

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

