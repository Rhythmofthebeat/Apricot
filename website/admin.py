import json
import io
import logging

import stripe
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, Response, current_app
from flask_login import login_required, current_user
from . import db
from .models import (
    EarnTask, TaskSubmission, User, EARN_DOMAINS, ContractorProfile, Company, Job, CompanyMember,
    Application, Feedback, FeedbackInsight, SavedJob, MeetingProposal, MeetingRoom, Message,
    EmployerSubscription, CompanyInvite, DeletedJob, Referral,
)
from .dataset_export import shape_submission
from ai_services import analyze_platform_feedback
from video_storage import delete_interview_videos

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin_bp', __name__)


def _require_admin():
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)


# ------------------------------------------------ #
# -------------------- DASHBOARD ----------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin')
@login_required
def dashboard():
    _require_admin()
    tasks = EarnTask.query.order_by(EarnTask.created_at.desc()).all()
    total_submissions = TaskSubmission.query.count()
    total_paid = db.session.query(db.func.sum(TaskSubmission.earnings_cents))\
        .filter_by(paid_out=False).scalar() or 0

    platform_stats = {
        "total_users": User.query.count(),
        "total_candidates": User.query.filter_by(role='candidate').count(),
        "total_employers": User.query.filter_by(role='employer').count(),
        "total_companies": Company.query.count(),
        "total_jobs": Job.query.count(),
        "total_applications": Application.query.count(),
        "total_interviews": Application.query.filter(Application.interview_verdict.isnot(None)).count(),
    }

    return render_template('admin_dashboard.html',
        tasks=tasks,
        total_submissions=total_submissions,
        total_pending_payout=total_paid,
        platform_stats=platform_stats,
    )


# ------------------------------------------------ #
# ------------------- COMPANIES ------------------ #
# ------------------------------------------------ #

@admin_bp.route('/admin/companies')
@login_required
def companies():
    _require_admin()
    all_companies = Company.query.order_by(Company.created_at.desc()).all()
    owner_ids = [c.owner_user_id for c in all_companies]
    owners = {u.id: u for u in User.query.filter(User.id.in_(owner_ids)).all()} if owner_ids else {}
    job_counts = dict(
        db.session.query(Job.company_id, db.func.count(Job.id)).group_by(Job.company_id).all()
    )
    member_counts = dict(
        db.session.query(CompanyMember.company_id, db.func.count(CompanyMember.id))
        .group_by(CompanyMember.company_id).all()
    )
    return render_template('admin_companies.html',
        companies=all_companies, owners=owners, job_counts=job_counts, member_counts=member_counts)


@admin_bp.route('/admin/companies/<int:company_id>')
@login_required
def company_detail(company_id):
    _require_admin()
    company = Company.query.get_or_404(company_id)
    owner = User.query.get(company.owner_user_id)
    members = (
        db.session.query(User, CompanyMember)
        .join(CompanyMember, CompanyMember.user_id == User.id)
        .filter(CompanyMember.company_id == company_id)
        .all()
    )
    jobs = Job.query.filter_by(company_id=company_id).order_by(Job.created_at.desc()).all()
    application_counts = dict(
        db.session.query(Application.job_id, db.func.count(Application.id))
        .filter(Application.job_id.in_([j.id for j in jobs]))
        .group_by(Application.job_id).all()
    ) if jobs else {}
    deleted_jobs = (
        DeletedJob.query
        .filter_by(company_id=company_id, restored_job_id=None)
        .order_by(DeletedJob.created_at.desc())
        .all()
    )

    # Flag any member whose email has another account under a different
    # case (e.g. the case-sensitivity bug fixed in auth.py) - lets the admin
    # spot and clean these up right from this page.
    duplicate_ids = set()
    for user, _ in members:
        if User.query.filter(db.func.lower(User.email) == user.email.lower(), User.id != user.id).first():
            duplicate_ids.add(user.id)

    return render_template('admin_company_detail.html',
        company=company, owner=owner, members=members, jobs=jobs, application_counts=application_counts,
        deleted_jobs=deleted_jobs, duplicate_ids=duplicate_ids)


def _cascade_delete_applications(application_ids):
    """Delete every row that hangs off a set of applications - meetings,
    messages, feedback, and stored interview videos - before the
    applications themselves."""
    if not application_ids:
        return
    for app_id in application_ids:
        delete_interview_videos(app_id)
    MeetingProposal.query.filter(MeetingProposal.application_id.in_(application_ids)).delete(synchronize_session=False)
    Message.query.filter(Message.application_id.in_(application_ids)).delete(synchronize_session=False)
    MeetingRoom.query.filter(MeetingRoom.application_id.in_(application_ids)).delete(synchronize_session=False)
    Feedback.query.filter(Feedback.application_id.in_(application_ids)).delete(synchronize_session=False)
    Application.query.filter(Application.id.in_(application_ids)).delete(synchronize_session=False)


def _cascade_delete_jobs(job_ids, source='admin'):
    """Snapshot each job into DeletedJob, then delete every application under
    it (and everything under those, via _cascade_delete_applications), then
    the job itself. Restoring a DeletedJob only recreates the job listing -
    the applications/messages/videos wiped out here are gone for good."""
    if not job_ids:
        return
    jobs = Job.query.filter(Job.id.in_(job_ids)).all()
    application_counts = dict(
        db.session.query(Application.job_id, db.func.count(Application.id))
        .filter(Application.job_id.in_(job_ids))
        .group_by(Application.job_id).all()
    )
    for job in jobs:
        db.session.add(DeletedJob(
            original_job_id=job.id,
            company_id=job.company_id,
            company_name=job.company.name,
            title=job.title,
            snapshot=json.dumps(job.to_snapshot_dict()),
            applicant_count=application_counts.get(job.id, 0),
            deleted_by_user_id=current_user.id,
            deleted_source=source,
        ))

    application_ids = [a.id for a in Application.query.filter(Application.job_id.in_(job_ids)).all()]
    _cascade_delete_applications(application_ids)
    SavedJob.query.filter(SavedJob.job_id.in_(job_ids)).delete(synchronize_session=False)
    Job.query.filter(Job.id.in_(job_ids)).delete(synchronize_session=False)


@admin_bp.route('/admin/jobs/<int:job_id>/delete', methods=['POST'])
@login_required
def delete_job(job_id):
    """Admin override of employer.delete_job() - that route refuses to delete
    a job once it has applicants (pushing the employer to close it instead,
    to preserve candidate history). This force-deletes it and everything
    under it: applications, meeting rooms, messages, feedback."""
    _require_admin()
    job = Job.query.get_or_404(job_id)
    company_id = job.company_id
    title = job.title
    applicant_count = Application.query.filter_by(job_id=job.id).count()

    _cascade_delete_jobs([job.id])
    db.session.commit()

    suffix = f" and {applicant_count} application{'s' if applicant_count != 1 else ''}" if applicant_count else ""
    flash(f'Deleted "{title}"{suffix}.', 'success')
    return redirect(url_for('admin_bp.company_detail', company_id=company_id))


@admin_bp.route('/admin/deleted-jobs/<int:history_id>/restore', methods=['POST'])
@login_required
def restore_job(history_id):
    """Recreates the job listing from its DeletedJob snapshot. Note this only
    brings back the listing itself - applications/messages/videos that were
    cascade-deleted alongside it are gone for good."""
    _require_admin()
    entry = DeletedJob.query.get_or_404(history_id)
    if entry.restored_job_id:
        flash("That job was already restored.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=entry.company_id))
    if not Company.query.get(entry.company_id):
        flash("Can't restore - the company this job belonged to was also deleted.", "error")
        return redirect(url_for('admin_bp.companies'))

    data = json.loads(entry.snapshot)
    job = Job(company_id=entry.company_id, **data)
    db.session.add(job)
    db.session.flush()
    entry.restored_job_id = job.id
    db.session.commit()
    flash(f'"{job.title}" restored.', 'success')
    return redirect(url_for('admin_bp.company_detail', company_id=entry.company_id))


@admin_bp.route('/admin/companies/<int:company_id>/members/<int:user_id>/remove', methods=['POST'])
@login_required
def remove_member(company_id, user_id):
    """Removes a CompanyMember (team member) from a company. Can't touch the
    owner this way - ownership isn't a CompanyMember row, it's Company.owner_user_id."""
    _require_admin()
    Company.query.get_or_404(company_id)
    member = CompanyMember.query.filter_by(company_id=company_id, user_id=user_id).first_or_404()
    db.session.delete(member)
    db.session.commit()
    flash("Member removed.", "success")
    return redirect(url_for('admin_bp.company_detail', company_id=company_id))


@admin_bp.route('/admin/users/<int:user_id>/delete-duplicate', methods=['POST'])
@login_required
def delete_duplicate_user(user_id):
    """Deletes a User row that is a confirmed duplicate of another account
    with the same email (case-insensitive) - e.g. left over from the
    case-sensitivity bug fixed in auth.py, where a mismatched-case lookup
    created a second, disconnected account instead of matching the first.
    Refuses unless there's a sibling account with the same email AND this
    row has zero data anywhere else in the app, so it can never silently
    discard someone's real applications, profile, or history."""
    _require_admin()
    user = User.query.get_or_404(user_id)

    sibling = User.query.filter(
        db.func.lower(User.email) == user.email.lower(), User.id != user.id
    ).first()
    if not sibling:
        flash(f"No other account shares {user.email} - not a confirmed duplicate, refusing to delete.", "error")
        return redirect(request.referrer or url_for('admin_bp.dashboard'))

    from .models import CandidateProfile

    blockers = []
    if Company.query.filter_by(owner_user_id=user.id).first():
        blockers.append("owns a company")
    if CandidateProfile.query.filter_by(user_id=user.id).first():
        blockers.append("has a candidate profile")
    if Application.query.filter_by(candidate_user_id=user.id).first():
        blockers.append("has applications")
    if SavedJob.query.filter_by(user_id=user.id).first():
        blockers.append("has saved jobs")
    if Referral.query.filter_by(referrer_user_id=user.id).first():
        blockers.append("has referrals made")
    if Referral.query.filter_by(referred_user_id=user.id).first():
        blockers.append("was referred by someone")
    if TaskSubmission.query.filter_by(user_id=user.id).first():
        blockers.append("has Earn task submissions")
    if ContractorProfile.query.filter_by(user_id=user.id).first():
        blockers.append("has a contractor profile")
    if MeetingProposal.query.filter_by(employer_user_id=user.id).first():
        blockers.append("has meeting proposals")
    if Message.query.filter_by(sender_user_id=user.id).first():
        blockers.append("has sent messages")
    if MeetingRoom.query.filter_by(employer_user_id=user.id).first():
        blockers.append("has meeting rooms")
    if CompanyInvite.query.filter_by(invited_by_user_id=user.id).first():
        blockers.append("sent invites")
    if DeletedJob.query.filter_by(deleted_by_user_id=user.id).first():
        blockers.append("deleted a job")
    if User.query.filter_by(referred_by_user_id=user.id).first():
        blockers.append("referred other users")

    if blockers:
        flash(f"Refusing to delete {user.email} - this account {', '.join(blockers)}. Not a clean duplicate.", "error")
        return redirect(request.referrer or url_for('admin_bp.dashboard'))

    CompanyMember.query.filter_by(user_id=user.id).delete()
    from .models import Notification
    Notification.query.filter_by(user_id=user.id).delete()
    email = user.email
    db.session.delete(user)
    db.session.commit()

    flash(f"Deleted duplicate account {email} (id={user_id}).", "success")
    return redirect(request.referrer or url_for('admin_bp.dashboard'))


@admin_bp.route('/admin/companies/<int:company_id>/members/add', methods=['POST'])
@login_required
def add_member(company_id):
    """Adds an existing user to a company as a team member, promoting them to
    role='employer' first if they signed up as a candidate. Exists because
    there's no self-serve way for a candidate account to become an employer
    once the email is already registered - e.g. someone at a company that
    already has a hub picked 'candidate' at signup by mistake."""
    _require_admin()
    company = Company.query.get_or_404(company_id)

    email = (request.form.get('email') or '').strip().lower()
    if not email:
        flash("Enter an email address.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user:
        flash(f"No account found for {email}.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    if user.id == company.owner_user_id:
        flash(f"{user.email} already owns this company.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    if CompanyMember.query.filter_by(company_id=company.id, user_id=user.id).first():
        flash(f"{user.email} is already a team member.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    if user.role != 'employer':
        user.role = 'employer'

    db.session.add(CompanyMember(company_id=company.id, user_id=user.id))
    CompanyInvite.query.filter_by(company_id=company.id, email=email, used=False).update({"used": True})
    db.session.commit()

    flash(f"{user.email} added to {company.name}.", "success")
    return redirect(url_for('admin_bp.company_detail', company_id=company_id))


@admin_bp.route('/admin/companies/<int:company_id>/delete', methods=['POST'])
@login_required
def delete_company(company_id):
    """Full cascade delete - every job, application, meeting, message,
    feedback entry, team membership, subscription, and pending invite under
    this company. The owner's User account itself is untouched (Company.
    owner_user_id points at User, not the reverse). Requires typing the
    exact company name to confirm, given the blast radius."""
    _require_admin()
    company = Company.query.get_or_404(company_id)

    confirm_name = (request.form.get('confirm_name') or '').strip()
    if confirm_name != company.name:
        flash("Company name didn't match what you typed - nothing was deleted.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    job_ids = [j.id for j in Job.query.filter_by(company_id=company.id).all()]
    _cascade_delete_jobs(job_ids)
    CompanyMember.query.filter_by(company_id=company.id).delete(synchronize_session=False)
    EmployerSubscription.query.filter_by(company_id=company.id).delete(synchronize_session=False)
    CompanyInvite.query.filter_by(company_id=company.id).delete(synchronize_session=False)

    name = company.name
    db.session.delete(company)
    db.session.commit()
    flash(f'Deleted company "{name}" and everything under it.', 'success')
    return redirect(url_for('admin_bp.companies'))


# ------------------------------------------------ #
# -------------------- FEEDBACK ------------------ #
# ------------------------------------------------ #

@admin_bp.route('/admin/feedback')
@login_required
def feedback():
    _require_admin()
    entries = Feedback.query.order_by(Feedback.created_at.desc()).all()
    avg_rating = round(sum(f.rating for f in entries) / len(entries), 1) if entries else None
    latest_insight = FeedbackInsight.query.order_by(FeedbackInsight.generated_at.desc()).first()
    recommendations = json.loads(latest_insight.recommendations) if latest_insight and latest_insight.recommendations else []
    return render_template('admin_feedback.html',
        entries=entries, avg_rating=avg_rating, insight=latest_insight, recommendations=recommendations)


@admin_bp.route('/admin/feedback/analyze', methods=['POST'])
@login_required
def analyze_feedback():
    _require_admin()
    entries = Feedback.query.order_by(Feedback.created_at.desc()).limit(200).all()
    if not entries:
        flash("No feedback to analyze yet.", "error")
        return redirect(url_for('admin_bp.feedback'))

    rows = [{"role": f.role, "context": f.context, "rating": f.rating, "comment": f.comment} for f in entries]
    try:
        result = analyze_platform_feedback(rows)
    except Exception as e:
        logger.exception("Feedback analysis failed")
        flash(f"AI analysis failed: {e}", "error")
        return redirect(url_for('admin_bp.feedback'))

    insight = FeedbackInsight(
        summary=result["summary"],
        recommendations=json.dumps(result["recommendations"]),
        feedback_count=len(entries),
    )
    db.session.add(insight)
    db.session.commit()
    flash("Feedback analyzed.", "success")
    return redirect(url_for('admin_bp.feedback'))


# ------------------------------------------------ #
# ------------------- CREATE TASK ---------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/new', methods=['GET', 'POST'])
@login_required
def create_task():
    _require_admin()
    if request.method == 'POST':
        task_type = request.form.get('task_type', 'ranking')
        selected_domains = request.form.getlist('required_domains')
        valid_domains = [d for d in selected_domains if d in EARN_DOMAINS]
        try:
            min_responses = max(1, int(request.form.get('min_responses', 3)))
        except (ValueError, TypeError):
            min_responses = 3
        task = EarnTask(
            title=request.form.get('title', '').strip(),
            description=request.form.get('description', '').strip(),
            task_type=task_type,
            domain=request.form.get('domain', '').strip() or None,
            required_domains=json.dumps(valid_domains) if valid_domains else None,
            min_responses=min_responses,
            pay_cents=int(float(request.form.get('pay_dollars', '5')) * 100),
            estimated_minutes=int(request.form.get('estimated_minutes', 5)),
            option_a=request.form.get('option_a', '').strip() or None,
            option_b=request.form.get('option_b', '').strip() or None,
            content=request.form.get('content', '').strip() or None,
            status='open',
        )
        db.session.add(task)
        db.session.commit()
        flash("Task created.", "success")
        return redirect(url_for('admin_bp.dashboard'))
    return render_template('admin_task_form.html', earn_domains=EARN_DOMAINS)


# ------------------------------------------------ #
# ------------------- CLOSE TASK ----------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>/close', methods=['POST'])
@login_required
def close_task(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    task.status = 'closed'
    db.session.commit()
    flash("Task closed.", "success")
    return redirect(url_for('admin_bp.dashboard'))


# ------------------------------------------------ #
# --------- EXPORT TRAINING DATA (JSONL) --------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>/export')
@login_required
def export_task(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    submissions = TaskSubmission.query.filter_by(task_id=task_id).all()

    lines = []
    for sub in submissions:
        record = shape_submission(task, sub)
        if record:
            lines.append(json.dumps(record))

    output = "\n".join(lines)
    filename = f"task_{task_id}_{task.task_type}.jsonl"
    return Response(
        output,
        mimetype="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ------------------------------------------------ #
# ------------- VIEW TASK SUBMISSIONS ------------ #
# ------------------------------------------------ #

@admin_bp.route('/admin/tasks/<int:task_id>')
@login_required
def task_submissions(task_id):
    _require_admin()
    task = EarnTask.query.get_or_404(task_id)
    submissions = TaskSubmission.query.filter_by(task_id=task_id)\
        .order_by(TaskSubmission.created_at.desc()).all()
    consensus = json.loads(task.consensus_answer) if task.consensus_answer else None
    return render_template('admin_task_submissions.html',
        task=task, submissions=submissions, consensus=consensus)


# ------------------------------------------------ #
# ------------------- PAYOUTS -------------------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/payouts')
@login_required
def payouts():
    _require_admin()
    rows = (
        db.session.query(User, ContractorProfile,
                         db.func.sum(TaskSubmission.earnings_cents).label('pending_cents'))
        .join(TaskSubmission, TaskSubmission.user_id == User.id)
        .outerjoin(ContractorProfile, ContractorProfile.user_id == User.id)
        .filter(TaskSubmission.paid_out == False)
        .group_by(User.id, ContractorProfile.id)
        .having(db.func.sum(TaskSubmission.earnings_cents) > 0)
        .all()
    )
    total_pending = sum(r.pending_cents for r in rows)
    return render_template('admin_payouts.html', rows=rows, total_pending=total_pending)


@admin_bp.route('/admin/payouts/pay/<int:user_id>', methods=['POST'])
@login_required
def pay_user(user_id):
    _require_admin()
    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        flash("Stripe is not configured.", "error")
        return redirect(url_for('admin_bp.payouts'))

    contractor = ContractorProfile.query.filter_by(user_id=user_id).first_or_404()
    if not contractor.stripe_account_id or not contractor.payout_enabled:
        flash("This worker hasn't completed Stripe payout setup.", "error")
        return redirect(url_for('admin_bp.payouts'))

    unpaid = TaskSubmission.query.filter_by(user_id=user_id, paid_out=False).all()
    amount = sum(s.earnings_cents for s in unpaid)

    if amount < 50:   # Stripe minimum is $0.50
        flash("Pending amount is below the $0.50 minimum transfer threshold.", "error")
        return redirect(url_for('admin_bp.payouts'))

    try:
        stripe.Transfer.create(
            amount=amount,
            currency='usd',
            destination=contractor.stripe_account_id,
            idempotency_key=f"payout_user_{user_id}_batch_{sum(s.id for s in unpaid)}",
        )
        for sub in unpaid:
            sub.paid_out = True
        db.session.commit()
        worker_name = contractor.user.first_name or contractor.user.email
        flash(f"Paid ${amount / 100:.2f} to {worker_name}.", "success")
    except stripe.error.StripeError as e:
        logger.exception("Stripe transfer failed for user %s", user_id)
        flash(f"Transfer failed: {e.user_message}", "error")

    return redirect(url_for('admin_bp.payouts'))


# ------------------------------------------------ #
# -------------------- REFERRALS ------------------ #
# ------------------------------------------------ #

@admin_bp.route('/admin/referrals')
@login_required
def referrals():
    _require_admin()
    rows = (
        Referral.query
        .filter_by(status='owed')
        .order_by(Referral.created_at.desc())
        .all()
    )
    paid_rows = (
        Referral.query
        .filter_by(status='paid')
        .order_by(Referral.paid_at.desc())
        .limit(50)
        .all()
    )
    total_owed = sum(r.commission_usd for r in rows)
    return render_template('admin_referrals.html', rows=rows, paid_rows=paid_rows, total_owed=total_owed)


@admin_bp.route('/admin/referrals/<int:referral_id>/mark-paid', methods=['POST'])
@login_required
def mark_referral_paid(referral_id):
    _require_admin()
    referral = Referral.query.get_or_404(referral_id)
    referral.status = 'paid'
    referral.paid_at = db.func.now()
    db.session.commit()
    flash(f"Marked ${referral.commission_usd:,} referral payout to {referral.referrer.first_name or referral.referrer.email} as paid.", "success")
    return redirect(url_for('admin_bp.referrals'))
