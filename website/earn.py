import json
import logging
from collections import Counter

import stripe
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, current_app
from flask_login import login_required, current_user
from . import db
from .models import EarnTask, TaskSubmission, ContractorProfile, EARN_DOMAINS

logger = logging.getLogger(__name__)

earn = Blueprint('earn', __name__)


def _get_contractor():
    return ContractorProfile.query.filter_by(user_id=current_user.id).first()


def _task_visible(task, user_domains):
    """True if task has no domain restriction, or if it overlaps with the user's domains."""
    if not task.required_domains:
        return True
    return bool(set(json.loads(task.required_domains)) & user_domains)


# ------------------------------------------------------------------ #
#  Consensus scoring engine
# ------------------------------------------------------------------ #

def _maybe_compute_consensus(task):
    """After each new submission, recompute consensus if min_responses is met.
    Scores any submission that doesn't yet have an agreement_score and updates
    that contributor's quality_score via a rolling exponential average.
    Must be called after db.session.flush() so the new submission is visible."""
    if task.task_type == 'writing':
        return  # free-text answers can't be auto-scored

    submissions = TaskSubmission.query.filter_by(task_id=task.id).all()
    if len(submissions) < task.min_responses:
        return

    responses = [s.response for s in submissions if s.response]
    if not responses:
        return

    # --- compute consensus ---
    if task.task_type in ('ranking', 'factcheck'):
        counts = Counter(responses)
        consensus = counts.most_common(1)[0][0]
        consensus_data = {
            "answer": consensus,
            "total": len(submissions),
            "distribution": dict(counts),
        }

    elif task.task_type == 'rating':
        try:
            scores = [float(r) for r in responses]
        except (ValueError, TypeError):
            return
        mean = sum(scores) / len(scores)
        consensus = str(round(mean * 2) / 2)   # round to nearest 0.5
        consensus_data = {
            "answer": consensus,
            "total": len(submissions),
            "mean": round(mean, 2),
        }
    else:
        return

    task.consensus_answer = json.dumps(consensus_data)

    # --- score submissions that haven't been scored yet ---
    for sub in submissions:
        if sub.agreement_score is not None or not sub.response:
            continue

        if task.task_type in ('ranking', 'factcheck'):
            sub.agreement_score = 1.0 if sub.response == consensus else 0.0

        elif task.task_type == 'rating':
            try:
                sub.agreement_score = round(
                    max(0.0, 1.0 - abs(float(sub.response) - consensus_data["mean"]) / 4), 4
                )
            except (ValueError, TypeError):
                continue

        # rolling exponential average: recent tasks count for 15% of quality score
        contractor = ContractorProfile.query.filter_by(user_id=sub.user_id).first()
        if contractor and sub.agreement_score is not None:
            contractor.quality_score = round(
                min(1.0, max(0.0, contractor.quality_score * 0.85 + sub.agreement_score * 0.15)), 4
            )


# ------------------------------------------------------------------ #
#  Onboarding — domain selection (required before accessing tasks)
# ------------------------------------------------------------------ #

@earn.route('/earn/onboard', methods=['GET', 'POST'])
@login_required
def onboard():
    if _get_contractor():
        return redirect(url_for('earn.tasks'))

    if request.method == 'POST':
        selected = request.form.getlist('domains')
        valid = [d for d in selected if d in EARN_DOMAINS]
        if not valid:
            flash("Select at least one area of expertise.", "error")
            return render_template('earn_onboard.html', domains=EARN_DOMAINS)

        existing_count = TaskSubmission.query.filter_by(user_id=current_user.id).count()
        db.session.add(ContractorProfile(
            user_id=current_user.id,
            domains=json.dumps(valid),
            tasks_completed=existing_count,
        ))
        db.session.commit()
        flash("You're set up! Start completing tasks to earn.", "success")
        return redirect(url_for('earn.tasks'))

    return render_template('earn_onboard.html', domains=EARN_DOMAINS)


# ------------------------------------------------------------------ #
#  Update domains (from the tasks page)
# ------------------------------------------------------------------ #

@earn.route('/earn/domains', methods=['POST'])
@login_required
def update_domains():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    selected = request.form.getlist('domains')
    valid = [d for d in selected if d in EARN_DOMAINS]
    if not valid:
        flash("Select at least one area of expertise.", "error")
        return redirect(url_for('earn.tasks'))

    contractor.domains = json.dumps(valid)
    db.session.commit()
    flash("Your expertise areas have been updated.", "success")
    return redirect(url_for('earn.tasks'))


# ------------------------------------------------------------------ #
#  Task list
# ------------------------------------------------------------------ #

@earn.route('/earn')
@login_required
def tasks():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    user_domains = set(json.loads(contractor.domains))

    all_open = EarnTask.query.filter_by(status='open').order_by(EarnTask.created_at.desc()).all()
    open_tasks = [t for t in all_open if _task_visible(t, user_domains)]

    completed_ids = {
        s.task_id for s in TaskSubmission.query.filter_by(user_id=current_user.id)
        .with_entities(TaskSubmission.task_id).all()
    }
    total_earned = db.session.query(db.func.sum(TaskSubmission.earnings_cents)) \
        .filter_by(user_id=current_user.id).scalar() or 0
    pending_payout = db.session.query(db.func.sum(TaskSubmission.earnings_cents)) \
        .filter_by(user_id=current_user.id, paid_out=False).scalar() or 0

    # Submission counts per task for the progress indicator
    task_ids = [t.id for t in open_tasks]
    sub_counts = {}
    if task_ids:
        rows = (
            db.session.query(TaskSubmission.task_id, db.func.count(TaskSubmission.id))
            .filter(TaskSubmission.task_id.in_(task_ids))
            .group_by(TaskSubmission.task_id)
            .all()
        )
        sub_counts = dict(rows)

    return render_template('earn_tasks.html',
        tasks=open_tasks,
        completed_ids=completed_ids,
        total_earned=total_earned,
        pending_payout=pending_payout,
        contractor=contractor,
        user_domains=sorted(user_domains),
        all_domains=EARN_DOMAINS,
        sub_counts=sub_counts,
    )


# ------------------------------------------------------------------ #
#  Task detail + submission
# ------------------------------------------------------------------ #

@earn.route('/earn/task/<int:task_id>', methods=['GET', 'POST'])
@login_required
def task_detail(task_id):
    task = EarnTask.query.get_or_404(task_id)
    if task.status != 'open':
        flash("This task is no longer available.", "error")
        return redirect(url_for('earn.tasks'))

    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    if task.required_domains:
        user_domains = set(json.loads(contractor.domains))
        if not (set(json.loads(task.required_domains)) & user_domains):
            abort(403)

    already_done = TaskSubmission.query.filter_by(task_id=task_id, user_id=current_user.id).first()
    if already_done:
        flash("You've already completed this task.", "success")
        return redirect(url_for('earn.tasks'))

    if request.method == 'POST':
        response = request.form.get('response', '').strip()
        explanation = request.form.get('explanation', '').strip()

        if not response:
            flash("Please select or provide a response.", "error")
            return render_template('earn_task_detail.html', task=task)

        sub = TaskSubmission(
            task_id=task_id,
            user_id=current_user.id,
            response=response,
            explanation=explanation,
            earnings_cents=task.pay_cents,
        )
        db.session.add(sub)
        db.session.flush()  # write sub to session so consensus query sees it

        contractor.tasks_completed += 1
        _maybe_compute_consensus(task)

        db.session.commit()
        flash(f"Task complete! ${task.pay_cents / 100:.2f} added to your earnings.", "success")
        return redirect(url_for('earn.tasks'))

    return render_template('earn_task_detail.html', task=task)


# ------------------------------------------------------------------ #
#  Stripe Connect payout setup
# ------------------------------------------------------------------ #

@earn.route('/earn/payouts/setup')
@login_required
def payout_setup():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        flash("Stripe payouts are not configured on this platform yet.", "error")
        return redirect(url_for('earn.tasks'))

    try:
        # Create a new Express account only if one doesn't exist yet
        if not contractor.stripe_account_id:
            account = stripe.Account.create(
                type='express',
                email=current_user.email,
                capabilities={'transfers': {'requested': True}},
            )
            contractor.stripe_account_id = account.id
            db.session.commit()

        link = stripe.AccountLink.create(
            account=contractor.stripe_account_id,
            refresh_url=url_for('earn.payout_setup', _external=True),
            return_url=url_for('earn.payout_return', _external=True),
            type='account_onboarding',
        )
        return redirect(link.url)
    except stripe.error.StripeError as e:
        logger.exception("Stripe Connect setup failed")
        flash(f"Could not start payout setup: {e.user_message}", "error")
        return redirect(url_for('earn.tasks'))


@earn.route('/earn/payouts/return')
@login_required
def payout_return():
    """Stripe redirects here after the worker completes (or skips) the Express onboarding flow."""
    contractor = _get_contractor()
    if contractor and contractor.stripe_account_id:
        stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
        try:
            account = stripe.Account.retrieve(contractor.stripe_account_id)
            if account.payouts_enabled:
                contractor.payout_enabled = True
                db.session.commit()
                flash("Payout account connected! You'll receive payments directly to your bank.", "success")
            else:
                flash("Almost there — Stripe needs a bit more information to enable payouts. Come back here to finish.", "info")
        except stripe.error.StripeError:
            flash("Payout setup saved. Stripe will notify you when your account is fully verified.", "info")
    return redirect(url_for('earn.history'))


@earn.route('/earn/payouts/refresh')
@login_required
def payout_refresh():
    """Stripe redirects here when the onboarding link expires — just restart the flow."""
    return redirect(url_for('earn.payout_setup'))


# ------------------------------------------------------------------ #
#  Earnings history
# ------------------------------------------------------------------ #

@earn.route('/earn/history')
@login_required
def history():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    submissions = TaskSubmission.query.filter_by(user_id=current_user.id) \
        .order_by(TaskSubmission.created_at.desc()).all()
    total = sum(s.earnings_cents for s in submissions)
    pending = sum(s.earnings_cents for s in submissions if not s.paid_out)
    return render_template('earn_history.html',
        submissions=submissions, total=total, pending=pending,
        contractor=contractor)
