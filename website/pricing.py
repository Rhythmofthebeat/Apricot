"""Pricing for the AI-training data business. Pure functions, no Flask/DB.

Everything here is a STARTING ASSUMPTION to be tuned against real contractor
cost and buyer feedback, not a validated price list. The cost side is anchored
to the hourly ranges Mercor publishes for each expertise level (entry labeling
$12-25/h, skilled generalist $25-53/h, credentialed domain expert $75-200+/h);
the buyer price is contractor cost times a markup that covers QA, payment
fees, consensus overhead (several people per item) and margin.

Three ways a buyer pays:
  1. Custom project  - buyer specifies a task; priced per unit from contractor cost.
  2. Dataset license - pre-built consensus-checked data, priced per record.
  3. Expert sourcing - same as (1) but with a minimum tier of verified experts.
"""

TIERS = ("standard", "skilled", "expert")

TIER_LABELS = {
    "standard": "Standard",
    "skilled": "Skilled (assessed)",
    "expert": "Verified expert (credentialed)",
}

# What we pay contractors, per hour-equivalent, by verification tier.
HOURLY_COST_CENTS = {"standard": 2000, "skilled": 4000, "expert": 10000}

# Typical minutes of work per item, by task type.
TASK_MINUTES = {"ranking": 5, "rating": 4, "factcheck": 6, "writing": 15}

# Buyer price = contractor cost x markup. Experts carry a higher markup
# because credential verification and scarce supply cost more to maintain.
MARKUP = {"standard": 1.6, "skilled": 1.7, "expert": 1.8}

MIN_PROJECT_CENTS = 50_000          # $500 floor so tiny projects cover review overhead
MAX_UNITS = 1_000_000
MAX_RESPONSES_PER_UNIT = 20

# Dataset licenses: price per record at standard-tier quality.
DATASET_PER_RECORD_CENTS = {"ranking": 75, "rating": 50, "factcheck": 60, "writing": 300}
DATASET_TIER_MULTIPLIER = {"standard": 1.0, "skilled": 1.5, "expert": 3.0}
EXCLUSIVE_MULTIPLIER = 3.0
# (minimum records, discount) - first match wins, so list largest first.
VOLUME_DISCOUNTS = ((100_000, 0.20), (25_000, 0.12), (5_000, 0.05))


def _check_tier(tier):
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier!r}")


def _check_task_type(task_type):
    if task_type not in TASK_MINUTES:
        raise ValueError(f"unknown task type: {task_type!r}")


def _check_count(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def contractor_task_cost_cents(task_type, tier, minutes=None):
    """What one completed item costs us in contractor pay."""
    _check_task_type(task_type)
    _check_tier(tier)
    mins = TASK_MINUTES[task_type] if minutes is None else minutes
    if mins <= 0:
        raise ValueError("minutes must be positive")
    return round(HOURLY_COST_CENTS[tier] * mins / 60)


def suggested_task_pay_cents(task_type, tier="standard", minutes=None):
    """Pay to put on an EarnTask so contractors earn their tier's hourly rate."""
    return contractor_task_cost_cents(task_type, tier, minutes)


def quote_custom_project(task_type, tier, units, responses_per_unit=3):
    """Quote a custom data project. `units` items, each judged by
    `responses_per_unit` contractors so consensus can be computed."""
    _check_task_type(task_type)
    _check_tier(tier)
    _check_count(units, "units", MAX_UNITS)
    _check_count(responses_per_unit, "responses_per_unit", MAX_RESPONSES_PER_UNIT)

    per_response = contractor_task_cost_cents(task_type, tier)
    contractor_cost = per_response * units * responses_per_unit
    raw_price = round(contractor_cost * MARKUP[tier])
    price = max(raw_price, MIN_PROJECT_CENTS)
    return {
        "task_type": task_type,
        "tier": tier,
        "units": units,
        "responses_per_unit": responses_per_unit,
        "contractor_cost_cents": contractor_cost,
        "price_cents": price,
        "per_unit_cents": round(price / units),
        "minimum_applied": raw_price < MIN_PROJECT_CENTS,
    }


def quote_dataset_license(task_type, records, tier="standard", exclusive=False):
    """Quote a license to existing consensus-checked records."""
    _check_task_type(task_type)
    _check_tier(tier)
    _check_count(records, "records", MAX_UNITS)

    per_record = DATASET_PER_RECORD_CENTS[task_type] * DATASET_TIER_MULTIPLIER[tier]
    if exclusive:
        per_record *= EXCLUSIVE_MULTIPLIER
    discount = next((d for threshold, d in VOLUME_DISCOUNTS if records >= threshold), 0.0)
    price = round(per_record * records * (1 - discount))
    return {
        "task_type": task_type,
        "tier": tier,
        "records": records,
        "exclusive": bool(exclusive),
        "volume_discount": discount,
        "price_cents": price,
        "per_record_cents": round(price / records, 2),
    }


def format_usd(cents):
    return f"${cents / 100:,.2f}"
