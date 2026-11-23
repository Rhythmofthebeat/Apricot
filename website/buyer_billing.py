"""Buyer-side billing rules. Pure functions - no Flask, DB or Stripe.

Buyers pay in one of two ways:
  * prepaid credits - the buyer tops up a balance (card/ACH/wire), and project
    milestones draw it down. We hold the cash before any contractor is paid.
  * card/ACH checkout for a single milestone.

Custom projects are billed in two milestones: a deposit when the buyer accepts
the quote (work starts once it is paid) and the remainder on delivery.
"""
import math

DEPOSIT_FRACTION = 0.5
MIN_TOPUP_CENTS = 10_000          # $100
MAX_TOPUP_CENTS = 5_000_000       # $50,000 - larger amounts should be invoiced/wired

MILESTONES = ("deposit", "final")


def split_milestones(total_cents):
    """(deposit, final) in cents; they always sum to the total."""
    if isinstance(total_cents, bool) or not isinstance(total_cents, int) or total_cents <= 0:
        raise ValueError("quote must be a positive number of cents")
    deposit = round(total_cents * DEPOSIT_FRACTION)
    return deposit, total_cents - deposit


def parse_dollars(raw, minimum_cents=1, maximum_cents=None):
    """Parse a user-entered dollar amount into whole cents, or raise ValueError."""
    try:
        text = str(raw).strip().replace(",", "").lstrip("$")
        value = float(text)
        if not math.isfinite(value):
            raise ValueError
        cents = round(value * 100)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Enter an amount in dollars, e.g. 500 or 1250.50.")
    if cents < minimum_cents:
        raise ValueError(f"The minimum is ${minimum_cents / 100:,.2f}.")
    if maximum_cents is not None and cents > maximum_cents:
        raise ValueError(f"The maximum online is ${maximum_cents / 100:,.2f}; contact us to pay by wire or invoice.")
    return cents


def credit_balance(entries):
    """Balance is always derived from the ledger, never stored."""
    return sum(e.amount_cents for e in entries)


def milestone_due(milestone, request_status, deposit_paid, final_paid):
    """Whether a milestone can be paid right now. Returns (ok, reason)."""
    if milestone not in MILESTONES:
        return False, "Unknown milestone."
    if milestone == "deposit":
        if deposit_paid:
            return False, "The deposit is already paid."
        if request_status != "accepted":
            return False, "Accept the quote before paying the deposit."
        return True, ""
    if final_paid:
        return False, "The final payment is already made."
    if not deposit_paid:
        return False, "Pay the deposit first."
    if request_status != "delivered":
        return False, "The final payment is due once the work is delivered."
    return True, ""


def milestone_reference(request_id, milestone):
    """Ledger idempotency key: a milestone can be charged to credits only once."""
    return f"request:{request_id}:{milestone}"
