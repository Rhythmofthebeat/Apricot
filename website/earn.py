import json
import logging
from collections import Counter
from datetime import datetime, timezone

import stripe
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort, current_app
from flask_login import login_required, current_user
from . import db
from .models import EarnTask, TaskSubmission, ContractorProfile, ExpertCredential, EARN_DOMAINS
from .expert_verification import (
    CREDENTIAL_TYPES, TRAINING_CONSENT_VERSION, ASSESSMENT_MIN_ATTEMPTS, ASSESSMENT_PASS_ACCURACY,
    SKILLED_MIN_TASKS, SKILLED_MIN_QUALITY, compute_tier, can_access_task, grade_gold,
    assessment_passed,
)
from email_utils import external_url

logger = logging.getLogger(__name__)

earn = Blueprint('earn', __name__)


@earn.app_context_processor
def _inject_consent_version():
    return {"consent_version": TRAINING_CONSENT_VERSION}


def _get_contractor():
    return ContractorProfile.query.filter_by(user_id=current_user.id).first()


def _approved_credential_domains(user_id):
    rows = ExpertCredential.query.filter_by(user_id=user_id, status='approved') \
        .with_entities(ExpertCredential.domain).all()
    return sorted({r[0] for r in rows})


def _tier_info(contractor):
    """(tier, approved credential domains) for a contractor, derived on demand
    so it can never drift out of sync with the underlying checks."""
    cred_domains = _approved_credential_domains(contractor.user_id)
    tier = compute_tier(
        assessment_correct=contractor.assessment_correct,
        assessment_attempts=contractor.assessment_attempts,
        quality_score=contractor.quality_score,
        tasks_completed=contractor.tasks_completed,
        identity_verified=contractor.payout_enabled,
        approved_credential_domains=cred_domains,
    )
    return tier, cred_domains


def _task_accessible(task, contractor, tier, cred_domains):
    return can_access_task(
        min_tier=task.min_tier,
        required_domains=json.loads(task.required_domains) if task.required_domains else [],
        tier=tier,
        declared_domains=json.loads(contractor.domains),
        credential_domains=cred_domains,
    )


def tier_not_enough(task, tier):
    from .expert_verification import tier_at_least
    return not tier_at_least(tier, task.min_tier)


def _needs_consent(contractor):
    return contractor.training_consent_version != TRAINING_CONSENT_VERSION


def _record_consent(contractor):
    contractor.training_consent_at = datetime.now(timezone.utc)
    contractor.training_consent_version = TRAINING_CONSENT_VERSION


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
#  Onboarding - domain selection (required before accessing tasks)
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

        if not request.form.get('training_consent'):
            flash("Please accept the data licence to join - your task responses are used as AI training data.", "error")
            return render_template('earn_onboard.html', domains=EARN_DOMAINS)

        existing_count = TaskSubmission.query.filter_by(user_id=current_user.id).count()
        profile = ContractorProfile(
            user_id=current_user.id,
            domains=json.dumps(valid),
            tasks_completed=existing_count,
        )
        _record_consent(profile)
        db.session.add(profile)
        db.session.commit()
        flash("You're set up! Start completing tasks to earn, and verify your credentials to unlock higher-paying expert work.", "success")
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

    if _needs_consent(contractor):
        return redirect(url_for('earn.consent'))

    user_domains = set(json.loads(contractor.domains))
    tier, cred_domains = _tier_info(contractor)

    all_open = EarnTask.query.filter_by(status='open').order_by(EarnTask.created_at.desc()).all()
    open_tasks = [t for t in all_open if _task_accessible(t, contractor, tier, cred_domains)]
    # Tasks hidden only because of verification level - shown as a nudge to verify.
    locked_count = sum(
        1 for t in all_open
        if t not in open_tasks and t.min_tier != 'standard'
        and _task_accessible(t, contractor, 'expert', sorted(set(cred_domains) | user_domains))
    )

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
        tier=tier,
        locked_count=locked_count,
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

    if _needs_consent(contractor):
        return redirect(url_for('earn.consent'))

    tier, cred_domains = _tier_info(contractor)
    if not _task_accessible(task, contractor, tier, cred_domains):
        if task.min_tier != 'standard' and tier_not_enough(task, tier):
            flash("This task needs a higher verification level. Verify your credentials to unlock it.", "info")
            return redirect(url_for('earn.verification'))
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
        if task.is_assessment:
            # Calibration task: grade against the known answer. It feeds the
            # skills assessment only - no consensus, no quality-score change.
            graded = grade_gold(task.task_type, response, task.gold_answer)
            if graded is not None:
                contractor.assessment_attempts += 1
                contractor.assessment_correct += 1 if graded else 0
                sub.agreement_score = 1.0 if graded else 0.0
        else:
            _maybe_compute_consensus(task)

        db.session.commit()
        flash(f"Task complete! ${task.pay_cents / 100:.2f} added to your earnings.", "success")
        return redirect(url_for('earn.tasks'))

    return render_template('earn_task_detail.html', task=task)


# ------------------------------------------------------------------ #
#  Training-data licence (consent)
# ------------------------------------------------------------------ #

@earn.route('/earn/consent', methods=['GET', 'POST'])
@login_required
def consent():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))
    if not _needs_consent(contractor):
        return redirect(url_for('earn.tasks'))

    if request.method == 'POST':
        if not request.form.get('training_consent'):
            flash("You need to accept the data licence to keep completing tasks.", "error")
            return render_template('earn_consent.html')
        _record_consent(contractor)
        db.session.commit()
        flash("Thanks - you're all set.", "success")
        return redirect(url_for('earn.tasks'))
    return render_template('earn_consent.html')


# ------------------------------------------------------------------ #
#  Expert verification: identity, credentials, skills assessment
# ------------------------------------------------------------------ #

MAX_CREDENTIALS_PER_USER = 10


@earn.route('/earn/verification')
@login_required
def verification():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))
    if _needs_consent(contractor):
        return redirect(url_for('earn.consent'))

    tier, cred_domains = _tier_info(contractor)
    credentials = ExpertCredential.query.filter_by(user_id=current_user.id) \
        .order_by(ExpertCredential.created_at.desc()).all()
    open_assessments = EarnTask.query.filter_by(status='open', is_assessment=True).count()
    return render_template('earn_verification.html',
        contractor=contractor, tier=tier, cred_domains=cred_domains,
        credentials=credentials, domains=EARN_DOMAINS, credential_types=CREDENTIAL_TYPES,
        assessment_passed=assessment_passed(contractor.assessment_correct, contractor.assessment_attempts),
        assessment_min_attempts=ASSESSMENT_MIN_ATTEMPTS,
        assessment_pass_pct=int(ASSESSMENT_PASS_ACCURACY * 100),
        skilled_min_tasks=SKILLED_MIN_TASKS, skilled_min_quality=SKILLED_MIN_QUALITY,
        open_assessments=open_assessments,
    )


def _clean(value, limit):
    return (value or '').strip()[:limit]


@earn.route('/earn/verification/credential', methods=['POST'])
@login_required
def add_credential():
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))

    domain = request.form.get('domain', '')
    credential_type = request.form.get('credential_type', '')
    title = _clean(request.form.get('title'), 200)
    issuer = _clean(request.form.get('issuer'), 200)
    license_number = _clean(request.form.get('license_number'), 100)
    verification_url = _clean(request.form.get('verification_url'), 500)

    if domain not in EARN_DOMAINS or credential_type not in CREDENTIAL_TYPES or not title or not issuer:
        flash("Choose a domain and type, and fill in the credential title and who issued it.", "error")
        return redirect(url_for('earn.verification'))
    if verification_url and not verification_url.lower().startswith(('http://', 'https://')):
        flash("The verification link must start with http:// or https://.", "error")
        return redirect(url_for('earn.verification'))
    if ExpertCredential.query.filter_by(user_id=current_user.id).count() >= MAX_CREDENTIALS_PER_USER:
        flash("You've reached the credential limit. Remove one before adding another.", "error")
        return redirect(url_for('earn.verification'))
    if not license_number and not verification_url:
        flash("Add a licence/certificate number or a public verification link so a reviewer can check it.", "error")
        return redirect(url_for('earn.verification'))

    db.session.add(ExpertCredential(
        user_id=current_user.id, domain=domain, credential_type=credential_type,
        title=title, issuer=issuer, license_number=license_number or None,
        verification_url=verification_url or None,
    ))
    db.session.commit()
    flash("Credential submitted. A reviewer will check it - you'll see the result here.", "success")
    return redirect(url_for('earn.verification'))


@earn.route('/earn/verification/credential/<int:credential_id>/delete', methods=['POST'])
@login_required
def delete_credential(credential_id):
    cred = ExpertCredential.query.filter_by(id=credential_id, user_id=current_user.id).first_or_404()
    if cred.status == 'approved':
        flash("An approved credential can't be removed here - contact support if it's wrong.", "error")
    else:
        db.session.delete(cred)
        db.session.commit()
        flash("Credential removed.", "success")
    return redirect(url_for('earn.verification'))


@earn.route('/earn/verification/listing', methods=['POST'])
@login_required
def toggle_listing():
    """Opt in/out of the anonymous expert directory data buyers can browse."""
    contractor = _get_contractor()
    if not contractor:
        return redirect(url_for('earn.onboard'))
    contractor.listed_in_directory = bool(request.form.get('listed'))
    db.session.commit()
    flash("You're listed anonymously for project work." if contractor.listed_in_directory
          else "You're no longer listed.", "success")
    return redirect(url_for('earn.verification'))


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
            refresh_url=external_url('earn.payout_setup'),
            return_url=external_url('earn.payout_return'),
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
                flash("Almost there - Stripe needs a bit more information to enable payouts. Come back here to finish.", "info")
        except stripe.error.StripeError:
            flash("Payout setup saved. Stripe will notify you when your account is fully verified.", "info")
    return redirect(url_for('earn.history'))


@earn.route('/earn/payouts/refresh')
@login_required
def payout_refresh():
    """Stripe redirects here when the onboarding link expires - just restart the flow."""
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
