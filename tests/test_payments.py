import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

# Load the pure modules directly: importing the `website` package would build the
# Flask app and connect to whatever DATABASE_URL points at.
_WEBSITE = Path(__file__).resolve().parent.parent / "website"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _WEBSITE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


po = _load("payouts")
bb = _load("buyer_billing")

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def sub(id, cents, days_old, **kw):
    base = dict(id=id, earnings_cents=cents, paid_out=False, payout_id=None, payout_hold=False,
                created_at=NOW - timedelta(days=days_old))
    base.update(kw)
    return NS(**base)


class PayableTest(unittest.TestCase):
    def test_hold_period(self):
        self.assertFalse(po.is_payable(sub(1, 500, 6), NOW))
        self.assertTrue(po.is_payable(sub(1, 500, 7), NOW))

    def test_excludes_paid_reserved_held_and_zero(self):
        for s in (sub(1, 500, 30, paid_out=True), sub(2, 500, 30, payout_id=9),
                  sub(3, 500, 30, payout_hold=True), sub(4, 0, 30)):
            self.assertFalse(po.is_payable(s, NOW))

    def test_naive_datetimes_treated_as_utc(self):
        naive = sub(1, 500, 10)
        naive.created_at = naive.created_at.replace(tzinfo=None)
        self.assertTrue(po.is_payable(naive, NOW))

    def test_summary_buckets_and_next_release(self):
        info = po.summarize_user([sub(1, 1000, 10), sub(2, 300, 2), sub(3, 200, 30, payout_hold=True),
                                  sub(4, 999, 30, paid_out=True)], NOW)
        self.assertEqual(info["payable_cents"], 1000)
        self.assertEqual(info["in_hold_cents"], 300)
        self.assertEqual(info["held_cents"], 200)
        self.assertEqual(info["next_release"], NOW + timedelta(days=5))


class PlanTest(unittest.TestCase):
    def test_plans_per_user_and_only_payable_submissions(self):
        subs = {1: [sub(1, 1500, 10), sub(2, 800, 10), sub(3, 5000, 1)]}
        planned, skipped = po.plan_payouts(subs, {}, NOW)
        self.assertEqual(planned, [{"user_id": 1, "amount_cents": 2300, "submission_ids": [1, 2]}])
        self.assertEqual(skipped, [])

    def test_below_minimum_carries_over(self):
        planned, skipped = po.plan_payouts({1: [sub(1, 1999, 10)]}, {}, NOW)
        self.assertEqual(planned, [])
        self.assertIn("minimum", skipped[0]["reason"])

    def test_blocked_users_skipped_with_reason(self):
        planned, skipped = po.plan_payouts({1: [sub(1, 5000, 10)]}, {1: "payouts frozen"}, NOW)
        self.assertEqual(planned, [])
        self.assertEqual(skipped[0]["reason"], "payouts frozen")

    def test_minimum_never_below_stripe_floor(self):
        planned, _ = po.plan_payouts({1: [sub(1, 10, 10)]}, {}, NOW, min_cents=0)
        self.assertEqual(planned, [])

    def test_nothing_payable_produces_nothing(self):
        planned, skipped = po.plan_payouts({1: [sub(1, 5000, 1)]}, {}, NOW)
        self.assertEqual((planned, skipped), ([], []))

    def test_idempotency_key_is_stable_and_distinct(self):
        self.assertEqual(po.idempotency_key("b1", 7), po.idempotency_key("b1", 7))
        self.assertNotEqual(po.idempotency_key("b1", 7), po.idempotency_key("b1", 8))
        self.assertNotEqual(po.idempotency_key("b1", 7), po.idempotency_key("b2", 7))


class BatchKeyTest(unittest.TestCase):
    def test_same_second_runs_get_distinct_keys(self):
        keys = {po.new_batch_key(NOW) for _ in range(200)}
        self.assertEqual(len(keys), 200)
        self.assertTrue(all(len(po.idempotency_key(k, 12345)) < 100 and len(k) <= 40 for k in keys))


class MilestoneTest(unittest.TestCase):
    def test_split_sums_to_total(self):
        for total in (1, 2, 3, 99_999, 1_250_050):
            d, f = bb.split_milestones(total)
            self.assertEqual(d + f, total)
        self.assertEqual(bb.split_milestones(1000), (500, 500))

    def test_split_rejects_bad_totals(self):
        for bad in (0, -5, 1.5, True, "100"):
            with self.assertRaises(ValueError):
                bb.split_milestones(bad)

    def test_milestone_ordering(self):
        ok = lambda *a: bb.milestone_due(*a)[0]
        self.assertFalse(ok("deposit", "quoted", False, False))
        self.assertTrue(ok("deposit", "accepted", False, False))
        self.assertFalse(ok("deposit", "accepted", True, False))
        self.assertFalse(ok("final", "in_progress", True, False))
        self.assertFalse(ok("final", "delivered", False, False))
        self.assertTrue(ok("final", "delivered", True, False))
        self.assertFalse(ok("final", "delivered", True, True))
        self.assertFalse(ok("bogus", "accepted", False, False))


class DollarParsingTest(unittest.TestCase):
    def test_parses(self):
        self.assertEqual(bb.parse_dollars("1,250.50"), 125050)
        self.assertEqual(bb.parse_dollars("$500"), 50000)

    def test_rejects(self):
        for raw, kw in (("abc", {}), ("", {}), ("nan", {}), ("inf", {}), ("-5", {}), ("0", {}),
                        ("50", {"minimum_cents": 10_000}), ("99999999", {"maximum_cents": 5_000_000})):
            with self.assertRaises(ValueError, msg=raw):
                bb.parse_dollars(raw, **kw)


class BalanceTest(unittest.TestCase):
    def test_balance_from_ledger(self):
        entries = [NS(amount_cents=50_000), NS(amount_cents=-20_000), NS(amount_cents=5_000)]
        self.assertEqual(bb.credit_balance(entries), 35_000)
        self.assertEqual(bb.credit_balance([]), 0)

    def test_reference_stable(self):
        self.assertEqual(bb.milestone_reference(4, "deposit"), "request:4:deposit")


if __name__ == "__main__":
    unittest.main()
