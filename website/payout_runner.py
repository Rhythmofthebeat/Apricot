"""Runs contractor payout batches against Stripe.

Safety design - this is the only place money leaves the platform:

1. RESERVE first. Each payable submission is claimed (payout_id set) in the
   database and committed BEFORE any money moves, using a guarded UPDATE so two
   concurrent runs can never claim the same submission.
2. SEND with an idempotency key that is stable per (batch, contractor). If the
   request times out and the outcome is unknown, the payout stays `pending` and
   a retry re-sends the same key - Stripe returns the original transfer rather
   than creating a second one.
3. Only a definite rejection (InvalidRequestError, e.g. insufficient platform
   balance or a bad destination) releases the reservation so the earnings roll
   into a later batch. Anything ambiguous is left `pending`.

Stripe keeps idempotency keys for about 24 hours: a pending payout should be
retried the same day.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone

import stripe
from flask import current_app

from . import db
from .models import TaskSubmission, ContractorProfile, ContractorPayout, User
from .payouts import plan_payouts, summarize_user, idempotency_key, new_batch_key, MIN_PAYOUT_CENTS

logger = logging.getLogger(__name__)


class PayoutError(Exception):
    """A problem that stops the whole run (e.g. Stripe isn't configured)."""


def _configure_stripe():
    key = current_app.config.get('STRIPE_SECRET_KEY')
    if not key:
        raise PayoutError("Stripe is not configured.")
    stripe.api_key = key


def _unpaid_by_user(only_user_id=None):
    query = TaskSubmission.query.filter(
        TaskSubmission.paid_out.is_(False),
        TaskSubmission.payout_id.is_(None),
        TaskSubmission.earnings_cents > 0,
    )
    if only_user_id is not None:
        query = query.filter(TaskSubmission.user_id == only_user_id)
    by_user = defaultdict(list)
    for s in query.all():
        by_user[s.user_id].append(s)
    return by_user


def _blocked_reasons(user_ids):
    profiles = {p.user_id: p for p in ContractorProfile.query.filter(ContractorProfile.user_id.in_(user_ids)).all()} \
        if user_ids else {}
    blocked = {}
    for uid in user_ids:
        p = profiles.get(uid)
        if p is None or not p.stripe_account_id or not p.payout_enabled:
            blocked[uid] = "payout setup not finished"
        elif p.payouts_frozen:
            blocked[uid] = "payouts frozen"
    return blocked, profiles


def build_overview(now=None):
    """Everything the admin payouts page needs, using the same rules the
    runner will apply, so the preview matches what actually happens."""
    now = now or datetime.now(timezone.utc)
    by_user = _unpaid_by_user()
    blocked, profiles = _blocked_reasons(list(by_user))
    planned, skipped = plan_payouts(by_user, blocked, now)
    planned_ids = {p["user_id"] for p in planned}
    skipped_reason = {s["user_id"]: s["reason"] for s in skipped}
    users = {u.id: u for u in User.query.filter(User.id.in_(list(by_user))).all()} if by_user else {}

    rows = []
    for uid, subs in by_user.items():
        if uid not in users:        # account deleted - nothing to show or pay
            continue
        info = summarize_user(subs, now)
        if uid in planned_ids:
            status, reason = "ready", ""
        elif uid in skipped_reason:
            status, reason = "blocked", skipped_reason[uid]
        elif info["payable_cents"] == 0 and info["held_cents"]:
            status, reason = "held", "admin hold on all earnings"
        else:
            status, reason = "hold", "in review hold"
        rows.append({"user": users.get(uid), "profile": profiles.get(uid), "status": status,
                     "reason": reason, **{k: info[k] for k in
                     ("payable_cents", "in_hold_cents", "held_cents", "next_release")}})
    order = {"ready": 0, "blocked": 1, "held": 2, "hold": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["payable_cents"]))
    return {
        "rows": rows,
        "ready_total": sum(p["amount_cents"] for p in planned),
        "ready_count": len(planned),
        "in_hold_total": sum(r["in_hold_cents"] for r in rows),
        "min_payout_cents": MIN_PAYOUT_CENTS,
    }


def _mark_paid(payout, transfer_id):
    payout.status = 'paid'
    payout.stripe_transfer_id = transfer_id
    payout.error = None
    payout.completed_at = datetime.now(timezone.utc)
    for sub in payout.submissions:
        sub.paid_out = True
    db.session.commit()


def _mark_failed(payout, message):
    payout.status = 'failed'
    payout.error = message[:500]
    payout.completed_at = datetime.now(timezone.utc)
    for sub in payout.submissions:   # release: they roll into a later batch
        sub.payout_id = None
    db.session.commit()


def _send(payout, stripe_account_id):
    """Send (or re-send, with the same idempotency key) one transfer.
    Returns 'paid' | 'failed' | 'pending'."""
    try:
        transfer = stripe.Transfer.create(
            amount=payout.amount_cents,
            currency='usd',
            destination=stripe_account_id,
            idempotency_key=payout.idempotency_key,
            metadata={"payout_id": str(payout.id), "user_id": str(payout.user_id)},
        )
    except stripe.error.InvalidRequestError as e:
        logger.warning("Payout %s rejected by Stripe: %s", payout.id, e)
        _mark_failed(payout, getattr(e, 'user_message', None) or str(e))
        return 'failed'
    except Exception as e:  # network error, 5xx, rate limit: outcome unknown
        logger.exception("Payout %s outcome unknown", payout.id)
        payout.error = f"Outcome unknown, retry: {e}"[:500]
        db.session.commit()
        return 'pending'
    _mark_paid(payout, transfer.id)
    return 'paid'


def run_payout_batch(admin_user_id, only_user_id=None, now=None):
    _configure_stripe()
    now = now or datetime.now(timezone.utc)
    by_user = _unpaid_by_user(only_user_id)
    blocked, profiles = _blocked_reasons(list(by_user))
    planned, skipped = plan_payouts(by_user, blocked, now)
    batch_key = new_batch_key(now)
    result = {"batch_key": batch_key, "paid": [], "pending": [], "failed": [], "skipped": list(skipped)}

    for plan in planned:
        uid, ids = plan["user_id"], plan["submission_ids"]
        payout = ContractorPayout(
            user_id=uid, batch_key=batch_key, amount_cents=plan["amount_cents"], status='pending',
            idempotency_key=idempotency_key(batch_key, uid), created_by_user_id=admin_user_id)
        db.session.add(payout)
        db.session.flush()

        # Guarded claim: only rows nobody else has reserved or paid.
        claimed = TaskSubmission.query.filter(
            TaskSubmission.id.in_(ids), TaskSubmission.payout_id.is_(None),
            TaskSubmission.paid_out.is_(False),
        ).update({TaskSubmission.payout_id: payout.id}, synchronize_session=False)
        if claimed != len(ids):
            db.session.rollback()   # undo the claim and the payout row together
            result["skipped"].append({"user_id": uid, "amount_cents": plan["amount_cents"],
                                      "reason": "earnings changed while running - try again"})
            continue
        db.session.commit()          # reservation is durable before any money moves

        outcome = _send(payout, profiles[uid].stripe_account_id)
        result[outcome].append({"user_id": uid, "amount_cents": plan["amount_cents"], "payout_id": payout.id})
    return result


def retry_payout(payout_id):
    """Re-send a pending payout with its original idempotency key."""
    _configure_stripe()
    payout = db.session.get(ContractorPayout, payout_id)
    if payout is None or payout.status != 'pending':
        raise PayoutError("Only a pending payout can be retried.")
    profile = ContractorProfile.query.filter_by(user_id=payout.user_id).first()
    if profile is None or not profile.stripe_account_id or not profile.payout_enabled:
        raise PayoutError("This contractor's payout setup is incomplete.")
    if profile.payouts_frozen:
        raise PayoutError("This contractor's payouts are frozen.")
    return _send(payout, profile.stripe_account_id)
