import os
from os import path
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from sqlalchemy import text
from config import Config

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
        # job — salary + interview config (added in later commits)
        "ALTER TABLE job ADD COLUMN salary_usd INTEGER",
        "ALTER TABLE job ADD COLUMN employment_type VARCHAR(50)",
        "ALTER TABLE job ADD COLUMN interview_length VARCHAR(20) NOT NULL DEFAULT 'standard'",
        "ALTER TABLE job ADD COLUMN interview_focus TEXT",
        "ALTER TABLE job ADD COLUMN interview_questions TEXT",
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

    from .models import User, Company, Job, CandidateProfile, Application, CompanyMember, CompanyInvite

    with app.app_context():
        db.create_all()
        _run_migrations(db)

    from .views import views
    from .auth import auth
    from .employer import employer
    from .candidate import candidate

    app.register_blueprint(views, url_prefix = '/')
    app.register_blueprint(auth, url_prefix = '/')
    app.register_blueprint(employer, url_prefix = '/')
    app.register_blueprint(candidate, url_prefix = '/')


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

