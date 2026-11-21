"""Expert verification rules for the Earn contractor network. Pure functions.

Mirrors the three checks Mercor describes for onboarding: identity,
credentials, and a skills assessment.

  identity    - Stripe Connect Express onboarding (KYC) is complete
  credentials - an admin approved a licence/degree/certification in a domain
  assessment  - accuracy on gold-answer calibration tasks

Tiers:
  standard - anyone onboarded
  skilled  - passed the assessment, or has a long consistent track record
  expert   - identity + an approved credential + passed assessment
"""
import hashlib
import hmac

TIERS = ("standard", "skilled", "expert")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

CREDENTIAL_TYPES = ("license", "degree", "certification", "employment")
CREDENTIAL_STATUSES = ("pending", "approved", "rejected")

ASSESSMENT_MIN_ATTEMPTS = 5
ASSESSMENT_PASS_ACCURACY = 0.8
SKILLED_MIN_TASKS = 20
SKILLED_MIN_QUALITY = 0.8

# Bump when the contributor data-licence wording changes; contributors then
# have to accept again.
TRAINING_CONSENT_VERSION = "1"


def assessment_passed(correct, attempts):
    if attempts < ASSESSMENT_MIN_ATTEMPTS:
        return False
    return correct / attempts >= ASSESSMENT_PASS_ACCURACY


def compute_tier(*, assessment_correct, assessment_attempts, quality_score, tasks_completed,
                 identity_verified, approved_credential_domains):
    passed = assessment_passed(assessment_correct, assessment_attempts)
    if passed and identity_verified and approved_credential_domains:
        return "expert"
    if passed or (tasks_completed >= SKILLED_MIN_TASKS and quality_score >= SKILLED_MIN_QUALITY):
        return "skilled"
    return "standard"


def tier_at_least(tier, minimum):
    return TIER_RANK.get(tier, 0) >= TIER_RANK.get(minimum or "standard", 0)


def can_access_task(*, min_tier, required_domains, tier, declared_domains, credential_domains):
    """required_domains / declared_domains / credential_domains are collections
    of domain names; required_domains empty means open to all domains.

    Below expert level a declared (self-reported) domain is enough. Expert-tier
    tasks need a credential an admin actually approved in a matching domain, so
    a self-declared specialty can never unlock them."""
    if not tier_at_least(tier, min_tier):
        return False
    required = set(required_domains or ())
    if (min_tier or "standard") == "expert":
        return bool(set(credential_domains) & required) if required else bool(credential_domains)
    if not required:
        return True
    return bool(required & set(declared_domains))


def grade_gold(task_type, response, gold):
    """True/False for a calibration task, None if it can't be graded."""
    if response is None or gold is None:
        return None
    response, gold = str(response).strip(), str(gold).strip()
    if not response or not gold:
        return None
    if task_type == "rating":
        try:
            return abs(float(response) - float(gold)) <= 1.0
        except ValueError:
            return None
    return response.lower() == gold.lower()


def pseudonym(user_id, secret):
    """Stable anonymous label for a contractor, so buyers can refer to someone
    in a request without us revealing who they are."""
    digest = hmac.new(str(secret).encode(), f"expert:{user_id}".encode(), hashlib.sha256).hexdigest()
    return "EX-" + digest[:8].upper()
