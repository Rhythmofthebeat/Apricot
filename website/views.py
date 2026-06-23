from flask import Blueprint, render_template, redirect, url_for
from flask_login import current_user


views = Blueprint('views', __name__)


@views.route('/')
@views.route('/home')
def home():
    if not current_user.is_authenticated:
        return render_template("landing.html")
    if current_user.role == 'employer':
        return redirect(url_for('employer.dashboard'))
    return redirect(url_for('candidate.browse_jobs'))
