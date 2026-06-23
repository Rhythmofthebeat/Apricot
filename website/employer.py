import json

from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, jsonify

MAX_INTERVIEW_QUESTIONS = 15


def _parse_interview_questions(raw):
    """Employer-edited question list comes in as a JSON array of strings from
    the post-job form. Falls back to None (no curated list) on bad input
    rather than failing the whole job save."""
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
from flask_login import login_required, current_user
from functools import wraps
from . import db
from .models import Company, Job, Application, JOB_CATEGORIES

from ai_services import generate_interview_question_preview
from email_utils import send_branded_email

employer = Blueprint('employer', __name__)

INTERVIEW_LENGTH_OPTIONS = ["short", "standard", "thorough"]

STATUS_EMAIL_COPY = {
    "interviewing": {
        "subject_tpl": "Your application is moving forward — {job_title}",
        "heading": "You're being considered for the role",
        "body_tpl": (
            "Good news — {company} has moved your application for <strong>{job_title}</strong> "
            "into the interviewing stage. They'll be in touch with next steps."
        ),
    },
    "hired": {
        "subject_tpl": "You're hired — {job_title}",
        "heading": "Congratulations!",
        "body_tpl": (
            "{company} has decided to move forward with hiring you for the <strong>{job_title}</strong> "
            "position. They'll be in touch with next steps."
        ),
    },
    "rejected": {
        "subject_tpl": "Update on your application — {job_title}",
        "heading": "Application update",
        "body_tpl": (
            "Thank you for applying to <strong>{job_title}</strong> at {company}. After review, they've "
            "decided not to move forward with your application at this time. We encourage you to keep "
            "applying to other roles on the platform."
        ),
    },
}


def employer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user.role != 'employer':
            abort(403)
        if not Company.query.filter_by(owner_user_id=current_user.id).first():
            return redirect(url_for('auth.complete_employer_profile'))
        return view(*args, **kwargs)
    return wrapped


def get_owned_company():
    company = Company.query.filter_by(owner_user_id=current_user.id).first()
    if not company:
        abort(403)
    return company


def summarize_explanation(explanation, max_len=140):
    """Condense a full match-explanation into a one-line teaser for the
    applicant card, preferring the resume's strengths."""
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


@employer.route('/employer/dashboard')
@login_required
@employer_required
def dashboard():
    company = get_owned_company()
    jobs = Job.query.filter_by(company_id=company.id).order_by(Job.created_at.desc()).all()
    applicant_counts = {
        job.id: Application.query.filter_by(job_id=job.id).count() for job in jobs
    }
    return render_template(
        "employer_dashboard.html",
        company=company,
        jobs=jobs,
        applicant_counts=applicant_counts,
    )


@employer.route('/employer/jobs/new', methods=['GET', 'POST'])
@login_required
@employer_required
def post_job():
    company = get_owned_company()

    if not current_user.email_verified:
        flash("Verify your company email before posting jobs.", "error")
        return redirect(url_for('employer.dashboard'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        location = request.form.get('location', '').strip()
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
                "post_job.html", categories=JOB_CATEGORIES, interview_lengths=INTERVIEW_LENGTH_OPTIONS,
                interview_questions=json.loads(interview_questions) if interview_questions else [],
            )

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
        )
        db.session.add(job)
        db.session.commit()
        flash("Job posted!", "success")
        return redirect(url_for('employer.dashboard'))

    return render_template("post_job.html", categories=JOB_CATEGORIES, interview_lengths=INTERVIEW_LENGTH_OPTIONS, interview_questions=[])


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
                "post_job.html", job=job, categories=JOB_CATEGORIES, interview_lengths=INTERVIEW_LENGTH_OPTIONS,
                interview_questions=_existing_interview_questions(job),
            )

        job.title = title
        job.description = description
        job.location = request.form.get('location', '').strip()
        job.employment_type = request.form.get('employment_type', '').strip()
        job.category = category
        job.interview_length = interview_length
        job.interview_focus = request.form.get('interview_focus', '').strip() or None
        job.interview_questions = _parse_interview_questions(request.form.get('interview_questions', ''))
        job.status = request.form.get('status', job.status)
        db.session.commit()
        flash("Job updated!", "success")
        return redirect(url_for('employer.dashboard'))

    return render_template(
        "post_job.html", job=job, categories=JOB_CATEGORIES, interview_lengths=INTERVIEW_LENGTH_OPTIONS,
        interview_questions=_existing_interview_questions(job),
    )


@employer.route('/employer/preview-interview-questions', methods=['POST'])
@login_required
@employer_required
def preview_interview_questions():
    """Generate sample questions for a job posting in progress — not tied to
    a real candidate or application, purely illustrative for the employer."""
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    interview_length = data.get('interview_length', 'standard')
    interview_focus = (data.get('interview_focus') or '').strip()

    if not title or not description:
        return jsonify({"error": "Add a job title and description first."}), 400

    try:
        questions = generate_interview_question_preview(title, description, interview_length, interview_focus)
        return jsonify({"questions": questions})
    except Exception:
        return jsonify({"error": "Failed to generate a preview. Please try again."}), 500


@employer.route('/employer/jobs/<int:job_id>/applicants')
@login_required
@employer_required
def applicants(job_id):
    company = get_owned_company()
    job = Job.query.get_or_404(job_id)
    if job.company_id != company.id:
        abort(403)

    applications = (
        Application.query.filter_by(job_id=job.id)
        .order_by(Application.match_score.desc())
        .all()
    )
    summaries = {app.id: summarize_explanation(app.match_explanation) for app in applications}
    audio_logs = {
        app.id: json.loads(app.interview_audio_log) if app.interview_audio_log else []
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
    return render_template(
        "job_applicants.html",
        job=job,
        applications=applications,
        summaries=summaries,
        audio_logs=audio_logs,
        work_auths=work_auths,
        meetings=meetings,
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

    meeting_type = request.form.get('meeting_type')  # 'virtual' | 'in_person'
    date = request.form.get('date', '').strip()
    time = request.form.get('time', '').strip()
    location_or_url = request.form.get('location_or_url', '').strip()
    notes = request.form.get('notes', '').strip()

    if meeting_type not in ('virtual', 'in_person') or not date or not time or not location_or_url:
        flash("Date, time, and a meeting link or address are required.", "error")
        return redirect(url_for('employer.applicants', job_id=job.id))

    meeting = {
        "type": meeting_type,
        "date": date,
        "time": time,
        "location_or_url": location_or_url,
        "notes": notes,
    }
    application.scheduled_meeting = json.dumps(meeting)
    db.session.commit()

    kind_label = "video call" if meeting_type == 'virtual' else "in-person meeting"
    where_label = f"Link: {location_or_url}" if meeting_type == 'virtual' else f"Location: {location_or_url}"
    candidate = application.candidate
    try:
        send_branded_email(
            to_email=candidate.email,
            subject=f"Meeting scheduled — {job.title} at {company.name}",
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
            cta_url=url_for('candidate.dashboard', _external=True) if meeting_type == 'virtual' else None,
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
        status_changed = application.status != new_status
        application.status = new_status
        db.session.commit()

        copy = STATUS_EMAIL_COPY.get(new_status)
        if status_changed and copy:
            candidate = application.candidate
            try:
                send_branded_email(
                    to_email=candidate.email,
                    subject=copy["subject_tpl"].format(job_title=job.title),
                    heading=copy["heading"],
                    body_html=copy["body_tpl"].format(company=company.name, job_title=job.title),
                    plain_text=copy["body_tpl"].format(company=company.name, job_title=job.title).replace("<strong>", "").replace("</strong>", ""),
                    cta_label="View My Applications",
                    cta_url=url_for('candidate.dashboard', _external=True),
                )
            except Exception as e:
                print(f"Status email send failed ({new_status}):", e)
        flash("Status updated.", "success")

    return redirect(url_for('employer.applicants', job_id=job.id))
