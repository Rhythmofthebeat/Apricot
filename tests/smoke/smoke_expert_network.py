"""Run with the project venv:  .venv/bin/python tests/smoke/smoke_expert_network.py

End-to-end smoke test of the verified-expert network (consent, tiers, assessment,
credential review, exports, buyer directory/requests/pricing) against a THROWAWAY
in-memory SQLite DB. Builds its own Flask app and never calls create_app, so it
cannot reach DATABASE_URL / production. It asserts the DB URI before doing anything."""
import json
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, REPO)

import jinja2
from flask import Flask
from flask_login import LoginManager

from website import db
from website.models import (User, ContractorProfile, EarnTask, TaskSubmission, ExpertCredential,
                            DataBuyer, DataRequest)
from website.earn import earn
from website.admin import admin_bp
from website.data_market import data_market

URI = "sqlite:///:memory:"
app = Flask(__name__, template_folder=REPO + "/website/templates")
app.config.update(SECRET_KEY="test-secret", SQLALCHEMY_DATABASE_URI=URI, TESTING=True,
                  SERVER_NAME="localhost", PUBLIC_BASE_URL="http://localhost")
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
    assert str(db.engine.url) == URI, db.engine.url
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


with app.app_context():
    worker, worker2, admin, buyer_user = (User(email="w@x.com", first_name="W"), User(email="w2@x.com"),
                                          User(email="a@x.com", is_admin=True), User(email="b@x.com"))
    db.session.add_all([worker, worker2, admin, buyer_user])
    db.session.commit()
    wid, w2id, aid, bid = worker.id, worker2.id, admin.id, buyer_user.id
c = app.test_client()

# consent gates onboarding; legacy contractors are re-prompted
login(c, wid)
c.post("/earn/onboard", data={"domains": ["Legal"]})
with app.app_context():
    check("no profile without consent", ContractorProfile.query.filter_by(user_id=wid).first() is None)
check("onboard with consent redirects", c.post("/earn/onboard", data={"domains": ["Legal"], "training_consent": "1"}).status_code == 302)
login(c, w2id)
with app.app_context():
    db.session.add(ContractorProfile(user_id=w2id, domains=json.dumps(["Legal"])))
    db.session.commit()
r = c.get("/earn")
check("legacy contractor sent to consent", r.status_code == 302 and "/earn/consent" in r.headers["Location"])
c.post("/earn/consent", data={"training_consent": "1"})

# tasks, tiers, assessment
with app.app_context():
    cal = [EarnTask(title=f"cal{i}", description="d", task_type="ranking", option_a="a", option_b="b",
                    is_assessment=True, gold_answer="A", pay_cents=100, min_responses=1) for i in range(5)]
    expert_task = EarnTask(title="expert legal", description="d", task_type="ranking", option_a="a", option_b="b",
                           min_tier="expert", required_domains=json.dumps(["Legal"]), pay_cents=900, min_responses=1)
    normal = EarnTask(title="normal", description="d", task_type="ranking", option_a="a", option_b="b",
                      pay_cents=200, min_responses=1)
    db.session.add_all(cal + [expert_task, normal])
    db.session.commit()
    cal_ids, expert_id, normal_id = [t.id for t in cal], expert_task.id, normal.id

login(c, wid)
r = c.get("/earn")
check("task list renders; expert task hidden", r.status_code == 200 and b"expert legal" not in r.data)
check("expert task redirects to verification", "/earn/verification" in c.get(f"/earn/task/{expert_id}").headers["Location"])
check("verification page renders", c.get("/earn/verification").status_code == 200)
for i, tid in enumerate(cal_ids):
    c.post(f"/earn/task/{tid}", data={"response": "A" if i < 4 else "B"})
with app.app_context():
    p = ContractorProfile.query.filter_by(user_id=wid).first()
    check("assessment graded 4/5, quality untouched", (p.assessment_correct, p.assessment_attempts, p.quality_score) == (4, 5, 1.0))

# credentials: validation, review, unlock
bad = {"domain": "Legal", "credential_type": "license", "title": "Attorney", "issuer": "State Bar"}
c.post("/earn/verification/credential", data=bad)
c.post("/earn/verification/credential", data=dict(bad, license_number="1", verification_url="javascript:alert(1)"))
with app.app_context():
    check("credential needs number/url and a safe link", ExpertCredential.query.count() == 0)
c.post("/earn/verification/credential", data=dict(bad, license_number="12345", verification_url="https://example.com/x"))
with app.app_context():
    cred_id = ExpertCredential.query.one().id
check("pending credential does not unlock expert task", c.get(f"/earn/task/{expert_id}").status_code == 302)
check("non-admin cannot review", c.post(f"/admin/experts/credentials/{cred_id}/review", data={"decision": "approve"}).status_code == 403)
login(c, aid)
c.post(f"/admin/experts/credentials/{cred_id}/review", data={"decision": "approve", "note": "ok"})
with app.app_context():
    ContractorProfile.query.filter_by(user_id=wid).first().payout_enabled = True
    db.session.commit()
login(c, wid)
check("expert task visible after all three checks", b"expert legal" in c.get("/earn").data)
check("expert completes expert task", "/earn/verification" not in c.post(f"/earn/task/{expert_id}", data={"response": "A"}).headers["Location"])

# admin task form validation
login(c, aid)
check("admin task form renders", c.get("/admin/tasks/new").status_code == 200)
r = c.post("/admin/tasks/new", data={"title": "t", "description": "d", "task_type": "writing", "is_assessment": "1",
                                     "gold_answer": "x", "pay_dollars": "5"})
check("writing assessment refused (re-render, no crash)", r.status_code == 200)

# export: consent + assessment filtering
with app.app_context():
    nc = User(email="nc@x.com")
    db.session.add(nc); db.session.commit()
    db.session.add(ContractorProfile(user_id=nc.id, domains=json.dumps(["Legal"])))
    db.session.add_all([TaskSubmission(task_id=normal_id, user_id=u, response="A") for u in (wid, w2id, nc.id)])
    db.session.commit()
    from website.dataset_export import build_bulk_export
    check("export excludes non-consenting + assessment", len(build_bulk_export(task_type="ranking")) == 3)

# buyer side
anon = app.test_client()
check("pricing page public", anon.get("/data/pricing").status_code == 200)
check("project quote renders", b"per item" in anon.get("/data/pricing?mode=project&task_type=ranking&tier=expert&units=2000&responses=3").data)
check("bad quote input is an error, not a 500", b"whole numbers" in anon.get("/data/pricing?mode=project&units=abc").data)
check("pricing hides contractor cost", b"contractor cost" not in anon.get("/data/pricing").data.lower())
login(c, bid)
check("non-buyer blocked from directory", c.get("/data/experts").status_code == 403)
c.post("/data/signup", data={"name": "Acme", "contact_email": "b@x.com"})
login(c, wid)
c.post("/earn/verification/listing", data={"listed": "1"})
login(c, bid)
r = c.get("/data/experts")
check("directory lists anonymous expert, no PII", b"EX-" in r.data and b"w@x.com" not in r.data)
check("directory domain filter excludes", b"EX-" not in c.get("/data/experts?domain=Healthcare&tier=expert").data)
c.post("/data/requests", data={"title": "Rank", "task_type": "ranking", "tier": "expert", "domain": "Legal",
                               "units": "3000", "responses_per_unit": "3"})
c.post("/data/requests", data={"title": "x", "task_type": "ranking", "tier": "platinum", "units": "5"})
with app.app_context():
    check("request saved with estimate; invalid tier rejected",
          DataRequest.query.count() == 1 and DataRequest.query.one().estimated_cents > 0)
    rid = DataRequest.query.one().id
login(c, aid)
c.post(f"/admin/data-requests/{rid}/update", data={"status": "quoted", "quoted_dollars": ""})
c.post(f"/admin/data-requests/{rid}/update", data={"status": "quoted", "quoted_dollars": "12500.50"})
with app.app_context():
    check("admin quotes request (and refuses a quote with no amount)",
          DataRequest.query.get(rid).quoted_cents == 1250050)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
