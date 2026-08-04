import json
import io
import logging

import stripe
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, Response, current_app
from flask_login import login_required, current_user
from . import db
from .models import EarnTask, TaskSubmission, User, EARN_DOMAINS, ContractorProfile, Company, Job, CompanyMember, Application
from .dataset_export import shape_submission

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
    return render_template('admin_company_detail.html',
        company=company, owner=owner, members=members, jobs=jobs, application_counts=application_counts)


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
