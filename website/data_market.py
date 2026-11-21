import json
import logging
from collections import defaultdict

import stripe
from flask import Blueprint, render_template, request, flash, redirect, url_for, Response, abort, current_app
from flask_login import login_required, current_user

from . import db
from .models import DataBuyer, EarnTask, DataRequest, ContractorProfile, ExpertCredential, EARN_DOMAINS
from .dataset_export import build_bulk_export
from .expert_verification import compute_tier, pseudonym, tier_at_least
from . import pricing

logger = logging.getLogger(__name__)

data_market = Blueprint('data_market', __name__)

TASK_TYPES = ("ranking", "rating", "factcheck", "writing")


def _require_data_buyer_admin():
    if not current_user.is_authenticated or not current_user.is_data_buyer_admin or not current_user.data_buyer_id:
        abort(403)


@data_market.route('/data/signup', methods=['GET', 'POST'])
@login_required
def signup():
    if current_user.data_buyer_id:
        return redirect(url_for('data_market.dashboard'))

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        contact_email = (request.form.get('contact_email') or current_user.email or '').strip()

        if not name:
            flash("Company name is required.", "error")
            return render_template('data_signup.html')

        buyer = DataBuyer(name=name, contact_email=contact_email, subscription_status='inactive')
        db.session.add(buyer)
        db.session.flush()  # assign buyer.id before linking

        current_user.data_buyer_id = buyer.id
        current_user.is_data_buyer_admin = True
        db.session.commit()

        flash("Account created! Subscribe below to unlock dataset downloads.", "success")
        return redirect(url_for('data_market.dashboard'))

    return render_template('data_signup.html')


@data_market.route('/data/dashboard')
@login_required
def dashboard():
    _require_data_buyer_admin()
    buyer = DataBuyer.query.get_or_404(current_user.data_buyer_id)

    counts = {t: EarnTask.query.filter_by(task_type=t).count() for t in TASK_TYPES}

    return render_template(
        'data_dashboard.html',
        buyer=buyer,
        task_types=TASK_TYPES,
        counts=counts,
        billing_configured=bool(current_app.config.get('STRIPE_SECRET_KEY') and current_app.config.get('STRIPE_DATA_PRICE_ID')),
        require_active_subscription=REQUIRE_ACTIVE_SUBSCRIPTION,
    )


@data_market.route('/data/subscribe', methods=['POST'])
@login_required
def subscribe():
    _require_data_buyer_admin()

    stripe_secret = current_app.config.get('STRIPE_SECRET_KEY')
    price_id = current_app.config.get('STRIPE_DATA_PRICE_ID')
    if not stripe_secret or not price_id:
        flash("Stripe billing isn't configured yet (missing STRIPE_SECRET_KEY / STRIPE_DATA_PRICE_ID).", "error")
        return redirect(url_for('data_market.dashboard'))

    buyer = DataBuyer.query.get_or_404(current_user.data_buyer_id)
    stripe.api_key = stripe_secret

    try:
        session = stripe.checkout.Session.create(
            mode='subscription',
            customer_email=buyer.contact_email,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=request.host_url.rstrip('/') + url_for('data_market.dashboard'),
            cancel_url=request.host_url.rstrip('/') + url_for('data_market.dashboard'),
            client_reference_id=str(buyer.id),
        )
    except Exception as e:
        flash(f"Could not start checkout: {e}", "error")
        return redirect(url_for('data_market.dashboard'))

    return redirect(session.url, code=303)


@data_market.route('/data/stripe/webhook', methods=['POST'])
def stripe_webhook():
    webhook_secret = current_app.config.get('STRIPE_DATA_WEBHOOK_SECRET')
    if not webhook_secret:
        abort(503)

    payload = request.data
    sig_header = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        abort(400)

    event_type = event['type']
    data_obj = event['data']['object']

    if event_type == 'checkout.session.completed':
        buyer = DataBuyer.query.get(int(data_obj.get('client_reference_id') or 0))
        if buyer:
            buyer.subscription_status = 'active'
            buyer.stripe_customer_id = data_obj.get('customer')
            buyer.stripe_subscription_id = data_obj.get('subscription')
            db.session.commit()

    elif event_type in ('customer.subscription.deleted', 'customer.subscription.canceled'):
        buyer = DataBuyer.query.filter_by(stripe_subscription_id=data_obj.get('id')).first()
        if buyer:
            buyer.subscription_status = 'canceled'
            db.session.commit()

    return '', 200


# Downloads are free while the marketplace is in beta - billing (STRIPE_DATA_
# PRICE_ID / STRIPE_DATA_WEBHOOK_SECRET, see data_dashboard.html) is fully
# built and still fires normally, this just doesn't gate export() on it yet.
# Flip back to True to re-enforce the active-subscription requirement.
REQUIRE_ACTIVE_SUBSCRIPTION = False


@data_market.route('/data/export/<task_type>')
@login_required
def export(task_type):
    _require_data_buyer_admin()

    if task_type not in TASK_TYPES:
        abort(404)

    buyer = DataBuyer.query.get_or_404(current_user.data_buyer_id)
    if REQUIRE_ACTIVE_SUBSCRIPTION and buyer.subscription_status != 'active':
        flash("An active data subscription is required to download datasets.", "error")
        return redirect(url_for('data_market.dashboard'))

    records = build_bulk_export(task_type=task_type)
    body = "\n".join(json.dumps(r) for r in records)

    return Response(
        body,
        mimetype='application/x-ndjson',
        headers={"Content-Disposition": f"attachment; filename={task_type}_dataset.jsonl"},
    )


# ------------------------------------------------------------------ #
#  Pricing (public) with a live quote calculator
# ------------------------------------------------------------------ #

def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return None


@data_market.route('/data/pricing')
def pricing_page():
    quote, error = None, None
    mode = request.args.get('mode')
    if mode in ('project', 'dataset'):
        task_type = request.args.get('task_type', 'ranking')
        tier = request.args.get('tier', 'skilled')
        try:
            if mode == 'project':
                units, responses = _int_arg('units', 1000), _int_arg('responses', 3)
                if units is None or responses is None:
                    raise ValueError("Enter whole numbers for items and responses per item.")
                quote = pricing.quote_custom_project(task_type, tier, units, responses)
            else:
                records = _int_arg('records', 10000)
                if records is None:
                    raise ValueError("Enter a whole number of records.")
                quote = pricing.quote_dataset_license(
                    task_type, records, tier, exclusive=bool(request.args.get('exclusive')))
        except ValueError as e:
            error = str(e)
    return render_template(
        'data_pricing.html', quote=quote, error=error, mode=mode,
        form=request.args, task_types=TASK_TYPES, tiers=pricing.TIERS, tier_labels=pricing.TIER_LABELS,
        examples={t: pricing.quote_custom_project('ranking', t, 1000, 3)['price_cents'] for t in pricing.TIERS},
        per_record=pricing.DATASET_PER_RECORD_CENTS, tier_mult=pricing.DATASET_TIER_MULTIPLIER,
        min_project=pricing.MIN_PROJECT_CENTS, exclusive_mult=pricing.EXCLUSIVE_MULTIPLIER,
        discounts=pricing.VOLUME_DISCOUNTS, usd=pricing.format_usd,
    )


# ------------------------------------------------------------------ #
#  Verified-expert directory (anonymous)
# ------------------------------------------------------------------ #

def _directory_entries():
    """Contractors who opted in and have reached skilled tier or above, with no
    name/email - just a stable pseudonym plus what a buyer needs to judge fit."""
    profiles = ContractorProfile.query.filter_by(listed_in_directory=True).all()
    if not profiles:
        return []
    creds = defaultdict(set)
    for c in ExpertCredential.query.filter(
            ExpertCredential.user_id.in_([p.user_id for p in profiles]),
            ExpertCredential.status == 'approved').all():
        creds[c.user_id].add(c.domain)

    entries = []
    for p in profiles:
        tier = compute_tier(
            assessment_correct=p.assessment_correct, assessment_attempts=p.assessment_attempts,
            quality_score=p.quality_score, tasks_completed=p.tasks_completed,
            identity_verified=p.payout_enabled, approved_credential_domains=creds[p.user_id])
        if tier == 'standard':
            continue
        entries.append({
            "label": pseudonym(p.user_id, current_app.secret_key),
            "tier": tier,
            "domains": json.loads(p.domains),
            "verified_domains": sorted(creds[p.user_id]),
            "quality_pct": round(p.quality_score * 100),
            "tasks_completed": p.tasks_completed,
        })
    entries.sort(key=lambda e: (e["tier"] != "expert", -e["quality_pct"], -e["tasks_completed"]))
    return entries


@data_market.route('/data/experts')
@login_required
def experts():
    _require_data_buyer_admin()
    domain = request.args.get('domain', '')
    tier = request.args.get('tier', '')
    entries = _directory_entries()
    if domain in EARN_DOMAINS:
        entries = [e for e in entries
                   if domain in e["verified_domains"] or (e["tier"] != "expert" and domain in e["domains"])]
    if tier in pricing.TIERS:
        entries = [e for e in entries if tier_at_least(e["tier"], tier)]
    return render_template('data_experts.html', entries=entries, domains=EARN_DOMAINS,
                           tiers=pricing.TIERS, tier_labels=pricing.TIER_LABELS,
                           sel_domain=domain, sel_tier=tier)


# ------------------------------------------------------------------ #
#  Custom data / expert-sourcing requests
# ------------------------------------------------------------------ #

@data_market.route('/data/requests', methods=['GET', 'POST'])
@login_required
def requests_page():
    _require_data_buyer_admin()
    buyer = DataBuyer.query.get_or_404(current_user.data_buyer_id)

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()[:200]
        task_type = request.form.get('task_type', '')
        tier = request.form.get('tier', '')
        domain = request.form.get('domain', '')
        details = (request.form.get('details') or '').strip()[:4000]
        try:
            units = int(request.form.get('units', ''))
            responses = int(request.form.get('responses_per_unit', 3))
            estimate = pricing.quote_custom_project(task_type, tier, units, responses)
        except (ValueError, TypeError) as e:
            flash(f"Check the request details: {e}", "error")
            return redirect(url_for('data_market.requests_page'))
        if not title:
            flash("Give the request a title.", "error")
            return redirect(url_for('data_market.requests_page'))

        db.session.add(DataRequest(
            buyer_id=buyer.id, requested_by_user_id=current_user.id, title=title,
            task_type=task_type, tier=tier, domain=domain if domain in EARN_DOMAINS else None,
            units=units, responses_per_unit=responses, details=details or None,
            estimated_cents=estimate["price_cents"],
        ))
        db.session.commit()
        flash("Request sent. We'll come back with a firm quote.", "success")
        return redirect(url_for('data_market.requests_page'))

    rows = DataRequest.query.filter_by(buyer_id=buyer.id).order_by(DataRequest.created_at.desc()).all()
    return render_template('data_requests.html', buyer=buyer, requests=rows, task_types=TASK_TYPES,
                           tiers=pricing.TIERS, tier_labels=pricing.TIER_LABELS, domains=EARN_DOMAINS,
                           usd=pricing.format_usd)
