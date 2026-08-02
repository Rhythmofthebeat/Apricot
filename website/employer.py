import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

import stripe
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, jsonify, current_app, send_from_directory
from flask_login import login_required, current_user
from functools import wraps
from PIL import Image
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from . import db, limiter
from .models import (
    Company, Job, Application, Notification,
    CompanyMember, CompanyInvite, CandidateProfile, User,
    MeetingRoom, Message, MeetingProposal, SavedJob,
    JOB_CATEGORIES, JOB_LOCATIONS, COMPANY_SIZES, INDUSTRIES,
)
from ai_services import generate_interview_question_preview, generate_rejection_feedback, analyze_job_description, transcribe_audio, compare_resume_to_job
from .candidate import _extract_score
from email_utils import send_branded_email, external_url

employer = Blueprint('employer', __name__)

MAX_INTERVIEW_QUESTIONS = 15

_VERDICT_SCORE = {"Strong Yes": 100, "Yes": 75, "No": 25, "Strong No": 0}

def compute_composite_score(app, ai_data):
    """Weighted overall score: resume 45%, interview verdict 45%, AI authenticity 10%.
    If no interview exists yet, falls back to resume score alone."""
    resume = app.match_score or 0
    if app.interview_verdict in _VERDICT_SCORE:
        verdict = _VERDICT_SCORE[app.interview_verdict]
        ai_penalty = (ai_data.get("score", 0) if ai_data else 0)
        return round(resume * 0.45 + verdict * 0.45 + (100 - ai_penalty) * 0.10)
    return resume
PLATFORM_FEE_PERCENT = 15          # salaried roles: 15% of first-year comp
HOURLY_PLATFORM_FEE_PERCENT = 20   # hourly roles: 20% of the annualized rate

STANDARD_ANNUAL_HOURS = 2080  # 40hrs/week x 52 weeks - used to annualize hourly rates


def _placement_fee_basis(application, job):
    """Return (basis_amount, fee_percent) for the placement fee.

    Uses the actual negotiated compensation entered by the employer when
    marking the candidate hired (application.hire_salary_usd/hire_pay_type),
    not the job posting's advertised salary - an employer has no incentive
    to keep the posting accurate once someone's actually hired, and the
    candidate cross-checks this figure before it's ever invoiced (see
    candidate.confirm_hire / candidate.dispute_hire). Falls back to the job
    posting only for legacy applications hired before this existed.

    Salaried/monthly roles use first-year compensation directly. Hourly
    roles are annualized to a comparable full-time figure first, and
    charged at a higher rate since the actual hours worked are less certain
    than a salaried hire's.
    """
    salary = application.hire_salary_usd or job.salary_usd
    pay_type = application.hire_pay_type or job.pay_type
    if not salary:
        return None, None
    if pay_type == 'hourly':
        return salary * STANDARD_ANNUAL_HOURS, HOURLY_PLATFORM_FEE_PERCENT
    if pay_type == 'monthly':
        return salary * 12, PLATFORM_FEE_PERCENT
    return salary, PLATFORM_FEE_PERCENT


HIRE_EVIDENCE_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}


def _hire_evidence_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "hire_evidence")
    os.makedirs(path, exist_ok=True)
    return path


def _save_hire_evidence(application_id, file_storage):
    """Save an optional offer-letter/screenshot upload for a hire. Returns
    the stored filename, or None if no valid file was given."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in HIRE_EVIDENCE_ALLOWED_EXTENSIONS:
        return None
    filename = f"{application_id}_{secrets.token_hex(8)}.{ext}"
    file_storage.save(os.path.join(_hire_evidence_dir(), filename))
    return filename


LOGO_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
LOGO_MAX_SIZE = (800, 800)
INTERVIEW_LENGTH_OPTIONS = ["short", "standard", "thorough"]

STATUS_EMAIL_COPY = {
    "interviewing": {
        "subject_tpl": "Your application is moving forward - {job_title}",
        "heading": "You're being considered for the role",
        "body_tpl": (
            "Good news - {company} has moved your application for <strong>{job_title}</strong> "
            "into the interviewing stage. They'll be in touch with next steps."
        ),
    },
    "hired": {
        "subject_tpl": "You're hired - {job_title}",
        "heading": "Congratulations!",
        "body_tpl": (
            "{company} has decided to move forward with hiring you for the <strong>{job_title}</strong> "
            "position. They'll be in touch with next steps."
        ),
    },
    "rejected": {
        "subject_tpl": "Update on your application - {job_title}",
        "heading": "Application update",
        "body_tpl": (
            "Thank you for applying to <strong>{job_title}</strong> at {company}. After review, they've "
            "decided not to move forward with your application at this time. We encourage you to keep "
            "applying to other roles on the platform."
        ),
    },
}


# ------------------------------------------------------------------ #
#  Auth helpers
# ------------------------------------------------------------------ #

def get_user_company():
    """Return the Company this user owns or is a member of, or None."""
    company = Company.query.filter_by(owner_user_id=current_user.id).first()
    if company:
        return company
    membership = CompanyMember.query.filter_by(user_id=current_user.id).first()
    if membership:
        return membership.company
    return None


def employer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user.role != 'employer':
            abort(403)
        if not get_user_company():
            return redirect(url_for('auth.complete_employer_profile'))
        return view(*args, **kwargs)
    return wrapped


def get_owned_company():
    company = get_user_company()
    if not company:
        abort(403)
    return company


def is_company_owner():
    return Company.query.filter_by(owner_user_id=current_user.id).first() is not None


# ------------------------------------------------------------------ #
#  Private helpers
# ------------------------------------------------------------------ #

def send_hire_fee_invoice(application, job, company, candidate, employer_user):
    """Create the Stripe placement-fee payment link and invoice the employer.
    Only called once the candidate has confirmed the hire - see candidate.confirm_hire."""
    payment_link_url = None
    if not application.stripe_payment_link:
        payment_link_url = _create_stripe_payment_link(application, job, company, candidate)
        if payment_link_url:
            application.stripe_payment_link = payment_link_url
            db.session.commit()

    hire_salary = application.hire_salary_usd or job.salary_usd
    hire_pay_type = application.hire_pay_type or job.pay_type

    basis_amount, fee_percent = _placement_fee_basis(application, job)
    fee_amount = int(basis_amount * fee_percent / 100) if basis_amount else None
    basis_label = (
        f"annualized rate of ${basis_amount:,}/yr (${hire_salary:,}/hr x {STANDARD_ANNUAL_HOURS:,} hrs)"
        if hire_pay_type == 'hourly' else f"first-year compensation of ${basis_amount:,}"
    )
    try:
        if payment_link_url and fee_amount:
            send_branded_email(
                to_email=employer_user.email,
                subject=f"Placement fee invoice - {candidate.first_name} / {job.title}",
                heading="Placement fee due",
                body_html=(
                    f"<strong>{candidate.first_name}</strong> has confirmed their hire for "
                    f"<strong>{job.title}</strong> through Avants AI.<br><br>"
                    f"The platform placement fee is <strong>${fee_amount:,}</strong> "
                    f"({fee_percent}% of {basis_label}).<br><br>"
                    f"This is due within 30 days of the candidate's start date. "
                    f"Click below to pay securely via Stripe."
                ),
                plain_text=(
                    f"{candidate.first_name} has confirmed their hire for {job.title} through Avants AI.\n\n"
                    f"Placement fee: ${fee_amount:,} ({fee_percent}% of {basis_label}).\n"
                    f"Pay here: {payment_link_url}"
                ),
                cta_label="Pay Placement Fee",
                cta_url=payment_link_url,
            )
        else:
            fee_percent_display = fee_percent or PLATFORM_FEE_PERCENT
            send_branded_email(
                to_email=employer_user.email,
                subject=f"Placement fee - {candidate.first_name} / {job.title}",
                heading="Placement fee due",
                body_html=(
                    f"<strong>{candidate.first_name}</strong> has confirmed their hire for "
                    f"<strong>{job.title}</strong> through Avants AI.<br><br>"
                    f"A placement fee of <strong>{fee_percent_display}% of first-year compensation</strong> "
                    f"is due within 30 days of the candidate's start date. "
                    f"Our team will follow up with an invoice."
                ),
                plain_text=(
                    f"{candidate.first_name} has confirmed their hire for {job.title} through Avants AI.\n\n"
                    f"A {fee_percent_display}% placement fee will be invoiced to you within 30 days of the start date."
                ),
                cta_label="View Dashboard",
                cta_url=external_url('employer.dashboard'),
            )
    except Exception as e:
        print(f"Hire fee invoice email failed: {e}")


def _create_stripe_payment_link(application, job, company, candidate):
    stripe.api_key = current_app.config.get("STRIPE_SECRET_KEY")
    basis_amount, fee_percent = _placement_fee_basis(application, job)
    if not stripe.api_key or not basis_amount:
        return None
    fee_cents = int(basis_amount * fee_percent / 100 * 100)
    if fee_cents <= 0:
        return None
    try:
        product = stripe.Product.create(
            name=f"Avants AI Placement Fee - {job.title} at {company.name}",
            metadata={"application_id": str(application.id)},
        )
        price = stripe.Price.create(product=product.id, unit_amount=fee_cents, currency="usd")
        link = stripe.PaymentLink.create(
            line_items=[{"price": price.id, "quantity": 1}],
            metadata={"application_id": str(application.id)},
            customer_creation="always",
        )
        return link.url
    except Exception as e:
        print("Stripe payment link creation failed:", e)
        return None


def _extract_description_text(raw):
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            parts = []
            for key in ('about', 'requirements', 'preferred', 'what_you_do', 'contract_terms'):
                val = (data.get(key) or '').strip()
                if val:
                    parts.append(val)
            return '\n\n'.join(parts)
    except (ValueError, TypeError):
        pass
    return raw or ''


def _parse_interview_questions(raw):
    if not raw:
        return None
    try:
        questions = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(questions, list):
        return None
    cleaned = [q.strip() for q in questions if isinstance(q, str) and q.strip()]
    return json.dumps(cleaned[:MAX_INTERVIEW_QUESTIONS]) if cleaned else None


def _existing_interview_questions(job):
    if not job or not job.interview_questions:
        return []
    try:
        return json.loads(job.interview_questions)
    except (ValueError, TypeError):
        return []


def summarize_explanation(explanation, max_len=140):
    if not explanation:
        return "No summary available..."
    match_lines = [
        line.strip().lstrip('+').strip()
        for line in explanation.split('\n')
        if line.strip().startswith('+')
    ]
    text = " ".join(match_lines[:2]) if match_lines else explanation.strip().replace('\n', ' ')
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text + "..."


# ------------------------------------------------------------------ #
#  Employer Dashboard
# ------------------------------------------------------------------ #

@employer.route('/employer/dashboard')
@login_required
@employer_required
def dashboard():
    company = get_owned_company()
    jobs = Job.query.filter_by(company_id=company.id).order_by(Job.created_at.desc()).all()
    applicant_counts = {job.id: Application.query.filter_by(job_id=job.id).count() for job in jobs}
    hired_counts = {job.id: Application.query.filter_by(job_id=job.id, status='hired').count() for job in jobs}

    total_applicants = sum(applicant_counts.values())
    total_hired = sum(hired_counts.values())
    open_jobs = sum(1 for j in jobs if j.status == 'open')

    members = CompanyMember.query.filter_by(company_id=company.id).all()
    owner_is_current = company.owner_user_id == current_user.id

    return render_template(
        "employer_dashboard.html",
        company=company,
        jobs=jobs,
        applicant_counts=applicant_counts,
        hired_counts=hired_counts,
        total_applicants=total_applicants,
        total_hired=total_hired,
        open_jobs=open_jobs,
        members=members,
        is_owner=owner_is_current,
    )


# ------------------------------------------------------------------ #
#  Job CRUD
# ------------------------------------------------------------------ #

@employer.route('/employer/jobs/new', methods=['GET', 'POST'])
@login_required
@employer_required
def post_job():
    company = get_owned_company()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        work_type = request.form.get('work_type', 'Remote (Worldwide)').strip()
        city = request.form.get('location_city', '').strip()
        if work_type in ('Hybrid', 'On-site') and city:
            location = f"{work_type} - {city}"
        else:
            location = work_type
        employment_type = request.form.get('employment_type', '').strip()
        category = request.form.get('category', 'Other')
        if category not in JOB_CATEGORIES:
            category = 'Other'
        interview_length = request.form.get('interview_length', 'standard')
        if interview_length not in INTERVIEW_LENGTH_OPTIONS:
            interview_length = 'standard'
        interview_focus = request.form.get('interview_focus', '').strip()
        interview_questions = _parse_interview_questions(request.form.get('interview_questions', ''))

        if not title or not description:
            flash("Title and description are required.", "error")
            return render_template(
                "post_job.html", categories=JOB_CATEGORIES, locations=JOB_LOCATIONS,
                interview_lengths=INTERVIEW_LENGTH_OPTIONS,
                interview_questions=json.loads(interview_questions) if interview_questions else [],
            )

        salary_raw = request.form.get('salary_usd', '').strip()
        salary_usd = int(salary_raw) if salary_raw.isdigit() else None
        salary_max_raw = request.form.get('salary_max_usd', '').strip()
        salary_max_usd = int(salary_max_raw) if salary_max_raw.isdigit() else None
        pay_type = request.form.get('pay_type', 'yearly')
        if pay_type not in ('yearly', 'hourly', 'monthly', 'stipend', 'contract'):
            pay_type = 'yearly'
        save_as_draft = request.form.get('save_as_draft') == '1'

        job = Job(
            company_id=company.id,
            title=title,
            description=description,
            location=location,
            employment_type=employment_type,
            category=category,
            interview_length=interview_length,
            interview_focus=interview_focus or None,
            interview_questions=interview_questions,
            salary_usd=salary_usd,
            salary_max_usd=salary_max_usd,
            pay_type=pay_type,
            status='draft' if save_as_draft else 'open',
        )
        db.session.add(job)
        db.session.commit()
        if save_as_draft:
            flash("Job saved as draft.", "success")
        else:
            flash("Job posted successfully!", "success")
        return redirect(url_for('employer.dashboard'))

    return render_template(
        "post_job.html",
        categories=JOB_CATEGORIES,
        locations=JOB_LOCATIONS,
        interview_lengths=INTERVIEW_LENGTH_OPTIONS,
        interview_questions=[],
    )


@employer.route('/employer/jobs/<int:job_id>/edit', methods=['GET', 'POST'])
@login_required
@employer_required
def edit_job(job_id):
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', 'Other')
        if category not in JOB_CATEGORIES:
            category = 'Other'
        interview_length = request.form.get('interview_length', 'standard')
        if interview_length not in INTERVIEW_LENGTH_OPTIONS:
            interview_length = 'standard'

        if not title or not description:
            flash("Title and description are required.", "error")
            return render_template(
                "post_job.html", job=job, categories=JOB_CATEGORIES, locations=JOB_LOCATIONS,
                interview_lengths=INTERVIEW_LENGTH_OPTIONS,
                interview_questions=_existing_interview_questions(job),
            )

        salary_raw = request.form.get('salary_usd', '').strip()
        salary_max_raw = request.form.get('salary_max_usd', '').strip()
        save_as_draft = request.form.get('save_as_draft') == '1'
        pay_type = request.form.get('pay_type', 'yearly')
        if pay_type not in ('yearly', 'hourly', 'monthly', 'stipend', 'contract'):
            pay_type = 'yearly'
        _edit_work_type = request.form.get('work_type', 'Remote (Worldwide)').strip()
        _edit_city = request.form.get('location_city', '').strip()
        job.title = title
        job.description = description
        job.location = f"{_edit_work_type} - {_edit_city}" if _edit_work_type in ('Hybrid', 'On-site') and _edit_city else _edit_work_type
        job.employment_type = request.form.get('employment_type', '').strip()
        job.category = category
        job.interview_length = interview_length
        job.interview_focus = request.form.get('interview_focus', '').strip() or None
        job.interview_questions = _parse_interview_questions(request.form.get('interview_questions', ''))
        job.status = 'draft' if save_as_draft else 'open'
        job.salary_usd = int(salary_raw) if salary_raw.isdigit() else None
        job.salary_max_usd = int(salary_max_raw) if salary_max_raw.isdigit() else None
        job.pay_type = pay_type
        db.session.commit()
        flash("Draft saved." if save_as_draft else "Job updated!", "success")
        return redirect(url_for('employer.dashboard'))

    return render_template(
        "post_job.html", job=job, categories=JOB_CATEGORIES, locations=JOB_LOCATIONS,
        interview_lengths=INTERVIEW_LENGTH_OPTIONS,
        interview_questions=_existing_interview_questions(job),
    )


@employer.route('/employer/jobs/<int:job_id>/publish', methods=['POST'])
@login_required
@employer_required
def publish_job(job_id):
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)
    job.status = 'open'
    db.session.commit()
    flash("Job published successfully.", "success")
    return redirect(url_for('employer.dashboard'))


@employer.route('/employer/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
@employer_required
def delete_job(job_id):
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)

    if Application.query.filter_by(job_id=job.id).count() > 0:
        flash("This job has applicants, so it can't be deleted - close it instead to stop new applications while keeping candidate history.", "error")
        return redirect(url_for('employer.dashboard'))

    SavedJob.query.filter_by(job_id=job.id).delete()
    db.session.delete(job)
    db.session.commit()
    flash("Job deleted.", "success")
    return redirect(url_for('employer.dashboard'))


@employer.route('/employer/preview-interview-questions', methods=['POST'])
@login_required
@employer_required
@limiter.limit("15 per hour")
def preview_interview_questions():
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    description = _extract_description_text(data.get('description') or '')
    interview_length = data.get('interview_length', 'standard')
    interview_focus = (data.get('interview_focus') or '').strip()

    if not title or not description:
        return jsonify({"error": "Add a job title and description first."}), 400

    try:
        questions = generate_interview_question_preview(title, description, interview_length, interview_focus)
        return jsonify({"questions": questions})
    except Exception as e:
        logger.exception("preview-interview-questions failed")
        msg = str(e)
        if "api_key" in msg.lower() or "authentication" in msg.lower():
            return jsonify({"error": "OpenAI API key is missing or invalid. Check your environment config."}), 500
        if "rate" in msg.lower():
            return jsonify({"error": "Rate limit hit. Wait a moment and try again."}), 429
        return jsonify({"error": f"Failed to generate questions: {msg}"}), 500


@employer.route('/employer/transcribe-question', methods=['POST'])
@login_required
@employer_required
@limiter.limit("20 per hour")
def transcribe_question():
    """Speech-to-text for an employer recording a custom interview question by voice."""
    audio = request.files.get('audio')
    if not audio or not audio.filename:
        return jsonify({"error": "No audio received."}), 400

    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
            audio.save(tmp.name)
            tmp_path = tmp.name
        text = transcribe_audio(tmp_path)
    except Exception:
        logger.exception("transcribe-question failed")
        return jsonify({"error": "Couldn't transcribe that recording. Try again or type the question."}), 500
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if not text:
        return jsonify({"error": "Didn't catch any speech - try recording again."}), 400
    return jsonify({"text": text})


@employer.route('/employer/jobs/<int:job_id>/score-resume', methods=['POST'])
@login_required
@employer_required
@limiter.limit("20 per hour")
def score_resume(job_id):
    """Score an externally-sourced resume (referral, LinkedIn candidate, etc.)
    against one of this employer's jobs - no application gets created, this
    is just a quick sourcing/screening check."""
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)

    data = request.get_json() or {}
    resume_text = (data.get('resume_text') or '').strip()
    if not resume_text:
        return jsonify({"error": "Add resume content first."}), 400

    try:
        result = compare_resume_to_job(job.description_text, resume_text)
    except Exception:
        logger.exception("score-resume failed")
        return jsonify({"error": "Failed to score this resume. Please try again."}), 500

    score = _extract_score(result)
    return jsonify({"score": score, "explanation": result.replace('*', '')})


# ------------------------------------------------------------------ #
#  Applicants
# ------------------------------------------------------------------ #

@employer.route('/employer/jobs/<int:job_id>/applicants')
@login_required
@employer_required
def applicants(job_id):
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)

    applications = Application.query.filter_by(job_id=job.id).all()
    summaries = {app.id: summarize_explanation(app.match_explanation) for app in applications}
    audio_logs = {
        app.id: json.loads(app.interview_audio_log) if app.interview_audio_log else []
        for app in applications
    }
    ai_likelihoods = {
        app.id: json.loads(app.ai_likelihood) if app.ai_likelihood else None
        for app in applications
    }
    work_auths = {
        app.id: json.loads(app.work_authorization) if app.work_authorization else None
        for app in applications
    }
    meetings = {
        app.id: json.loads(app.scheduled_meeting) if app.scheduled_meeting else None
        for app in applications
    }
    profiles = {
        app.candidate_user_id: CandidateProfile.query.filter_by(user_id=app.candidate_user_id).first()
        for app in applications
    }

    composite_scores = {
        app.id: compute_composite_score(app, ai_likelihoods.get(app.id))
        for app in applications
    }
    applications.sort(key=lambda a: composite_scores[a.id], reverse=True)

    # Blind mode: assign anonymous IDs in composite-score order
    blind_labels = {}
    if company.blind_mode_enabled:
        for i, app in enumerate(applications, 1):
            blind_labels[app.id] = f"Candidate #{i}"

    return render_template(
        "job_applicants.html",
        job=job,
        applications=applications,
        summaries=summaries,
        audio_logs=audio_logs,
        work_auths=work_auths,
        meetings=meetings,
        profiles=profiles,
        blind_mode=company.blind_mode_enabled,
        blind_labels=blind_labels,
        ai_likelihoods=ai_likelihoods,
        composite_scores=composite_scores,
    )


@employer.route('/employer/applications/<int:application_id>/schedule-meeting', methods=['POST'])
@login_required
@employer_required
def schedule_meeting(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    meeting_type = request.form.get('meeting_type')
    date = request.form.get('date', '').strip()
    time = request.form.get('time', '').strip()
    location_or_url = request.form.get('location_or_url', '').strip()
    notes = request.form.get('notes', '').strip()

    if meeting_type not in ('virtual', 'in_person') or not date or not time or not location_or_url:
        flash("Date, time, and a meeting link or address are required.", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    meeting = {"type": meeting_type, "date": date, "time": time, "location_or_url": location_or_url, "notes": notes}
    application.scheduled_meeting = json.dumps(meeting)
    db.session.commit()

    kind_label = "video call" if meeting_type == 'virtual' else "in-person meeting"
    where_label = f"Link: {location_or_url}" if meeting_type == 'virtual' else f"Location: {location_or_url}"
    candidate = application.candidate
    try:
        send_branded_email(
            to_email=candidate.email,
            subject=f"Meeting scheduled - {job.title} at {company.name}",
            heading="A meeting has been scheduled",
            body_html=(
                f"Hi {candidate.first_name or ''},<br><br>"
                f"<strong>{company.name}</strong> would like to meet with you about your application for "
                f"<strong>{job.title}</strong>.<br><br>"
                f"<strong>Type:</strong> {kind_label}<br>"
                f"<strong>Date:</strong> {date}<br>"
                f"<strong>Time:</strong> {time}<br>"
                f"<strong>{'Link' if meeting_type == 'virtual' else 'Location'}:</strong> {location_or_url}"
                + (f"<br><br><strong>Notes:</strong> {notes}" if notes else "")
            ),
            plain_text=(
                f"Hi {candidate.first_name or ''},\n\n"
                f"{company.name} would like to meet with you about your application for {job.title}.\n\n"
                f"Type: {kind_label}\nDate: {date}\nTime: {time}\n{where_label}\n"
                + (f"\nNotes: {notes}\n" if notes else "")
            ),
            cta_label="View My Applications" if meeting_type == 'virtual' else None,
            cta_url=external_url('candidate.dashboard') if meeting_type == 'virtual' else None,
        )
        flash("Meeting scheduled and the candidate has been emailed.", "success")
    except Exception as e:
        print("Meeting email send failed:", e)
        flash("Meeting saved, but the notification email couldn't be sent.", "error")

    return redirect(url_for('employer.applicants', job_id=job.id))


@employer.route('/employer/applications/<int:application_id>/status', methods=['POST'])
@login_required
@employer_required
def update_status(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    new_status = request.form.get('status')
    if new_status in ('applied', 'interviewing', 'rejected', 'hired'):
        if new_status == 'hired':
            # The placement fee is based on this figure, not the job posting's
            # advertised salary - require it up front rather than invoicing
            # off a number the employer has no incentive to keep accurate.
            salary_raw = request.form.get('hire_salary_usd', '').strip()
            pay_type = request.form.get('hire_pay_type', '').strip()
            try:
                salary_value = int(float(salary_raw)) if salary_raw else None
            except ValueError:
                salary_value = None
            if not salary_value or salary_value <= 0 or pay_type not in ('yearly', 'hourly', 'monthly', 'stipend', 'contract'):
                flash("Enter the candidate's actual compensation (amount and pay type) to mark them hired - the placement fee is calculated from this, not the job posting.", "error")
                return redirect(url_for('employer.applicants', job_id=job.id))

            application.hire_salary_usd = salary_value
            application.hire_pay_type = pay_type
            application.hire_salary_disputed = False  # fresh hire event, not yet re-disputed

            evidence_filename = _save_hire_evidence(application.id, request.files.get('hire_evidence'))
            if evidence_filename:
                application.hire_evidence_filename = evidence_filename

        status_changed = application.status != new_status
        application.status = new_status
        db.session.commit()

        copy = STATUS_EMAIL_COPY.get(new_status)
        if status_changed and copy:
            candidate = application.candidate

            if new_status == 'hired':
                # Reset confirmation state - this is a fresh hire event, not yet verified by the candidate.
                # The placement fee invoice only fires once they confirm (see candidate.confirm_hire).
                application.candidate_confirmed_hire = False
                application.hire_confirmed_at = None
                db.session.commit()

            if candidate.notifications_enabled:
                notif_messages = {
                    "interviewing": f"Your application for {job.title} at {company.name} is moving forward!",
                    "hired": f"Congratulations! {company.name} has hired you for {job.title}.",
                    "rejected": f"Update on your application for {job.title} at {company.name}.",
                }
                notif = Notification(
                    user_id=candidate.id,
                    message=notif_messages.get(new_status, f"Your application status changed to {new_status}."),
                    link=url_for('candidate.dashboard', _external=False),
                )
                db.session.add(notif)
                db.session.commit()

            try:
                if new_status == 'hired':
                    # Email the candidate - congratulations + ask them to confirm the hire.
                    # The employer isn't invoiced for the placement fee until the candidate confirms.
                    send_branded_email(
                        to_email=candidate.email,
                        subject=f"You're hired - {job.title}",
                        heading="You got the job!",
                        body_html=(
                            f"Congratulations - <strong>{company.name}</strong> has decided to move forward "
                            f"with you for the <strong>{job.title}</strong> role!<br><br>"
                            f"Please confirm the hire on your dashboard so we can let {company.name} know. "
                            f"Best of luck in your new role!"
                        ),
                        plain_text=(
                            f"Congratulations - {company.name} has offered you the {job.title} role! "
                            f"Please confirm the hire on your dashboard."
                        ),
                        cta_label="Confirm My Hire",
                        cta_url=external_url('candidate.dashboard'),
                    )
                else:
                    send_branded_email(
                        to_email=candidate.email,
                        subject=copy["subject_tpl"].format(job_title=job.title),
                        heading=copy["heading"],
                        body_html=copy["body_tpl"].format(company=company.name, job_title=job.title),
                        plain_text=copy["body_tpl"].format(company=company.name, job_title=job.title).replace("<strong>", "").replace("</strong>", ""),
                        cta_label="View My Applications",
                        cta_url=external_url('candidate.dashboard'),
                    )
            except Exception as e:
                print(f"Status email send failed ({new_status}):", e)
        # For rejections, if the caller wants JSON (AJAX), return a feedback draft
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        if new_status == 'rejected' and status_changed and is_ajax:
            try:
                draft = generate_rejection_feedback(
                    job_title=job.title,
                    company_name=company.name,
                    match_score=application.match_score,
                    match_explanation=application.match_explanation or '',
                    interview_verdict=application.interview_verdict,
                    interview_transcript=application.interview_transcript or '',
                )
            except Exception:
                draft = ""
            return jsonify({
                "status": "ok",
                "application_id": application.id,
                "candidate_name": application.candidate.first_name,
                "feedback_draft": draft,
                "redirect": url_for('employer.applicants', job_id=job.id),
            })

        flash("Status updated.", "success")

    return redirect(url_for('employer.applicants', job_id=job.id))


@employer.route('/employer/applications/<int:application_id>/hire-evidence')
@login_required
def hire_evidence(application_id):
    """Serves the optional hire-salary evidence upload. Viewable by the
    employer who owns the job or the candidate the application belongs to -
    nobody else."""
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)

    user_company = get_user_company() if current_user.role == 'employer' else None
    is_owning_employer = user_company is not None and job.company_id == user_company.id
    is_owning_candidate = current_user.id == application.candidate_user_id
    if not (is_owning_employer or is_owning_candidate):
        abort(403)
    if not application.hire_evidence_filename:
        abort(404)
    return send_from_directory(_hire_evidence_dir(), application.hire_evidence_filename)


@employer.route('/employer/applications/<int:application_id>/send-feedback', methods=['POST'])
@login_required
@employer_required
def send_candidate_feedback(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    feedback_text = (request.get_json() or {}).get('feedback', '').strip()
    if not feedback_text:
        return jsonify({"error": "No feedback provided"}), 400

    application.candidate_feedback = feedback_text
    db.session.commit()

    # Notify candidate
    candidate = application.candidate
    if candidate.notifications_enabled:
        notif = Notification(
            user_id=candidate.id,
            message=f"You received feedback on your {job.title} application from {company.name}.",
            link=url_for('candidate.dashboard', _external=False),
        )
        db.session.add(notif)
        db.session.commit()

    try:
        send_branded_email(
            to_email=candidate.email,
            subject=f"Feedback on your {job.title} application",
            heading="Application feedback",
            body_html=(
                f"<strong>{company.name}</strong> wanted to share some feedback on your "
                f"application for <strong>{job.title}</strong>:<br><br>"
                f"<blockquote style='border-left:3px solid #6366f1;padding-left:14px;color:#374151;'>"
                f"{feedback_text}</blockquote>"
            ),
            plain_text=f"Feedback from {company.name} on your {job.title} application:\n\n{feedback_text}",
            cta_label="View My Applications",
            cta_url=external_url('candidate.dashboard'),
        )
    except Exception as e:
        print("Feedback email failed:", e)

    return jsonify({"status": "sent"})


@employer.route('/employer/jobs/analyze-jd', methods=['POST'])
@login_required
@employer_required
@limiter.limit("15 per hour")
def analyze_jd():
    data = request.get_json() or {}
    try:
        result = analyze_job_description(
            title=data.get('title', ''),
            about=data.get('about', ''),
            requirements=data.get('requirements', ''),
            preferred=data.get('preferred', ''),
            what_you_do=data.get('what_you_do', ''),
            contract_terms=data.get('contract_terms', ''),
            salary_provided=bool(data.get('salary_usd')),
            category=data.get('category', ''),
        )
        return jsonify(result)
    except Exception as e:
        print("JD analysis failed:", e)
        return jsonify({"error": "Analysis failed"}), 500


@employer.route('/employer/talent')
@login_required
@employer_required
def talent_pool():
    import re as _re
    company = get_owned_company()
    open_jobs = Job.query.filter_by(company_id=company.id, status='open').all()

    # Build keyword set from employer's open jobs
    job_corpus = ' '.join(
        f"{j.title} {j.category} {(j.description_text or '')[:300]}"
        for j in open_jobs
    ).lower()
    job_words = set(_re.findall(r'\b\w{3,}\b', job_corpus))

    # Eagerly load users to avoid N+1 queries
    from sqlalchemy.orm import joinedload
    profiles = (
        CandidateProfile.query
        .filter_by(open_to_work=True)
        .options(joinedload(CandidateProfile.user))
        .all()
    )

    def _score(prof):
        text = ' '.join(filter(None, [
            prof.skills, prof.target_roles, prof.headline
        ])).lower()
        words = set(_re.findall(r'\b\w{3,}\b', text))
        if not words or not job_words:
            return 0
        return round(len(words & job_words) / len(words) * 100)

    all_scored = sorted(
        [{"profile": p, "user": p.user, "score": _score(p)} for p in profiles if p.user],
        key=lambda x: x["score"],
        reverse=True,
    )
    suggested = [x for x in all_scored if x["score"] > 0]

    return render_template(
        "talent_pool.html",
        all_scored=all_scored,
        suggested=suggested,
        company=company,
        open_jobs=open_jobs,
    )


@employer.route('/employer/analytics')
@login_required
@employer_required
def analytics():
    company = get_owned_company()
    jobs = Job.query.filter_by(company_id=company.id).all()
    job_ids = [j.id for j in jobs]

    if not job_ids:
        return render_template('employer_analytics.html', company=company, stats={}, top_jobs=[])

    all_apps = Application.query.filter(Application.job_id.in_(job_ids)).all()

    total = len(all_apps)
    interviewed = sum(1 for a in all_apps if a.interview_transcript)
    hired = sum(1 for a in all_apps if a.status == 'hired')
    rejected = sum(1 for a in all_apps if a.status == 'rejected')
    scores = [a.match_score for a in all_apps if a.match_score is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else None
    interview_rate = round(interviewed / total * 100) if total else 0
    hire_rate = round(hired / total * 100) if total else 0

    # Per-job breakdown
    jobs_by_id = {j.id: j for j in jobs}
    job_counts = {}
    for a in all_apps:
        jid = a.job_id
        if jid not in job_counts:
            job_counts[jid] = {"title": jobs_by_id[jid].title, "total": 0, "interviewed": 0, "hired": 0}
        job_counts[jid]["total"] += 1
        if a.interview_transcript:
            job_counts[jid]["interviewed"] += 1
        if a.status == "hired":
            job_counts[jid]["hired"] += 1

    top_jobs = sorted(job_counts.values(), key=lambda x: x["total"], reverse=True)[:5]

    stats = {
        "total": total,
        "interviewed": interviewed,
        "hired": hired,
        "rejected": rejected,
        "avg_score": avg_score,
        "interview_rate": interview_rate,
        "hire_rate": hire_rate,
        "open_jobs": sum(1 for j in jobs if j.status == 'open'),
    }

    return render_template('employer_analytics.html', company=company, stats=stats, top_jobs=top_jobs)


@employer.route('/employer/applications/<int:application_id>/notes', methods=['POST'])
@login_required
@employer_required
def save_notes(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)
    application.employer_notes = request.get_json().get('notes', '').strip() or None
    db.session.commit()
    return jsonify({"status": "saved"})


# ------------------------------------------------------------------ #
#  Company Settings
# ------------------------------------------------------------------ #

@employer.route('/employer/company/settings', methods=['GET', 'POST'])
@login_required
@employer_required
def company_settings():
    company = get_owned_company()

    if request.method == 'POST':
        action = request.form.get('action', 'save_profile')

        if action == 'save_profile':
            name = request.form.get('name', '').strip()
            if not name:
                flash("Company name is required.", "error")
            else:
                company.name = name
                company.website = request.form.get('website', '').strip() or None
                company.description = request.form.get('description', '').strip() or None
                company.industry = request.form.get('industry', '').strip() or None
                company.size = request.form.get('size', '').strip() or None
                company.location = request.form.get('location', '').strip() or None
                company.blind_mode_enabled = request.form.get('blind_mode_enabled') == 'on'
                db.session.commit()
                flash("Company profile updated.", "success")

        elif action == 'remove_logo':
            if company.logo_filename:
                logos_dir = os.path.join(os.path.dirname(__file__), "static", "logos")
                filepath = os.path.join(logos_dir, company.logo_filename)
                try:
                    os.remove(filepath)
                except FileNotFoundError:
                    pass
                company.logo_filename = None
                company.logo_version = (company.logo_version or 0) + 1
                db.session.commit()
            flash("Logo removed.", "success")

        elif action == 'remove_member':
            member_id = request.form.get('member_id', type=int)
            if company.owner_user_id != current_user.id:
                flash("Only the company owner can remove team members.", "error")
            elif member_id:
                member = CompanyMember.query.filter_by(id=member_id, company_id=company.id).first()
                if member:
                    db.session.delete(member)
                    db.session.commit()
                    flash("Team member removed.", "success")

        return redirect(url_for('employer.company_settings'))

    members = CompanyMember.query.filter_by(company_id=company.id).all()
    pending_invites = CompanyInvite.query.filter_by(company_id=company.id, used=False).all()
    open_jobs_count = Job.query.filter_by(company_id=company.id, status='open').count()

    return render_template(
        "company_settings.html",
        company=company,
        members=members,
        pending_invites=pending_invites,
        sizes=COMPANY_SIZES,
        industries=INDUSTRIES,
        is_owner=(company.owner_user_id == current_user.id),
        open_jobs_count=open_jobs_count,
    )


def _detect_image_format(path):
    """Return detected format from magic bytes, or None if unrecognised."""
    try:
        with open(path, 'rb') as f:
            h = f.read(16)
        if h[:8] == b'\x89PNG\r\n\x1a\n':
            return 'PNG'
        if h[:3] == b'\xff\xd8\xff':
            return 'JPEG'
        if h[:6] in (b'GIF87a', b'GIF89a'):
            return 'GIF'
        if h[:4] == b'RIFF' and h[8:12] == b'WEBP':
            return 'WEBP'
        if h[:2] == b'BM':
            return 'BMP'
        if h[:4] in (b'II*\x00', b'MM\x00*'):
            return 'TIFF'
        # HEIC/HEIF: 'ftyp' box appears at offset 4
        if h[4:8] == b'ftyp':
            return 'HEIF'
        if h[:4] == b'\x00\x00\x00\x0c' and h[8:12] in (b'avif', b'avis'):
            return 'AVIF'
    except OSError:
        pass
    return None


@employer.route('/employer/company/logo', methods=['POST'])
@login_required
@employer_required
def upload_logo():
    company = get_owned_company()
    file = request.files.get('logo')

    if not file or not file.filename:
        flash("No file selected.", "error")
        return redirect(url_for('employer.company_settings'))

    ext = (file.filename.rsplit('.', 1)[-1] if '.' in file.filename else '').lower()
    if ext not in LOGO_ALLOWED_EXTENSIONS:
        flash("Unsupported file type. Use PNG, JPG, GIF, or WebP.", "error")
        return redirect(url_for('employer.company_settings'))

    logos_dir = os.path.join(os.path.dirname(__file__), "static", "logos")
    os.makedirs(logos_dir, exist_ok=True)

    tmp_path = os.path.join(logos_dir, f"_tmp_{company.id}.bin")
    final_path = os.path.join(logos_dir, f"company_{company.id}.png")

    try:
        file.save(tmp_path)

        # Guard: empty file
        if os.path.getsize(tmp_path) == 0:
            flash("The uploaded file was empty. Please try again.", "error")
            return redirect(url_for('employer.company_settings'))

        raw = Image.open(tmp_path)
        raw.load()  # force full decode so errors surface here, not later

        if raw.mode == 'RGBA':
            bg = Image.new("RGB", raw.size, (255, 255, 255))
            bg.paste(raw, mask=raw.split()[3])
            img = bg
        elif raw.mode == 'P':
            converted = raw.convert("RGBA")
            bg = Image.new("RGB", converted.size, (255, 255, 255))
            bg.paste(converted, mask=converted.split()[3])
            img = bg
        else:
            img = raw.convert("RGB")

        img.thumbnail(LOGO_MAX_SIZE, Image.LANCZOS)
        img.save(final_path, "PNG", optimize=True)
        company.logo_filename = f"company_{company.id}.png"
        company.logo_version = (company.logo_version or 0) + 1
        db.session.commit()
        flash("Logo updated.", "success")
    except Exception as e:
        flash(f"Could not process image: {e}", "error")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return redirect(url_for('employer.company_settings'))


# ------------------------------------------------------------------ #
#  Team Invitations
# ------------------------------------------------------------------ #

@employer.route('/employer/company/invite', methods=['POST'])
@login_required
@employer_required
def invite_member():
    company = get_owned_company()

    if company.owner_user_id != current_user.id:
        flash("Only the company owner can send invitations.", "error")
        return redirect(url_for('employer.company_settings'))

    email = request.form.get('email', '').strip().lower()
    if not email or '@' not in email:
        flash("A valid email address is required.", "error")
        return redirect(url_for('employer.company_settings'))

    from .models import FREE_EMAIL_DOMAINS, User
    domain = email.split('@')[-1]
    if domain in FREE_EMAIL_DOMAINS:
        flash("Please invite using a company email address - personal email providers like Gmail aren't accepted.", "error")
        return redirect(url_for('employer.company_settings'))

    owner = User.query.get(company.owner_user_id)
    owner_domain = owner.email.split('@')[-1].lower() if owner and owner.email else None
    if owner_domain and domain != owner_domain:
        flash(f"You can only invite teammates with an @{owner_domain} email address.", "error")
        return redirect(url_for('employer.company_settings'))

    # Don't invite someone already a member or the owner
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        if existing_user.id == company.owner_user_id:
            flash("That person is already the company owner.", "error")
            return redirect(url_for('employer.company_settings'))
        already = CompanyMember.query.filter_by(company_id=company.id, user_id=existing_user.id).first()
        if already:
            flash("That person is already a team member.", "error")
            return redirect(url_for('employer.company_settings'))

    # Delete any previous unused invite for this email + company
    CompanyInvite.query.filter_by(company_id=company.id, email=email, used=False).delete()
    db.session.commit()

    token = secrets.token_urlsafe(32)
    invite = CompanyInvite(
        company_id=company.id,
        invited_by_user_id=current_user.id,
        email=email,
        token=token,
    )
    db.session.add(invite)
    db.session.commit()

    invite_url = external_url('employer.accept_invite', token=token)
    try:
        send_branded_email(
            to_email=email,
            subject=f"You've been invited to join {company.name} on Avants AI",
            heading=f"Join {company.name} on Avants AI",
            body_html=(
                f"<strong>{current_user.first_name or current_user.email}</strong> has invited you to join "
                f"<strong>{company.name}</strong> as a team member on Avants AI. "
                f"As a team member, you'll be able to post jobs and manage applicants for {company.name}.<br><br>"
                f"This invite expires if not used."
            ),
            plain_text=(
                f"{current_user.first_name or current_user.email} has invited you to join {company.name} on Avants AI.\n\n"
                f"Accept the invite here: {invite_url}"
            ),
            cta_label="Accept Invitation",
            cta_url=invite_url,
        )
        flash(f"Invitation sent to {email}.", "success")
    except Exception as e:
        print("Invite email failed:", e)
        flash(f"Invite created but email delivery failed. Share this link manually: {invite_url}", "error")

    return redirect(url_for('employer.company_settings'))


@employer.route('/employer/join/<token>')
@login_required
def accept_invite(token):
    invite = CompanyInvite.query.filter_by(token=token, used=False).first()

    if not invite:
        flash("This invitation is invalid or has already been used.", "error")
        return redirect(url_for('views.home'))

    if current_user.role != 'employer':
        flash("You need an employer account to accept this invitation.", "error")
        return redirect(url_for('views.home'))

    if invite.email.lower() != current_user.email.lower():
        flash("This invitation was sent to a different email address.", "error")
        return redirect(url_for('views.home'))

    company = Company.query.get_or_404(invite.company_id)

    # Check if already a member or owner
    if company.owner_user_id == current_user.id:
        flash("You are already the owner of this company.", "info")
        return redirect(url_for('employer.dashboard'))

    existing = CompanyMember.query.filter_by(company_id=company.id, user_id=current_user.id).first()
    if existing:
        flash(f"You're already a team member of {company.name}.", "info")
        return redirect(url_for('employer.dashboard'))

    member = CompanyMember(company_id=company.id, user_id=current_user.id)
    db.session.add(member)
    invite.used = True
    db.session.commit()

    flash(f"Welcome to {company.name}! You can now post jobs and manage applicants.", "success")
    return redirect(url_for('employer.dashboard'))


# ------------------------------------------------------------------ #
#  Public Company Page
# ------------------------------------------------------------------ #

# ------------------------------------------------------------------ #
#  Meeting Scheduling - Propose Times
# ------------------------------------------------------------------ #

@employer.route('/employer/applications/<int:application_id>/propose-times', methods=['POST'])
@login_required
@employer_required
def propose_times(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    notes = request.form.get('notes', '').strip()
    slots = []
    for i in range(1, 4):
        d = request.form.get(f'date_{i}', '').strip()
        t = request.form.get(f'time_{i}', '').strip()
        if d and t:
            slots.append({'date': d, 'time': t})

    if not slots:
        flash("Add at least one time slot.", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    # Cancel any existing pending proposal for this application
    MeetingProposal.query.filter_by(
        application_id=application_id, status='pending'
    ).update({'status': 'expired'})

    proposal = MeetingProposal(
        application_id=application_id,
        employer_user_id=current_user.id,
        slots=json.dumps(slots),
        notes=notes or None,
    )
    db.session.add(proposal)

    candidate = application.candidate
    notif = Notification(
        user_id=candidate.id,
        message=f"{company.name} wants to schedule an interview for {job.title} - pick a time!",
        link=url_for('candidate.interviews'),
    )
    db.session.add(notif)
    db.session.commit()

    try:
        slot_lines = ''.join(
            f"<li><strong>{s['date']}</strong> at {s['time']}</li>" for s in slots
        )
        send_branded_email(
            to_email=candidate.email,
            subject=f"{company.name} wants to schedule an interview - {job.title}",
            heading=f"{company.name} wants to meet you!",
            body_html=(
                f"Hi {candidate.first_name or 'there'},<br><br>"
                f"<strong>{company.name}</strong> would like to schedule a live video interview "
                f"for <strong>{job.title}</strong>. Pick whichever time works best for you:<br><br>"
                f"<ul>{slot_lines}</ul>"
                + (f"<br><strong>Notes:</strong> {notes}<br>" if notes else "")
                + "<br>Click below to choose your slot - the interview room will be created instantly."
            ),
            plain_text=(
                f"Hi {candidate.first_name or 'there'},\n\n"
                f"{company.name} wants to interview you for {job.title}.\n\n"
                "Available times:\n"
                + ''.join(f"  • {s['date']} at {s['time']}\n" for s in slots)
                + (f"\nNotes: {notes}\n" if notes else "")
            ),
            cta_label="Pick a Time",
            cta_url=external_url('candidate.interviews'),
        )
        flash(f"Time slots sent to {candidate.first_name or candidate.email}.", "success")
    except Exception:
        flash("Proposal saved - email notification failed.", "error")

    return redirect(url_for('employer.applicants', job_id=job.id))


# ------------------------------------------------------------------ #
#  In-App Messaging
# ------------------------------------------------------------------ #

@employer.route('/employer/applications/<int:application_id>/messages.json')
@login_required
@employer_required
def messages_json(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    # Mark unread messages (from candidate) as read
    Message.query.filter_by(application_id=application_id, read=False).filter(
        Message.sender_user_id != current_user.id
    ).update({"read": True})
    db.session.commit()

    msgs = Message.query.filter_by(application_id=application_id).order_by(Message.created_at).all()
    return jsonify([{
        "id": m.id,
        "body": m.body,
        "sender_name": m.sender.first_name or m.sender.email,
        "is_mine": m.sender_user_id == current_user.id,
        "time": m.created_at.strftime("%b %d, %H:%M") if m.created_at else "",
    } for m in msgs])


@employer.route('/employer/applications/<int:application_id>/message', methods=['POST'])
@login_required
@employer_required
def send_message(application_id):
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    body = (request.json or {}).get('body', '').strip() if request.is_json else request.form.get('body', '').strip()
    if not body:
        return jsonify({"error": "Message body is required"}), 400

    msg = Message(application_id=application_id, sender_user_id=current_user.id, body=body)
    db.session.add(msg)

    # Notify candidate
    candidate = application.candidate
    notif = Notification(
        user_id=candidate.id,
        message=f"New message from {company.name} about your {job.title} application.",
        link=url_for('candidate.dashboard'),
    )
    db.session.add(notif)
    db.session.commit()

    return jsonify({
        "id": msg.id,
        "body": msg.body,
        "sender_name": current_user.first_name or current_user.email,
        "is_mine": True,
        "time": msg.created_at.strftime("%b %d, %H:%M") if msg.created_at else "",
    })




# ------------------------------------------------------------------ #
#  In-App Video Meetings (Daily.co)
# ------------------------------------------------------------------ #

@employer.route('/employer/applications/<int:application_id>/meeting/create', methods=['POST'])
@login_required
@employer_required
def create_meeting_room(application_id):
    from daily_utils import create_room
    company = get_owned_company()
    application = Application.query.get_or_404(application_id)
    job = Job.query.get_or_404(application.job_id)
    if job.company_id != company.id:
        abort(403)

    date = request.form.get('date', '').strip()
    time_ = request.form.get('time', '').strip()
    notes = request.form.get('notes', '').strip()

    if not date or not time_:
        flash("Date and time are required.", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    api_key = current_app.config.get('DAILY_API_KEY')
    if not api_key:
        flash("Daily.co API key is not configured.", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    try:
        room_data = create_room(api_key)
    except Exception as e:
        logger.exception("Daily.co room creation failed")
        flash(f"Could not create meeting room: {e}", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    room = MeetingRoom(
        application_id=application_id,
        employer_user_id=current_user.id,
        room_name=room_data['name'],
        room_url=room_data['url'],
        scheduled_date=date,
        scheduled_time=time_,
        notes=notes or None,
    )
    db.session.add(room)

    join_url = external_url('views.join_meeting', room_name=room_data['name'])
    application.scheduled_meeting = json.dumps({
        "type": "virtual",
        "date": date,
        "time": time_,
        "location_or_url": join_url,
        "notes": notes,
        "in_app": True,
    })
    db.session.commit()

    candidate = application.candidate

    # In-app notification (shows in the bell)
    notif = Notification(
        user_id=candidate.id,
        message=f"{company.name} wants to meet you! Interview scheduled for {job.title} on {date} at {time_}.",
        link=url_for('candidate.interviews'),
    )
    db.session.add(notif)
    db.session.commit()

    # Email
    try:
        send_branded_email(
            to_email=candidate.email,
            subject=f"🎉 {company.name} wants to interview you - {job.title}",
            heading=f"{company.name} wants to meet you!",
            body_html=(
                f"Hi {candidate.first_name or 'there'},<br><br>"
                f"Great news - <strong>{company.name}</strong> has scheduled a live video interview "
                f"with you for <strong>{job.title}</strong>.<br><br>"
                f"<strong>📅 Date:</strong> {date}<br>"
                f"<strong>🕐 Time:</strong> {time_}<br>"
                + (f"<strong>📝 Notes:</strong> {notes}<br>" if notes else "")
                + "<br>The interview takes place directly on Avants - no Zoom needed. "
                "Click the button below when it's time."
            ),
            plain_text=(
                f"Hi {candidate.first_name or 'there'},\n\n"
                f"{company.name} has scheduled a live interview for {job.title}.\n\n"
                f"Date: {date}\nTime: {time_}\n"
                + (f"Notes: {notes}\n" if notes else "")
                + f"\nJoin here: {join_url}\n"
            ),
            cta_label="Join Interview",
            cta_url=join_url,
        )
        flash("Interview room created - candidate notified by email and in-app.", "success")
    except Exception:
        flash("Interview room created and candidate notified in-app (email failed).", "error")

    return redirect(url_for('employer.applicants', job_id=job.id))


@employer.route('/employer/meetings')
@login_required
@employer_required
def employer_meetings():
    company = get_owned_company()
    jobs = Job.query.filter_by(company_id=company.id).all()
    job_ids = [j.id for j in jobs]
    job_map = {j.id: j for j in jobs}

    rooms = (
        MeetingRoom.query
        .join(Application, Application.id == MeetingRoom.application_id)
        .filter(Application.job_id.in_(job_ids))
        .order_by(MeetingRoom.created_at.desc())
        .all()
    )

    candidate_map = {}
    for room in rooms:
        app = room.application
        if app.candidate_user_id not in candidate_map:
            candidate_map[app.candidate_user_id] = app.candidate

    from datetime import date as _date
    return render_template(
        'employer_meetings.html',
        company=company,
        rooms=rooms,
        job_map=job_map,
        candidate_map=candidate_map,
        now_date=_date.today().isoformat(),
    )


@employer.route('/employer/meetings/<int:room_id>/recordings')
@login_required
@employer_required
def room_recordings(room_id):
    from daily_utils import get_recordings
    company = get_owned_company()
    room = MeetingRoom.query.get_or_404(room_id)
    job = Job.query.get_or_404(room.application.job_id)
    if job.company_id != company.id:
        abort(403)

    api_key = current_app.config.get('DAILY_API_KEY')
    if not api_key:
        return jsonify({"error": "Daily.co not configured"}), 503
    try:
        recordings = get_recordings(api_key, room.room_name)
        return jsonify(recordings)
    except Exception as e:
        logger.exception("Failed to fetch recordings for room %s", room.room_name)
        return jsonify({"error": str(e)}), 502


@employer.route('/employer/meetings/<int:room_id>/recording/<recording_id>/link')
@login_required
@employer_required
def recording_link(room_id, recording_id):
    from daily_utils import get_recording_link
    company = get_owned_company()
    room = MeetingRoom.query.get_or_404(room_id)
    job = Job.query.get_or_404(room.application.job_id)
    if job.company_id != company.id:
        abort(403)

    api_key = current_app.config.get('DAILY_API_KEY')
    if not api_key:
        return jsonify({"error": "Daily.co not configured"}), 503
    try:
        link = get_recording_link(api_key, recording_id)
        return jsonify({"download_link": link})
    except Exception as e:
        logger.exception("Failed to get recording link %s", recording_id)
        return jsonify({"error": str(e)}), 502


@employer.route('/companies/<int:company_id>')
def company_page(company_id):
    company = Company.query.get_or_404(company_id)
    open_jobs = Job.query.filter_by(company_id=company.id, status='open').order_by(Job.created_at.desc()).all()
    total_hires = Application.query.join(Job).filter(
        Job.company_id == company.id,
        Application.status == 'hired',
    ).count()
    member_count = CompanyMember.query.filter_by(company_id=company.id).count() + 1  # +1 for owner
    return render_template(
        "company_page.html",
        company=company,
        open_jobs=open_jobs,
        total_hires=total_hires,
        member_count=member_count,
    )
