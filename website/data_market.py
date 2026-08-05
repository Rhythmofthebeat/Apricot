import json
import logging

import stripe
from flask import Blueprint, render_template, request, flash, redirect, url_for, Response, abort, current_app
from flask_login import login_required, current_user

from . import db
from .models import DataBuyer, EarnTask
from .dataset_export import build_bulk_export

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
