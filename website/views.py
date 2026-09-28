import stripe
from flask import Blueprint, render_template, redirect, url_for, request, current_app, Response, abort
from flask_login import current_user, login_required
from datetime import timezone
from sqlalchemy.sql import func

from . import db
from .models import Application, Job, User, Company, MeetingRoom, Referral
from .employer import _placement_fee_basis

REFERRAL_COMMISSION_PERCENT = 10  # referrer's cut of the platform's placement fee, not of the hire's salary

views = Blueprint('views', __name__)

def _landing_context():
    latest_jobs = (Job.query
        .filter_by(status='open')
        .order_by(Job.created_at.desc())
        .limit(8)
        .all())

    job_data = [{"job": job} for job in latest_jobs]

    return {"job_data": job_data}


@views.route('/')
@views.route('/home')
def home():
    if current_user.is_authenticated:
        if current_user.role == 'employer':
            return redirect(url_for('employer.dashboard'))
        return redirect(url_for('candidate.browse_jobs'))

    return render_template("landing.html", **_landing_context())


@views.route('/site')
def public_site():
    """Marketing landing page for signed-in users - home() bounces them
    straight to their dashboard, so this is the only way to see it again
    while logged in."""
    return render_template("landing.html", **_landing_context())


def _credit_referral(application):
    """Owe the referrer 10% of the platform's 15% placement fee, once,
    the first time an application's fee is actually paid. No-ops if the
    hired candidate wasn't referred, or a Referral already exists for this
    application (the unique constraint on application_id also guards this)."""
    candidate = User.query.get(application.candidate_user_id)
    if not candidate or not candidate.referred_by_user_id:
        return
    if Referral.query.filter_by(application_id=application.id).first():
        return

    basis_amount, fee_percent = _placement_fee_basis(application, application.job)
    if not basis_amount or not fee_percent:
        return

    platform_fee = basis_amount * fee_percent / 100
    commission = round(platform_fee * REFERRAL_COMMISSION_PERCENT / 100)
    if commission <= 0:
        return

    db.session.add(Referral(
        referrer_user_id=candidate.referred_by_user_id,
        referred_user_id=candidate.id,
        application_id=application.id,
        commission_usd=commission,
    ))


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
        metadata = session["metadata"] if "metadata" in session else {}
        app_id = metadata["application_id"] if "application_id" in metadata else None
        if app_id:
            application = Application.query.get(int(app_id))
            if application:
                if not application.fee_paid:
                    application.fee_paid = True
                    _credit_referral(application)
                # Hourly hires check out in subscription mode - capture the
                # subscription id so employer.end_hourly_engagement can
                # cancel it later when the engagement actually ends.
                subscription_id = session.get("subscription")
                if subscription_id and not application.hire_billing_subscription_id:
                    application.hire_billing_subscription_id = subscription_id
                db.session.commit()

    elif event["type"] == "customer.subscription.deleted":
        # Covers cancellations that happen directly in Stripe (not via the
        # "End Engagement" button), so the app's state doesn't drift out of
        # sync with what's actually being billed.
        subscription = event["data"]["object"]
        sub_id = subscription.get("id")
        if sub_id:
            application = Application.query.filter_by(hire_billing_subscription_id=sub_id).first()
            if application and not application.hire_engagement_ended_at:
                application.hire_engagement_ended_at = func.now()
                db.session.commit()

    return "", 200


@views.route('/sitemap.xml')
def sitemap():
    jobs = Job.query.filter_by(status='open').order_by(Job.created_at.desc()).all()
    base = request.host_url.rstrip('/')

    def _lastmod(job):
        dt = job.created_at
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime('%Y-%m-%d') if dt else ''

    urls = [
        f"<url><loc>{base}/jobs</loc><changefreq>daily</changefreq><priority>0.9</priority></url>",
        f"<url><loc>{base}/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>",
    ]
    for job in jobs:
        loc = f"{base}/jobs/{job.id}"
        lm = _lastmod(job)
        urls.append(f"<url><loc>{loc}</loc>{'<lastmod>' + lm + '</lastmod>' if lm else ''}<changefreq>weekly</changefreq><priority>0.8</priority></url>")

    xml = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(urls) + '</urlset>'
    return Response(xml, mimetype='application/xml')


def _resolve_meeting_access(room):
    """Returns (application, is_employer) for the current user's relationship
    to this meeting room, or aborts 403 if they have none."""
    application = Application.query.get_or_404(room.application_id)

    if current_user.id == application.candidate_user_id:
        return application, False
    if current_user.role == 'employer':
        job = Job.query.get_or_404(application.job_id)
        from .models import CompanyMember
        owns = Company.query.filter_by(owner_user_id=current_user.id, id=job.company_id).first()
        member = CompanyMember.query.filter_by(user_id=current_user.id, company_id=job.company_id).first()
        if not owns and not member:
            abort(403)
        return application, True
    abort(403)


@views.route('/meeting/<room_name>')
@login_required
def join_meeting(room_name):
    room = MeetingRoom.query.filter_by(room_name=room_name).first_or_404()
    application, is_employer = _resolve_meeting_access(room)
    return render_template('meeting_room.html', room=room, application=application, is_employer=is_employer)


@views.route('/meeting/<room_name>/recording', methods=['POST'])
@login_required
def upload_meeting_recording_route(room_name):
    """Receives a participant's own local-camera recording of the call (see
    meeting_room.html's client-side MediaRecorder), uploaded once they click
    'Save & Leave' rather than on every chunk - keeps this to one request per
    person per call."""
    room = MeetingRoom.query.filter_by(room_name=room_name).first_or_404()
    _, is_employer = _resolve_meeting_access(room)

    recording = request.files.get('recording')
    if not recording or not recording.filename:
        return {"error": "No recording provided."}, 400

    from video_storage import upload_meeting_recording

    try:
        path = upload_meeting_recording(room.id, 'employer' if is_employer else 'candidate', recording.read())
    except Exception:
        current_app.logger.exception(f"Failed to upload meeting recording for room {room.id}")
        return {"error": "Upload failed."}, 502

    if is_employer:
        room.employer_recording_path = path
    else:
        room.candidate_recording_path = path
    db.session.commit()

    return {"status": "saved"}


@views.route('/meeting/<room_name>/recording/<role>')
@login_required
def play_meeting_recording(room_name, role):
    if role not in ('candidate', 'employer'):
        abort(404)

    room = MeetingRoom.query.filter_by(room_name=room_name).first_or_404()
    _resolve_meeting_access(room)  # any authorized party may view either side's recording

    path = room.employer_recording_path if role == 'employer' else room.candidate_recording_path
    if not path:
        abort(404)

    from video_storage import get_signed_meeting_recording_url
    return redirect(get_signed_meeting_recording_url(path))


@views.route('/terms')
def terms():
    return render_template('terms.html')


@views.route('/privacy')
def privacy():
    return render_template('privacy.html')


@views.route('/robots.txt')
def robots():
    content = f"User-agent: *\nAllow: /\nDisallow: /employer/\nDisallow: /candidate/\nDisallow: /admin/\nSitemap: {request.host_url.rstrip('/')}/sitemap.xml\n"
    return Response(content, mimetype='text/plain')
