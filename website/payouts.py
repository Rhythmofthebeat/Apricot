"""Contractor payout rules. Pure functions - no Flask, DB or Stripe.

Policy (all tunable below):
  * Earnings sit in a review hold for HOLD_DAYS before they become payable, so
    consensus scoring and fraud review can run first.
  * Payouts run in batches; one transfer per contractor per batch.
  * A contractor needs MIN_PAYOUT_CENTS payable before we send a transfer, to
    avoid per-transfer fees eating small amounts. Anything below carries over.
  * Frozen contractors, held submissions, and contractors without a verified
    Stripe account are never paid.
"""
import secrets
from datetime import datetime, timedelta, timezone

HOLD_DAYS = 7
MIN_PAYOUT_CENTS = 2000      # $20
STRIPE_MIN_TRANSFER_CENTS = 50


def _aware(dt):
    """Databases such as SQLite hand back naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def release_date(created_at, hold_days=HOLD_DAYS):
    created = _aware(created_at)
    return created + timedelta(days=hold_days) if created else None


def is_unpaid(sub):
    return (not sub.paid_out) and sub.payout_id is None and sub.earnings_cents > 0


def is_payable(sub, now, hold_days=HOLD_DAYS):
    if not is_unpaid(sub) or sub.payout_hold:
        return False
    released = release_date(sub.created_at, hold_days)
    return released is not None and released <= _aware(now)


def idempotency_key(batch_key, user_id):
    """Stable per (batch, contractor): retrying a payout re-sends the same key,
    so Stripe returns the original transfer instead of creating a second one."""
    return f"payout:{batch_key}:{user_id}"


def summarize_user(subs, now, hold_days=HOLD_DAYS):
    """Split one contractor's unpaid submissions into payable / in hold / held."""
    payable, in_hold, held = [], [], []
    for s in subs:
        if not is_unpaid(s):
            continue
        if s.payout_hold:
            held.append(s)
        elif is_payable(s, now, hold_days):
            payable.append(s)
        else:
            in_hold.append(s)
    next_release = min((release_date(s.created_at, hold_days) for s in in_hold), default=None)
    return {
        "payable": payable,
        "payable_cents": sum(s.earnings_cents for s in payable),
        "in_hold_cents": sum(s.earnings_cents for s in in_hold),
        "held_cents": sum(s.earnings_cents for s in held),
        "next_release": next_release,
    }


def plan_payouts(subs_by_user, blocked, now, min_cents=MIN_PAYOUT_CENTS, hold_days=HOLD_DAYS):
    """subs_by_user: {user_id: [submission, ...]}; blocked: {user_id: reason}
    for contractors who can't be paid right now (frozen, no verified payout
    account). Returns (planned, skipped): planned is a list of dicts
    {user_id, amount_cents, submission_ids}; skipped a list of
    {user_id, amount_cents, reason}."""
    min_cents = max(min_cents, STRIPE_MIN_TRANSFER_CENTS)
    planned, skipped = [], []
    for user_id in sorted(subs_by_user):
        info = summarize_user(subs_by_user[user_id], now, hold_days)
        amount = info["payable_cents"]
        if amount <= 0:
            continue
        if user_id in blocked:
            skipped.append({"user_id": user_id, "amount_cents": amount, "reason": blocked[user_id]})
        elif amount < min_cents:
            skipped.append({"user_id": user_id, "amount_cents": amount,
                            "reason": f"below the ${min_cents / 100:.0f} minimum - carries over"})
        else:
            planned.append({"user_id": user_id, "amount_cents": amount,
                            "submission_ids": [s.id for s in info["payable"]]})
    return planned, skipped


def new_batch_key(now=None):
    """Timestamp plus a random suffix: two runs in the same second (a double
    click) must still get different keys, or the second would collide with the
    first's idempotency key instead of being an independent batch."""
    now = _aware(now) if now else datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
