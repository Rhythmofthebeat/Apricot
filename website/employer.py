import json
import secrets

import stripe
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, jsonify, current_app
from flask_login import login_required, current_user
from functools import wraps

from . import db
from .models import (
    Company, Job, Application, Notification,
    CompanyMember, CompanyInvite,
    JOB_CATEGORIES, JOB_LOCATIONS, COMPANY_SIZES, INDUSTRIES,
)
from ai_services import generate_interview_question_preview
from email_utils import send_branded_email

employer = Blueprint('employer', __name__)

MAX_INTERVIEW_QUESTIONS = 15
PLATFORM_FEE_PERCENT = 12.5
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

def _create_stripe_payment_link(application, job, company, candidate):
    stripe.api_key = current_app.config.get("STRIPE_SECRET_KEY")
    if not stripe.api_key or not job.salary_usd:
        return None
    fee_cents = int(job.salary_usd * PLATFORM_FEE_PERCENT / 100 * 100)
    if fee_cents <= 0:
        return None
    try:
        product = stripe.Product.create(
            name=f"Avants AI Placement Fee — {job.title} at {company.name}",
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
                "post_job.html", categories=JOB_CATEGORIES, locations=JOB_LOCATIONS,
                interview_lengths=INTERVIEW_LENGTH_OPTIONS,
                interview_questions=json.loads(interview_questions) if interview_questions else [],
            )

        salary_raw = request.form.get('salary_usd', '').strip()
        salary_usd = int(salary_raw) if salary_raw.isdigit() else None

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
        )
        db.session.add(job)
        db.session.commit()
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
        job.title = title
        job.description = description
        job.location = request.form.get('location', '').strip()
        job.employment_type = request.form.get('employment_type', '').strip()
        job.category = category
        job.interview_length = interview_length
        job.interview_focus = request.form.get('interview_focus', '').strip() or None
        job.interview_questions = _parse_interview_questions(request.form.get('interview_questions', ''))
        job.status = request.form.get('status', job.status)
        job.salary_usd = int(salary_raw) if salary_raw.isdigit() else None
        db.session.commit()
        flash("Job updated!", "success")
        return redirect(url_for('employer.dashboard'))

    return render_template(
        "post_job.html", job=job, categories=JOB_CATEGORIES, locations=JOB_LOCATIONS,
        interview_lengths=INTERVIEW_LENGTH_OPTIONS,
        interview_questions=_existing_interview_questions(job),
    )


@employer.route('/employer/preview-interview-questions', methods=['POST'])
@login_required
@employer_required
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
    except Exception:
        return jsonify({"error": "Failed to generate a preview. Please try again."}), 500


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

            payment_link_url = None
            if new_status == 'hired' and not application.stripe_payment_link:
                payment_link_url = _create_stripe_payment_link(application, job, company, candidate)
                if payment_link_url:
                    application.stripe_payment_link = payment_link_url
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
                fee_amount = int(job.salary_usd * PLATFORM_FEE_PERCENT / 100) if job.salary_usd else None
                if new_status == 'hired' and payment_link_url:
                    body_html = (
                        f"Congratulations — <strong>{company.name}</strong> has offered you the <strong>{job.title}</strong> role!<br><br>"
                        f"As part of your placement through Avants AI, a <strong>${fee_amount:,} placement fee</strong> "
                        f"(12.5% of first-year compensation) is due within 30 days of your start date.<br><br>"
                        f"Click the button below to pay securely via Stripe."
                    )
                    plain_text = (
                        f"Congratulations — {company.name} has offered you the {job.title} role!\n\n"
                        f"Avants AI placement fee: ${fee_amount:,} (12.5% of first-year compensation).\n"
                        f"Pay here: {payment_link_url}"
                    )
                    send_branded_email(
                        to_email=candidate.email,
                        subject=f"You're hired — {job.title}",
                        heading="You got the job!",
                        body_html=body_html,
                        plain_text=plain_text,
                        cta_label="Pay Placement Fee",
                        cta_url=payment_link_url,
                    )
                elif new_status == 'hired':
                    body_html = (
                        f"Congratulations — <strong>{company.name}</strong> has offered you the <strong>{job.title}</strong> role!<br><br>"
                        f"As part of your placement through Avants AI, a placement fee of <strong>12.5% of your first-year compensation</strong> "
                        f"will be invoiced to you within 30 days of your start date. Our team will be in touch."
                    )
                    plain_text = (
                        f"Congratulations — {company.name} has offered you the {job.title} role!\n\n"
                        f"Avants AI placement fee (12.5% of first-year compensation) will be invoiced to you."
                    )
                    send_branded_email(
                        to_email=candidate.email,
                        subject=f"You're hired — {job.title}",
                        heading="You got the job!",
                        body_html=body_html,
                        plain_text=plain_text,
                        cta_label="View My Dashboard",
                        cta_url=url_for('candidate.dashboard', _external=True),
                    )
                else:
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
                db.session.commit()
                flash("Company profile updated.", "success")

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

    # Don't invite someone already a member or the owner
    from .models import User
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

    invite_url = url_for('employer.accept_invite', token=token, _external=True)
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
