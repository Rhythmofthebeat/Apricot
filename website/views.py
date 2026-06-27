import stripe
from flask import Blueprint, render_template, redirect, url_for, request, current_app
from flask_login import current_user

from . import db
from .models import Application, Job, User, Company

views = Blueprint('views', __name__)

_AVATAR_COLORS = [
    "#4f46e5", "#0891b2", "#059669", "#d97706",
    "#dc2626", "#7c3aed", "#db2777", "#0284c7",
]

def _avatar_color(letter):
    return _AVATAR_COLORS[ord(letter.upper()) % len(_AVATAR_COLORS)]


@views.route('/')
@views.route('/home')
def home():
    if not current_user.is_authenticated:
        latest_jobs = (Job.query
            .filter_by(status='open')
            .order_by(Job.created_at.desc())
            .limit(8)
            .all())

        job_data = []
        for job in latest_jobs:
            app_count = Application.query.filter_by(job_id=job.id).count()
            recent_apps = (Application.query
                .filter_by(job_id=job.id)
                .limit(4)
                .all())
            avatars = []
            for app in recent_apps:
                u = User.query.get(app.candidate_user_id)
                if u and u.first_name:
                    avatars.append({
                        "letter": u.first_name[0].upper(),
                        "color": _avatar_color(u.first_name[0]),
                    })
            job_data.append({"job": job, "app_count": app_count, "avatars": avatars})

        open_job_count = Job.query.filter_by(status='open').count()
        candidate_count = User.query.filter_by(role='candidate').count()
        company_count = Company.query.count()

        return render_template("landing.html",
            job_data=job_data,
            open_job_count=open_job_count,
            candidate_count=candidate_count,
            company_count=company_count,
        )
    if current_user.role == 'employer':
        return redirect(url_for('employer.dashboard'))
    return redirect(url_for('candidate.browse_jobs'))


@views.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Stripe sends checkout.session.completed when a payment link is paid.
    We look up the application by metadata and mark fee_paid = True."""
    stripe.api_key = current_app.config.get("STRIPE_SECRET_KEY")
    webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")

    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = stripe.Event.construct_from(
                stripe.util.convert_to_stripe_object(request.get_json()),
                stripe.api_key,
            )
    except (ValueError, stripe.error.SignatureVerificationError):
        return "Bad request", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        app_id = (session.get("metadata") or {}).get("application_id")
        if app_id:
            application = Application.query.get(int(app_id))
            if application and not application.fee_paid:
                application.fee_paid = True
                db.session.commit()

    return "", 200
