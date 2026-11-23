"""Version-safe access to Stripe webhook objects.

Older stripe-python StripeObjects subclassed dict, so `obj.get(key)` worked.
Newer releases (requirements.txt only pins stripe>=9.0.0, so a fresh deploy gets
the latest) do not: `.get` raises AttributeError, which turned a webhook into a
500 and rolled back the work it was handling. Indexing works on every version.
"""


def sget(obj, key, default=None):
    try:
        value = obj[key]
    except (KeyError, AttributeError, TypeError):
        return default
    return default if value is None else value
