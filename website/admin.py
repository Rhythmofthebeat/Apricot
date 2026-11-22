import json
import io
import logging
from datetime import datetime, timezone

import stripe
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, Response, current_app
from flask_login import login_required, current_user
import credential_storage
from . import db
from .models import (
    EarnTask, TaskSubmission, User, EARN_DOMAINS, ContractorProfile, Company, Job, CompanyMember,
    Application, Feedback, FeedbackInsight, SavedJob, MeetingProposal, MeetingRoom, Message,
    EmployerSubscription, CompanyInvite, DeletedJob, Referral, ExpertCredential, DataRequest,
    ContractorPayout, BuyerCreditEntry, DataBuyer,
)
from .dataset_export import shape_submission, consented_user_ids
from .expert_verification import TIERS
from .pricing import TIER_LABELS, suggested_task_pay_cents, format_usd
from .payout_runner import build_overview, run_payout_batch, retry_payout, PayoutError
from .payouts import HOLD_DAYS
from .buyer_billing import parse_dollars, credit_balance
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


@admin_bp.route('/admin/companies/<int:company_id>/set-owner', methods=['POST'])
@login_required
def set_company_owner(company_id):
    """Transfers ownership of a company to a different existing user -
    admin-only, since ownership controls billing, who can invite/remove team
    members, and who signed the Employer Platform Agreement. There's no
    self-serve way to do this otherwise (owner_user_id is only ever set once,
    at company creation). The previous owner is kept on as a regular team
    member rather than losing access outright."""
    _require_admin()
    company = Company.query.get_or_404(company_id)

    email = (request.form.get('email') or '').strip().lower()
    if not email:
        flash("Enter an email address.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    new_owner = User.query.filter(db.func.lower(User.email) == email).first()
    if not new_owner:
        flash(f"No account found for {email}.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    if new_owner.id == company.owner_user_id:
        flash(f"{new_owner.email} already owns this company.", "error")
        return redirect(url_for('admin_bp.company_detail', company_id=company_id))

    old_owner_id = company.owner_user_id

    # The owner is tracked exclusively via Company.owner_user_id, never also
    # as a CompanyMember - drop any existing membership row for the incoming
    # owner so they're not double-counted.
    CompanyMember.query.filter_by(company_id=company.id, user_id=new_owner.id).delete()

    new_owner.role = 'employer'
    company.owner_user_id = new_owner.id

    if not CompanyMember.query.filter_by(company_id=company.id, user_id=old_owner_id).first():
        db.session.add(CompanyMember(company_id=company.id, user_id=old_owner_id))

    db.session.commit()
    flash(f"{new_owner.email} is now the owner of {company.name}. The previous owner was kept on as a team member.", "success")
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

def _task_form():
    return render_template('admin_task_form.html', earn_domains=EARN_DOMAINS, tiers=TIERS,
                           tier_labels=TIER_LABELS,
                           pay_hint={t: format_usd(suggested_task_pay_cents('ranking', t)) for t in TIERS})


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
        min_tier = request.form.get('min_tier', 'standard')
        if min_tier not in TIERS:
            min_tier = 'standard'
        is_assessment = bool(request.form.get('is_assessment'))
        gold_answer = request.form.get('gold_answer', '').strip() or None
        if is_assessment:
            valid_gold = {
                'ranking': {'A', 'B', 'tie'},
                'rating': {'1', '2', '3', '4', '5'},
                'factcheck': {'accurate', 'mostly', 'inaccurate'},
            }.get(task_type)
            if valid_gold is None or gold_answer not in valid_gold:
                flash("Assessment tasks need a known-correct answer that matches the task type "
                      "(ranking: A/B/tie, rating: 1-5, fact-check: accurate/mostly/inaccurate). "
                      "Writing tasks can't be graded automatically.", "error")
                return _task_form()
        else:
            gold_answer = None
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
            min_tier=min_tier,
            is_assessment=is_assessment,
            gold_answer=gold_answer,
            status='open',
        )
        db.session.add(task)
        db.session.commit()
        flash("Task created.", "success")
        return redirect(url_for('admin_bp.dashboard'))
    return _task_form()


# ------------------------------------------------ #
# ------------- EXPERT CREDENTIAL REVIEW --------- #
# ------------------------------------------------ #

@admin_bp.route('/admin/experts')
@login_required
def experts():
    _require_admin()
    pending = ExpertCredential.query.filter_by(status='pending') \
        .order_by(ExpertCredential.created_at.asc()).all()
    recent = ExpertCredential.query.filter(ExpertCredential.status != 'pending') \
        .order_by(ExpertCredential.reviewed_at.desc()).limit(30).all()
    return render_template('admin_experts.html', pending=pending, recent=recent)


@admin_bp.route('/admin/experts/credentials/<int:credential_id>/review', methods=['POST'])
@login_required
def review_credential(credential_id):
    _require_admin()
    cred = ExpertCredential.query.get_or_404(credential_id)
    decision = request.form.get('decision')
    if decision not in ('approve', 'reject'):
        abort(400)
    if cred.user_id == current_user.id:
        flash("You can't review your own credential.", "error")
        return redirect(url_for('admin_bp.experts'))

    note = (request.form.get('note') or '').strip()[:1000]
    if decision == 'reject' and not note:
        flash("Add a note so the contractor knows why it was rejected.", "error")
        return redirect(url_for('admin_bp.experts'))

    cred.status = 'approved' if decision == 'approve' else 'rejected'
    cred.reviewer_note = note or None
    cred.reviewed_by_user_id = current_user.id
    cred.reviewed_at = datetime.now(timezone.utc)
    # Data minimisation: the document has served its purpose. Only forget the
    # path once the delete actually succeeded, so a failure can be retried.
    if cred.evidence_path and credential_storage.delete_credential_file(cred.evidence_path):
        cred.evidence_path = None
    db.session.commit()
    flash(f"Credential {cred.status}.", "success")
    return redirect(url_for('admin_bp.experts'))


@admin_bp.route('/admin/experts/credentials/<int:credential_id>/document')
@login_required
def credential_document(credential_id):
    """Short-lived signed link to a pending credential's document. Sensitive,
    so each view is logged and nothing is cached or stored."""
    _require_admin()
    cred = ExpertCredential.query.get_or_404(credential_id)
    if not cred.evidence_path:
        abort(404)
    try:
        url = credential_storage.signed_credential_url(cred.evidence_path, expires_in=120)
    except Exception:
        logger.exception("Could not sign credential document %s", credential_id)
        flash("Couldn't open that document.", "error")
        return redirect(url_for('admin_bp.experts'))
    logger.info("Admin %s viewed credential document %s", current_user.id, credential_id)
    response = redirect(url)
    response.headers["Cache-Control"] = "no-store"
    return response


# ------------------------------------------------ #
# ----------- BUYER DATA / EXPERT REQUESTS ------- #
# ------------------------------------------------ #

REQUEST_STATUSES = ('submitted', 'quoted', 'accepted', 'in_progress', 'delivered', 'declined')


@admin_bp.route('/admin/data-requests')
@login_required
def data_requests():
    _require_admin()
    rows = DataRequest.query.order_by(DataRequest.created_at.desc()).all()
    buyers = DataBuyer.query.order_by(DataBuyer.name).all()
    balances = {b.id: credit_balance(b.credit_entries) for b in buyers}
    return render_template('admin_data_requests.html', requests=rows, statuses=REQUEST_STATUSES,
                           tier_labels=TIER_LABELS, buyers=buyers, balances=balances)


@admin_bp.route('/admin/data-requests/<int:request_id>/update', methods=['POST'])
@login_required
def update_data_request(request_id):
    _require_admin()
    req = DataRequest.query.get_or_404(request_id)
    status = request.form.get('status')
    if status not in REQUEST_STATUSES:
        abort(400)

    quote_raw = (request.form.get('quoted_dollars') or '').strip()
    if req.accepted_at:
        # The buyer accepted this price and the milestones were computed from it.
        if quote_raw:
            try:
                changed = parse_dollars(quote_raw) != req.quoted_cents
            except ValueError:
                changed = True
            if changed:
                flash("The buyer already accepted this quote, so the price is locked.", "error")
                return redirect(url_for('admin_bp.data_requests'))
        if status in ('submitted', 'quoted'):
            flash("The buyer already accepted this quote - it can't go back to an earlier stage.", "error")
            return redirect(url_for('admin_bp.data_requests'))
    elif status in ('accepted', 'in_progress', 'delivered'):
        flash("The buyer has to accept the quote first - that step is theirs, not yours.", "error")
        return redirect(url_for('admin_bp.data_requests'))
    if status in ('in_progress', 'delivered') and not req.deposit_paid_at:
        flash("Work can't start until the deposit is paid. If they wired it, record the wire as credit below - they can then pay the deposit from their balance.", "error")
        return redirect(url_for('admin_bp.data_requests'))
    if quote_raw:
        try:
            quoted = round(float(quote_raw) * 100)
            if quoted <= 0:
                raise ValueError
        except ValueError:
            flash("Enter the quote as a positive dollar amount.", "error")
            return redirect(url_for('admin_bp.data_requests'))
        req.quoted_cents = quoted
    if status == 'quoted' and not req.quoted_cents:
        flash("Set a quote amount before marking a request as quoted.", "error")
        return redirect(url_for('admin_bp.data_requests'))

    req.status = status
    req.admin_notes = (request.form.get('admin_notes') or '').strip()[:2000] or None
    db.session.commit()
    flash("Request updated.", "success")
    if status == 'declined' and req.deposit_paid_at:
        flash("A deposit was already paid on this request - refund it by recording a refund credit for the buyer.", "info")
    return redirect(url_for('admin_bp.data_requests'))


@admin_bp.route('/admin/buyers/credit', methods=['POST'])
@login_required
def record_buyer_credit():
    """Record money that moved outside Stripe Checkout (a wire/ACH received, a
    refund back to credits, or a correction). Goes into the same append-only
    ledger the buyer's balance is computed from."""
    _require_admin()
    try:
        buyer_id = int(request.form.get('buyer', ''))
    except ValueError:
        abort(400)
    buyer = DataBuyer.query.get_or_404(buyer_id)
    kind = request.form.get('kind')
    note = (request.form.get('note') or '').strip()[:300]
    if kind not in ('wire', 'refund', 'debit') or not note:
        flash("Choose a type and add a note describing the payment (e.g. the wire reference).", "error")
        return redirect(url_for('admin_bp.data_requests'))
    try:
        cents = parse_dollars(request.form.get('amount'), 1, 100_000_000)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for('admin_bp.data_requests'))

    if kind == 'debit':
        if credit_balance(buyer.credit_entries) < cents:
            flash("That would take the buyer's balance below zero.", "error")
            return redirect(url_for('admin_bp.data_requests'))
        entry_type, signed = 'adjustment', -cents
    else:
        entry_type, signed = ('topup' if kind == 'wire' else 'refund'), cents
    db.session.add(BuyerCreditEntry(buyer_id=buyer.id, amount_cents=signed, entry_type=entry_type,
                                    note=note, created_by_user_id=current_user.id))
    db.session.commit()
    flash(f"Recorded {format_usd(cents)} ({entry_type}) for {buyer.name}.", "success")
    return redirect(url_for('admin_bp.data_requests'))


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

    # Same rule as the buyer marketplace: calibration tasks and non-consenting
    # contractors' responses are never exported.
    consented = consented_user_ids()
    lines = []
    for sub in submissions:
        if task.is_assessment or sub.user_id not in consented:
            continue
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
    overview = build_overview()
    recent = ContractorPayout.query.order_by(ContractorPayout.created_at.desc()).limit(40).all()
    pending = [p for p in recent if p.status == 'pending']
    return render_template('admin_payouts.html', overview=overview, recent=recent, pending=pending,
                           hold_days=HOLD_DAYS)


def _report_batch(result):
    paid = sum(r["amount_cents"] for r in result["paid"])
    if result["paid"]:
        flash(f"Paid {len(result['paid'])} contractor(s), ${paid / 100:,.2f} total.", "success")
    if result["pending"]:
        flash(f"{len(result['pending'])} transfer(s) have an unknown outcome and are pending - "
              "retry them below (same idempotency key, so no double pay).", "error")
    if result["failed"]:
        flash(f"{len(result['failed'])} transfer(s) were rejected by Stripe (see Recent payouts); "
              "the earnings were released to the next batch.", "error")
    if not (result["paid"] or result["pending"] or result["failed"]):
        flash("Nothing was ready to pay.", "info")


@admin_bp.route('/admin/payouts/run', methods=['POST'])
@login_required
def run_payouts():
    """Pay every contractor whose earnings have cleared the review hold."""
    _require_admin()
    try:
        _report_batch(run_payout_batch(current_user.id))
    except PayoutError as e:
        flash(str(e), "error")
    return redirect(url_for('admin_bp.payouts'))


@admin_bp.route('/admin/payouts/pay/<int:user_id>', methods=['POST'])
@login_required
def pay_user(user_id):
    """Pay one contractor now. Same rules as the batch (hold period, minimum,
    verified account, not frozen) - there is deliberately no way to skip them."""
    _require_admin()
    try:
        result = run_payout_batch(current_user.id, only_user_id=user_id)
        _report_batch(result)
        for s in result["skipped"]:
            flash(f"Not paid: {s['reason']}.", "info")
    except PayoutError as e:
        flash(str(e), "error")
    return redirect(url_for('admin_bp.payouts'))


@admin_bp.route('/admin/payouts/<int:payout_id>/retry', methods=['POST'])
@login_required
def retry_payout_route(payout_id):
    _require_admin()
    try:
        outcome = retry_payout(payout_id)
        flash({"paid": "Payout sent.", "failed": "Stripe rejected the payout; earnings were released.",
               "pending": "Still no confirmation from Stripe - try again shortly."}[outcome],
              "success" if outcome == "paid" else "error")
    except PayoutError as e:
        flash(str(e), "error")
    return redirect(url_for('admin_bp.payouts'))


@admin_bp.route('/admin/contractors/<int:user_id>/freeze', methods=['POST'])
@login_required
def toggle_payout_freeze(user_id):
    """Fraud/QA freeze: no payouts to this contractor until unfrozen."""
    _require_admin()
    contractor = ContractorProfile.query.filter_by(user_id=user_id).first_or_404()
    contractor.payouts_frozen = not contractor.payouts_frozen
    db.session.commit()
    flash("Payouts frozen." if contractor.payouts_frozen else "Payouts unfrozen.", "success")
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
