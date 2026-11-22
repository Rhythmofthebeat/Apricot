import importlib.util
import unittest
from pathlib import Path

# Load the pure modules directly: importing the `website` package would build the
# Flask app and connect to whatever DATABASE_URL points at.
_WEBSITE = Path(__file__).resolve().parent.parent / "website"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _WEBSITE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pricing = _load("pricing")
ev = _load("expert_verification")


class PricingTest(unittest.TestCase):
    def test_task_cost_follows_tier_hourly_rate(self):
        # 5-minute ranking at $20/h, $40/h, $100/h
        self.assertEqual(pricing.contractor_task_cost_cents("ranking", "standard"), 167)
        self.assertEqual(pricing.contractor_task_cost_cents("ranking", "skilled"), 333)
        self.assertEqual(pricing.contractor_task_cost_cents("ranking", "expert"), 833)

    def test_custom_quote_applies_markup_over_cost(self):
        q = pricing.quote_custom_project("writing", "expert", 1000, 3)
        self.assertGreater(q["price_cents"], q["contractor_cost_cents"])
        self.assertAlmostEqual(q["price_cents"] / q["contractor_cost_cents"], 1.8, places=2)
        self.assertFalse(q["minimum_applied"])

    def test_small_project_hits_minimum(self):
        q = pricing.quote_custom_project("rating", "standard", 10, 3)
        self.assertEqual(q["price_cents"], pricing.MIN_PROJECT_CENTS)
        self.assertTrue(q["minimum_applied"])

    def test_higher_tier_costs_more(self):
        prices = [pricing.quote_custom_project("factcheck", t, 5000, 3)["price_cents"]
                  for t in pricing.TIERS]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(len(set(prices)), 3)

    def test_dataset_exclusive_and_volume_discount(self):
        base = pricing.quote_dataset_license("ranking", 1000)
        excl = pricing.quote_dataset_license("ranking", 1000, exclusive=True)
        self.assertEqual(excl["price_cents"], base["price_cents"] * 3)
        big = pricing.quote_dataset_license("ranking", 100_000)
        self.assertEqual(big["volume_discount"], 0.20)
        self.assertLess(big["per_record_cents"], base["per_record_cents"])

    def test_rejects_bad_input(self):
        for call in (
            lambda: pricing.quote_custom_project("ranking", "platinum", 10),
            lambda: pricing.quote_custom_project("poetry", "standard", 10),
            lambda: pricing.quote_custom_project("ranking", "standard", 0),
            lambda: pricing.quote_custom_project("ranking", "standard", True),
            lambda: pricing.quote_custom_project("ranking", "standard", 10, 0),
            lambda: pricing.quote_dataset_license("ranking", -5),
            lambda: pricing.contractor_task_cost_cents("ranking", "standard", minutes=0),
        ):
            with self.assertRaises(ValueError):
                call()


class TierTest(unittest.TestCase):
    def _tier(self, **kw):
        base = dict(assessment_correct=0, assessment_attempts=0, quality_score=1.0,
                    tasks_completed=0, identity_verified=False, approved_credential_domains=[])
        base.update(kw)
        return ev.compute_tier(**base)

    def test_new_contractor_is_standard_despite_default_quality_of_one(self):
        self.assertEqual(self._tier(), "standard")

    def test_assessment_needs_minimum_attempts(self):
        self.assertEqual(self._tier(assessment_correct=3, assessment_attempts=3), "standard")
        self.assertEqual(self._tier(assessment_correct=5, assessment_attempts=5), "skilled")

    def test_assessment_needs_accuracy(self):
        self.assertEqual(self._tier(assessment_correct=3, assessment_attempts=5), "standard")
        self.assertEqual(self._tier(assessment_correct=4, assessment_attempts=5), "skilled")

    def test_track_record_alone_reaches_skilled(self):
        self.assertEqual(self._tier(tasks_completed=20, quality_score=0.85), "skilled")
        self.assertEqual(self._tier(tasks_completed=20, quality_score=0.5), "standard")

    def test_expert_needs_all_three_checks(self):
        passed = dict(assessment_correct=5, assessment_attempts=5)
        self.assertEqual(self._tier(**passed, identity_verified=True,
                                    approved_credential_domains=["Legal"]), "expert")
        self.assertEqual(self._tier(**passed, identity_verified=False,
                                    approved_credential_domains=["Legal"]), "skilled")
        self.assertEqual(self._tier(**passed, identity_verified=True,
                                    approved_credential_domains=[]), "skilled")
        self.assertEqual(self._tier(identity_verified=True,
                                    approved_credential_domains=["Legal"]), "standard")


class AccessTest(unittest.TestCase):
    def _can(self, **kw):
        base = dict(min_tier="standard", required_domains=[], tier="standard",
                    declared_domains=[], credential_domains=[])
        base.update(kw)
        return ev.can_access_task(**base)

    def test_open_task(self):
        self.assertTrue(self._can())

    def test_declared_domain_unlocks_standard_task(self):
        self.assertTrue(self._can(required_domains=["Legal"], declared_domains=["Legal"]))
        self.assertFalse(self._can(required_domains=["Legal"], declared_domains=["Software"]))

    def test_tier_floor(self):
        self.assertFalse(self._can(min_tier="skilled", tier="standard"))
        self.assertTrue(self._can(min_tier="skilled", tier="expert"))

    def test_self_declared_domain_cannot_unlock_expert_task(self):
        self.assertFalse(self._can(min_tier="expert", tier="expert", required_domains=["Legal"],
                                   declared_domains=["Legal"], credential_domains=[]))

    def test_credential_in_wrong_domain_cannot_unlock_expert_task(self):
        self.assertFalse(self._can(min_tier="expert", tier="expert", required_domains=["Legal"],
                                   credential_domains=["Healthcare"]))

    def test_matching_credential_unlocks_expert_task(self):
        self.assertTrue(self._can(min_tier="expert", tier="expert", required_domains=["Legal"],
                                  credential_domains=["Legal"]))

    def test_unrestricted_expert_task_needs_any_credential(self):
        self.assertFalse(self._can(min_tier="expert", tier="expert"))
        self.assertTrue(self._can(min_tier="expert", tier="expert", credential_domains=["Legal"]))


class GoldGradingTest(unittest.TestCase):
    def test_exact_match_case_insensitive(self):
        self.assertTrue(ev.grade_gold("ranking", " a ", "A"))
        self.assertFalse(ev.grade_gold("ranking", "B", "A"))

    def test_rating_tolerates_one_point(self):
        self.assertTrue(ev.grade_gold("rating", "4", "5"))
        self.assertFalse(ev.grade_gold("rating", "2", "5"))

    def test_ungradable(self):
        self.assertIsNone(ev.grade_gold("rating", "great", "5"))
        self.assertIsNone(ev.grade_gold("ranking", "A", None))
        self.assertIsNone(ev.grade_gold("ranking", "", "A"))


class DocumentTypeTest(unittest.TestCase):
    def test_detects_by_content(self):
        self.assertEqual(ev.detect_document_type(b"%PDF-1.7 ..."), ("pdf", "application/pdf"))
        self.assertEqual(ev.detect_document_type(b"\xff\xd8\xff\xe0abc"), ("jpg", "image/jpeg"))
        self.assertEqual(ev.detect_document_type(b"\x89PNG\r\n\x1a\nabc"), ("png", "image/png"))

    def test_rejects_everything_else(self):
        for data in (b"", None, b"<html><script>alert(1)</script>", b"MZ\x90\x00 exe", b"GIF89a", b"PK\x03\x04 zip",
                     b"  %PDF-1.4 leading space"):
            self.assertIsNone(ev.detect_document_type(data))


class PseudonymTest(unittest.TestCase):
    def test_stable_and_secret_dependent(self):
        self.assertEqual(ev.pseudonym(7, "s1"), ev.pseudonym(7, "s1"))
        self.assertNotEqual(ev.pseudonym(7, "s1"), ev.pseudonym(8, "s1"))
        self.assertNotEqual(ev.pseudonym(7, "s1"), ev.pseudonym(7, "s2"))

    def test_format(self):
        label = ev.pseudonym(7, "s1")
        self.assertRegex(label, r"^EX-[0-9A-F]{8}$")


if __name__ == "__main__":
    unittest.main()
