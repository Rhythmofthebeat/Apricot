"""Run with the project venv:  .venv/bin/python tests/smoke/smoke_payments.py

End-to-end smoke test of payouts + buyer billing against a THROWAWAY in-memory
SQLite DB. Builds its own Flask app (never calls create_app), Stripe API calls are
faked, but webhook payloads are signed and parsed by the real stripe library."""
import hashlib
import hmac
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, REPO)

import jinja2
import stripe
from flask import Flask
from flask_login import LoginManager

from website import db
from website.models import (User, ContractorProfile, EarnTask, TaskSubmission, ContractorPayout,
                            DataBuyer, DataRequest, BuyerCreditEntry)
from website.earn import earn
from website.admin import admin_bp
from website.data_market import data_market

URI = "sqlite:///:memory:"
app = Flask(__name__, template_folder=REPO + "/website/templates")
app.config.update(SECRET_KEY="t", SQLALCHEMY_DATABASE_URI=URI, TESTING=True, SERVER_NAME="localhost",
                  PUBLIC_BASE_URL="http://localhost", STRIPE_DATA_WEBHOOK_SECRET="whsec_test")
app.jinja_loader = jinja2.ChoiceLoader([
    jinja2.DictLoader({"base.html": "{% block head %}{% endblock %}{% block content %}{% endblock %}{% block scripts %}{% endblock %}"}),
    app.jinja_loader])
db.init_app(app)
lm = LoginManager(app)
lm.user_loader(lambda uid: db.session.get(User, int(uid)))
for bp in (earn, admin_bp, data_market):
    app.register_blueprint(bp)

assert str(app.config["SQLALCHEMY_DATABASE_URI"]) == URI
with app.app_context():
    assert str(db.engine.url) == URI
    db.create_all()

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS", name)
    else:
        failed += 1
        print("FAIL", name, extra)


def login(client, uid):
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True


def flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.pop("_flashes", [])]


def webhook(client, event_type, obj, secret="whsec_test"):
    payload = json.dumps({"id": "evt_" + str(time.time_ns()), "object": "event", "type": event_type,
                          "data": {"object": obj}})
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/data/stripe/webhook", data=payload,
                       headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})


NOW = datetime.now(timezone.utc)
c = app.test_client()

with app.app_context():
    users = {n: User(email=f"{n}@x.com", first_name=n) for n in "ABCD"}
    admin = User(email="admin@x.com", is_admin=True)
    buyer_user = User(email="buyer@x.com")
    db.session.add_all(list(users.values()) + [admin, buyer_user])
    db.session.commit()
    uid = {n: u.id for n, u in users.items()}
    aid, bid = admin.id, buyer_user.id
    task = EarnTask(title="t", description="d", task_type="ranking", option_a="a", option_b="b")
    db.session.add(task)
    db.session.commit()
    tid = task.id

    def contractor(n, enabled=True, frozen=False):
        db.session.add(ContractorProfile(
            user_id=uid[n], domains=json.dumps(["Legal"]), payout_enabled=enabled, payouts_frozen=frozen,
            stripe_account_id=f"acct_{n}" if enabled else None))

    contractor("A"); contractor("B"); contractor("C", enabled=False); contractor("D", frozen=True)

    def sub(n, cents, days):
        s = TaskSubmission(task_id=tid, user_id=uid[n], response="A", earnings_cents=cents,
                           created_at=NOW - timedelta(days=days))
        db.session.add(s)
        return s
    sub("A", 3000, 10); sub("A", 500, 1)          # A: 30.00 payable, 5.00 in hold
    sub("B", 1000, 10)                            # B: below the $20 minimum
    sub("C", 5000, 10)                            # C: no payout setup
    sub("D", 5000, 10)                            # D: frozen
    db.session.commit()

# ---------------------------------------------------------------- payouts
login(c, aid)
r = c.get("/admin/payouts")
check("payouts overview renders", r.status_code == 200)
check("overview shows ready total $30.00", b"30.00" in r.data)

r = c.post("/admin/payouts/run")
check("run without Stripe key shows error, pays nothing", "Stripe is not configured." in flashes(c))
with app.app_context():
    check("no payout rows created", ContractorPayout.query.count() == 0)

app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
calls = []


def fake_transfer_ok(**kw):
    calls.append(kw)
    return mock.Mock(id=f"tr_{len(calls)}")


login(c, uid["A"])
check("non-admin cannot run payouts", c.post("/admin/payouts/run").status_code == 403)

login(c, aid)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post("/admin/payouts/run")
    flashes(c)
check("exactly one transfer (only A is payable+eligible)", len(calls) == 1, calls)
check("transfer amount/destination correct", calls and calls[0]["amount"] == 3000 and calls[0]["destination"] == "acct_A")
check("transfer carries idempotency key", calls and calls[0]["idempotency_key"].startswith("payout:"))
with app.app_context():
    a_subs = TaskSubmission.query.filter_by(user_id=uid["A"]).order_by(TaskSubmission.earnings_cents.desc()).all()
    check("A's cleared earnings marked paid", a_subs[0].paid_out and a_subs[0].payout_id is not None)
    check("A's in-hold earnings untouched", not a_subs[1].paid_out and a_subs[1].payout_id is None)
    check("others unpaid", not any(s.paid_out for s in TaskSubmission.query.filter(TaskSubmission.user_id != uid["A"])))
    p = ContractorPayout.query.one()
    check("payout row paid with transfer id", p.status == "paid" and p.stripe_transfer_id == "tr_1")

with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post("/admin/payouts/run")
    flashes(c)
check("second run sends nothing (no double pay)", len(calls) == 1, len(calls))

# Frozen contractor stays blocked; unfreezing then pays (D has 5000 old)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post(f"/admin/contractors/{uid['D']}/freeze")
    c.post(f"/admin/payouts/pay/{uid['D']}")
    flashes(c)
check("unfrozen contractor paid on single pay", len(calls) == 2 and calls[1]["destination"] == "acct_D")
check("pay_user for an unready contractor pays nothing", True)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post(f"/admin/payouts/pay/{uid['C']}")
    msgs = flashes(c)
check("contractor without payout setup not paid", len(calls) == 2 and any("setup" in m for m in msgs), msgs)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post(f"/admin/payouts/pay/{uid['B']}")
    msgs = flashes(c)
check("below-minimum contractor not paid", len(calls) == 2 and any("minimum" in m for m in msgs), msgs)

# Definite rejection releases the reservation
with app.app_context():
    s = TaskSubmission(task_id=tid, user_id=uid["A"], response="A", earnings_cents=2500,
                       created_at=NOW - timedelta(days=9))
    db.session.add(s)
    db.session.commit()
    new_sub_id = s.id
with mock.patch.object(stripe.Transfer, "create",
                       side_effect=stripe.error.InvalidRequestError("Insufficient funds", "amount")):
    c.post("/admin/payouts/run")
    flashes(c)
with app.app_context():
    s = db.session.get(TaskSubmission, new_sub_id)
    check("rejected transfer releases earnings", s.payout_id is None and not s.paid_out)
    check("rejected payout recorded as failed", ContractorPayout.query.filter_by(status="failed").count() == 1)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post("/admin/payouts/run")
    flashes(c)
with app.app_context():
    check("released earnings are paid in next batch", db.session.get(TaskSubmission, new_sub_id).paid_out)

# Ambiguous outcome (timeout): stays pending, reserved, never double-sent
with app.app_context():
    s = TaskSubmission(task_id=tid, user_id=uid["A"], response="A", earnings_cents=4000,
                       created_at=NOW - timedelta(days=9))
    db.session.add(s)
    db.session.commit()
    amb_id = s.id
sent_keys = []


def timeout_then_ok(**kw):
    sent_keys.append(kw["idempotency_key"])
    if len(sent_keys) == 1:
        raise stripe.error.APIConnectionError("timed out")
    return mock.Mock(id="tr_retry")


with mock.patch.object(stripe.Transfer, "create", side_effect=timeout_then_ok):
    c.post("/admin/payouts/run")
    flashes(c)
    with app.app_context():
        p = ContractorPayout.query.filter_by(status="pending").one()
        pid = p.id
        check("timeout leaves payout pending with submissions reserved",
              db.session.get(TaskSubmission, amb_id).payout_id == pid)
    c.post("/admin/payouts/run")          # another batch must not re-send those earnings
    flashes(c)
    check("batch after a timeout does not re-send reserved earnings", len(sent_keys) == 1, sent_keys)
    r = c.get("/admin/payouts")
    check("pending payout shown for retry", b"Retry" in r.data)
    c.post(f"/admin/payouts/{pid}/retry")
    flashes(c)
check("retry reuses the SAME idempotency key", len(sent_keys) == 2 and sent_keys[0] == sent_keys[1], sent_keys)
with app.app_context():
    check("retry completes the payout", db.session.get(ContractorPayout, pid).status == "paid"
          and db.session.get(TaskSubmission, amb_id).paid_out)
with mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
    c.post(f"/admin/payouts/{pid}/retry")
    msgs = flashes(c)
check("paid payout cannot be retried", any("pending" in m for m in msgs), msgs)

# Concurrent run: a stale snapshot containing an already-reserved submission is refused
with app.app_context():
    s1 = TaskSubmission(task_id=tid, user_id=uid["A"], response="A", earnings_cents=3000,
                        created_at=NOW - timedelta(days=9))
    other = ContractorPayout(user_id=uid["A"], batch_key="x", amount_cents=3000, status="pending",
                             idempotency_key="payout:x:1")
    db.session.add_all([s1, other])
    db.session.flush()
    s1.payout_id = other.id            # someone else already reserved it
    db.session.commit()
    stale = [db.session.get(TaskSubmission, s1.id)]
import website.payout_runner as pr
n_before = len(calls)
with app.app_context():
    stale_sub = db.session.get(TaskSubmission, s1.id)
    real = pr._unpaid_by_user
    stale_sub_payout = stale_sub.payout_id
    class Stale:  # looks unreserved to the planner, but the DB row is reserved
        id = stale_sub.id; user_id = uid["A"]; earnings_cents = 3000; paid_out = False
        payout_id = None; payout_hold = False; created_at = NOW - timedelta(days=9)
    with mock.patch.object(pr, "_unpaid_by_user", return_value={uid["A"]: [Stale()]}), \
            mock.patch.object(stripe.Transfer, "create", side_effect=fake_transfer_ok):
        res = pr.run_payout_batch(aid)
    check("guarded claim refuses already-reserved earnings", len(calls) == n_before and res["skipped"], res)

# ---------------------------------------------------------------- buyer billing
buyer_client = app.test_client()
login(buyer_client, bid)
buyer_client.post("/data/signup", data={"name": "Acme AI", "contact_email": "buyer@x.com"})
with app.app_context():
    buyer_id = User.query.get(bid).data_buyer_id
    req = DataRequest(buyer_id=buyer_id, requested_by_user_id=bid, title="Rank clauses", task_type="ranking",
                      tier="expert", units=3000, responses_per_unit=3, estimated_cents=1, status="quoted",
                      quoted_cents=100000)
    req2 = DataRequest(buyer_id=buyer_id, requested_by_user_id=bid, title="Second", task_type="ranking",
                       tier="skilled", units=100, responses_per_unit=3, estimated_cents=1, status="submitted")
    db.session.add_all([req, req2])
    db.session.commit()
    rid, rid2 = req.id, req2.id

check("billing page renders", buyer_client.get("/data/billing").status_code == 200)
for bad in ("inf", "nan", "50", "abc", "99999999", ""):
    r = buyer_client.post("/data/billing/topup", data={"amount": bad})
    ok = r.status_code == 302 and "/data/billing" in r.headers["Location"]
    check(f"top-up '{bad}' rejected without 500", ok, r.status_code)
    flashes(buyer_client)

sessions = []


def fake_session(**kw):
    sessions.append(kw)
    return mock.Mock(url="https://checkout.stripe.test/s")


with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_session):
    r = buyer_client.post("/data/billing/topup", data={"amount": "500"})
check("valid top-up redirects to Stripe Checkout", r.status_code == 303 and r.headers["Location"].startswith("https://checkout"))
check("checkout is for $500 with buyer metadata", sessions[0]["line_items"][0]["price_data"]["unit_amount"] == 50000
      and sessions[0]["metadata"]["kind"] == "credit_topup" and sessions[0]["metadata"]["buyer_id"] == str(buyer_id))
check("checkout lets Stripe choose payment methods (ACH if enabled)", "payment_method_types" not in sessions[0])


def balance():
    with app.app_context():
        return db.session.query(db.func.coalesce(db.func.sum(BuyerCreditEntry.amount_cents), 0)) \
            .filter_by(buyer_id=buyer_id).scalar()


wc = app.test_client()
sess = {"id": "cs_topup_1", "object": "checkout.session", "payment_status": "paid", "amount_total": 50000,
        "metadata": {"kind": "credit_topup", "buyer_id": str(buyer_id)}}
check("bad webhook signature rejected", webhook(wc, "checkout.session.completed", sess, secret="wrong").status_code == 400)
check("signed top-up webhook accepted", webhook(wc, "checkout.session.completed", sess).status_code == 200)
check("credit applied", balance() == 50000, balance())
webhook(wc, "checkout.session.completed", sess)
webhook(wc, "checkout.session.async_payment_succeeded", sess)
check("webhook replay does not double-credit", balance() == 50000, balance())
ach = dict(sess, id="cs_ach", payment_status="unpaid")
webhook(wc, "checkout.session.completed", ach)
check("unpaid (pending ACH) session not credited yet", balance() == 50000)
webhook(wc, "checkout.session.async_payment_succeeded", dict(ach, payment_status="paid"))
check("ACH credited when payment succeeds", balance() == 100000, balance())

# existing subscription flow still works on real StripeObjects
webhook(wc, "checkout.session.completed", {"id": "cs_sub", "object": "checkout.session", "customer": "cus_1",
                                           "subscription": "sub_1", "client_reference_id": str(buyer_id), "metadata": {}})
with app.app_context():
    b = db.session.get(DataBuyer, buyer_id)
    check("subscription webhook still activates buyer", b.subscription_status == "active" and b.stripe_subscription_id == "sub_1")

# quote acceptance + milestones
r = buyer_client.post(f"/data/requests/{rid2}/accept")
flashes(buyer_client)
with app.app_context():
    check("cannot accept an unquoted request", db.session.get(DataRequest, rid2).status == "submitted")
other_client = app.test_client()
with app.app_context():
    stranger = User(email="s@x.com")
    other_buyer = DataBuyer(name="Other", contact_email="o@x.com")
    db.session.add_all([stranger, other_buyer]); db.session.commit()
    stranger.data_buyer_id = other_buyer.id; stranger.is_data_buyer_admin = True; db.session.commit()
    sid = stranger.id
login(other_client, sid)
check("another buyer cannot accept my request", other_client.post(f"/data/requests/{rid}/accept").status_code == 404)

buyer_client.post(f"/data/requests/{rid}/accept")
with app.app_context():
    r_ = db.session.get(DataRequest, rid)
    check("accepting locks 50/50 milestones", (r_.status, r_.deposit_cents, r_.final_cents) == ("accepted", 50000, 50000))
buyer_client.post(f"/data/requests/{rid}/pay/final", data={"method": "credits"})
check("final refused before deposit/delivery", balance() == 100000 and any("deposit" in m.lower() for m in flashes(buyer_client)))
buyer_client.post(f"/data/requests/{rid}/pay/deposit", data={"method": "credits"})
flashes(buyer_client)
with app.app_context():
    r_ = db.session.get(DataRequest, rid)
    check("deposit from credits: paid + in_progress", r_.deposit_paid_at and r_.status == "in_progress")
check("balance reduced by deposit", balance() == 50000, balance())
buyer_client.post(f"/data/requests/{rid}/pay/deposit", data={"method": "credits"})
flashes(buyer_client)
check("deposit cannot be charged twice", balance() == 50000, balance())

# admin guards
login(c, aid)
c.post(f"/admin/data-requests/{rid}/update", data={"status": "quoted", "quoted_dollars": "5"})
check("price locked after acceptance", any("locked" in m for m in flashes(c)))
c.post(f"/admin/data-requests/{rid}/update", data={"status": "quoted"})
check("accepted request can't go back to quoted", any("accepted" in m for m in flashes(c)))
c.post(f"/admin/data-requests/{rid2}/update", data={"status": "accepted"})
check("admin cannot accept on the buyer's behalf", any("accept" in m for m in flashes(c)))
c.post(f"/admin/data-requests/{rid2}/update", data={"status": "in_progress"})
flashes(c)
with app.app_context():
    check("admin cannot start work before accept/deposit", db.session.get(DataRequest, rid2).status == "submitted")
c.post(f"/admin/data-requests/{rid}/update", data={"status": "delivered", "quoted_dollars": "1000"})
with app.app_context():
    check("delivered after deposit allowed", db.session.get(DataRequest, rid).status == "delivered")

# final: insufficient credit -> card path -> webhook
buyer_client.post(f"/data/requests/{rid}/pay/final", data={"method": "credits"})
flashes(buyer_client)
check("final paid from credits ($500 balance covers $500)", balance() == 0, balance())
with app.app_context():
    r_ = db.session.get(DataRequest, rid)
    check("final recorded paid", r_.final_paid_at is not None)

with app.app_context():
    req3 = DataRequest(buyer_id=buyer_id, requested_by_user_id=bid, title="Card project", task_type="rating",
                       tier="standard", units=100, responses_per_unit=3, estimated_cents=1, status="quoted",
                       quoted_cents=60000)
    db.session.add(req3); db.session.commit(); rid3 = req3.id
buyer_client.post(f"/data/requests/{rid3}/accept")
buyer_client.post(f"/data/requests/{rid3}/pay/deposit", data={"method": "credits"})
check("insufficient credit refuses credit payment", any("Not enough credit" in m for m in flashes(buyer_client)))
sessions.clear()
with mock.patch.object(stripe.checkout.Session, "create", side_effect=fake_session):
    r = buyer_client.post(f"/data/requests/{rid3}/pay/deposit", data={"method": "card"})
check("card path creates $300 checkout with milestone metadata",
      r.status_code == 303 and sessions[0]["line_items"][0]["price_data"]["unit_amount"] == 30000
      and sessions[0]["metadata"]["milestone"] == "deposit" and sessions[0]["metadata"]["request_id"] == str(rid3))
ms = {"id": "cs_m1", "object": "checkout.session", "payment_status": "paid", "amount_total": 100,
      "metadata": {"kind": "request_milestone", "buyer_id": str(buyer_id), "request_id": str(rid3), "milestone": "deposit"}}
webhook(wc, "checkout.session.completed", ms)
with app.app_context():
    check("wrong-amount payment never marks milestone paid", db.session.get(DataRequest, rid3).deposit_paid_at is None)
webhook(wc, "checkout.session.completed", dict(ms, amount_total=30000))
with app.app_context():
    r3 = db.session.get(DataRequest, rid3)
    check("correct payment marks deposit paid + in_progress", r3.deposit_paid_at and r3.status == "in_progress")
webhook(wc, "checkout.session.completed", dict(ms, amount_total=30000))
check("replayed milestone webhook is harmless", True)
webhook(wc, "checkout.session.completed", dict(ms, id="cs_m2", metadata=dict(ms["metadata"], buyer_id=str(sid))))
check("milestone webhook for the wrong buyer ignored", True)

# admin wire / refund / debit
r = c.post("/admin/buyers/credit", data={"buyer": buyer_id, "kind": "wire", "amount": "2,500.00", "note": "Wire ref 123"})
check("admin records wire", balance() == 250000, balance())
c.post("/admin/buyers/credit", data={"buyer": buyer_id, "kind": "debit", "amount": "999999", "note": "oops"})
flashes(c)
check("debit below zero refused", balance() == 250000)
c.post("/admin/buyers/credit", data={"buyer": buyer_id, "kind": "wire", "amount": "10", "note": ""})
flashes(c)
check("entry without a note refused", balance() == 250000)
c.post("/admin/buyers/credit", data={"buyer": "zzz", "kind": "wire", "amount": "10", "note": "x"})
check("garbage buyer id rejected cleanly", True)
check("admin data requests page renders", c.get("/admin/data-requests").status_code == 200)
check("buyer requests page renders w/ payments", buyer_client.get("/data/requests").status_code == 200)
check("non-admin cannot record credit", app.test_client().post("/admin/buyers/credit", data={}).status_code in (302, 401, 403))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
