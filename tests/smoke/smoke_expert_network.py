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
from website.candidate import candidate
from website.views import views
import io
from unittest import mock
import credential_storage

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
for bp in (earn, admin_bp, data_market, candidate, views):
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


# ---- credential document uploads (storage is faked; no Supabase call is made) ----
store = {}
deleted = []


def fake_upload(user_id, data, ext, mime):
    path = f"{user_id}/doc{len(store)}.{ext}"
    store[path] = (data, mime)
    return path


def fake_delete(path):
    deleted.append(path)
    store.pop(path, None)
    return True


PDF = b"%PDF-1.4 fake licence"
form = {"domain": "Finance", "credential_type": "certification", "title": "CFA charterholder", "issuer": "CFA Institute"}
login(c, w2id)
with mock.patch.object(credential_storage, "upload_credential_file", side_effect=fake_upload) as up, \
        mock.patch.object(credential_storage, "delete_credential_file", side_effect=fake_delete), \
        mock.patch.object(credential_storage, "signed_credential_url", return_value="https://signed.test/x?t=1"):
    with app.app_context():
        n0 = ExpertCredential.query.count()
    c.post("/earn/verification/credential", data=dict(form, evidence=(io.BytesIO(b"<html><script>x</script>"), "licence.pdf")),
           content_type="multipart/form-data")
    with app.app_context():
        check("HTML disguised as .pdf rejected, nothing stored", ExpertCredential.query.count() == n0 and not store)
    c.post("/earn/verification/credential", data=dict(form, evidence=(io.BytesIO(PDF + b"0" * (5 * 1024 * 1024)), "big.pdf")),
           content_type="multipart/form-data")
    with app.app_context():
        check("oversized document rejected", ExpertCredential.query.count() == n0 and not store)
    c.post("/earn/verification/credential", data=form)
    with app.app_context():
        check("no number/link/document still rejected", ExpertCredential.query.count() == n0)

    r = c.post("/earn/verification/credential", data=dict(form, evidence=(io.BytesIO(PDF), "x.pdf")),
               content_type="multipart/form-data")
    with app.app_context():
        cred = ExpertCredential.query.filter_by(user_id=w2id).one()
        check("document alone is enough; stored privately by content type",
              cred.evidence_path and len(store) == 1 and up.call_args.args[2:] == ("pdf", "application/pdf"), up.call_args)
        doc_cred_id, doc_path = cred.id, cred.evidence_path
    check("evidence path is per-user, unguessable", doc_path.startswith(f"{w2id}/") and doc_path.endswith(".pdf"))

    with mock.patch.object(credential_storage, "upload_credential_file", side_effect=RuntimeError("storage down")):
        c.post("/earn/verification/credential", data=dict(form, title="Other", evidence=(io.BytesIO(PDF), "y.pdf")),
               content_type="multipart/form-data")
    with app.app_context():
        check("storage failure creates no credential row", ExpertCredential.query.filter_by(title="Other").count() == 0)

    check("non-admin cannot open documents", c.get(f"/admin/experts/credentials/{doc_cred_id}/document").status_code == 403)
    login(c, aid)
    r = c.get(f"/admin/experts/credentials/{doc_cred_id}/document")
    check("admin gets a signed redirect, not cached",
          r.status_code == 302 and r.headers["Location"].startswith("https://signed.test/") and "no-store" in r.headers["Cache-Control"])
    check("pending page offers the document link", b"view document" in c.get("/admin/experts").data)
    with app.app_context():
        nodoc = ExpertCredential(user_id=w2id, domain="Legal", credential_type="license", title="n", issuer="i",
                                 license_number="9")
        db.session.add(nodoc); db.session.commit(); nodoc_id = nodoc.id
    check("credential with no document has none to open", c.get(f"/admin/experts/credentials/{nodoc_id}/document").status_code == 404)

    c.post(f"/admin/experts/credentials/{doc_cred_id}/review", data={"decision": "approve", "note": "verified"})
    with app.app_context():
        cred = db.session.get(ExpertCredential, doc_cred_id)
        check("document deleted once reviewed", cred.evidence_path is None and doc_path in deleted and not store)

    # If the delete fails the path must be kept so it can be retried
    login(c, w2id)
    c.post("/earn/verification/credential", data=dict(form, title="Again", evidence=(io.BytesIO(PDF), "z.pdf")),
           content_type="multipart/form-data")
    with app.app_context():
        again = ExpertCredential.query.filter_by(title="Again").one(); again_id, again_path = again.id, again.evidence_path
    login(c, aid)
    with mock.patch.object(credential_storage, "delete_credential_file", return_value=False):
        c.post(f"/admin/experts/credentials/{again_id}/review", data={"decision": "reject", "note": "illegible"})
    with app.app_context():
        check("failed delete keeps the path for retry", db.session.get(ExpertCredential, again_id).evidence_path == again_path)

    # contractor removing a pending credential deletes its file
    login(c, w2id)
    c.post("/earn/verification/credential", data=dict(form, title="Removable", evidence=(io.BytesIO(PDF), "r.pdf")),
           content_type="multipart/form-data")
    with app.app_context():
        rem = ExpertCredential.query.filter_by(title="Removable").one(); rem_id, rem_path = rem.id, rem.evidence_path
    c.post(f"/earn/verification/credential/{rem_id}/delete")
    check("removing a credential deletes its file", rem_path in deleted)

    # account deletion purges credentials + files and unlists the contractor
    with app.app_context():
        u = db.session.get(User, w2id); u.role = "candidate"
        pr = ContractorProfile.query.filter_by(user_id=w2id).first(); pr.listed_in_directory = True
        keep = ExpertCredential(user_id=w2id, domain="Legal", credential_type="license", title="k", issuer="i",
                                evidence_path=f"{w2id}/keep.pdf")
        db.session.add(keep); db.session.commit()
    r = c.post("/account/delete", data={"confirm": "delete my account"})
    with app.app_context():
        check("account deletion removes credentials", ExpertCredential.query.filter_by(user_id=w2id).count() == 0)
        check("account deletion unlists from directory", not ContractorProfile.query.filter_by(user_id=w2id).first().listed_in_directory)
    check("account deletion deletes stored documents", f"{w2id}/keep.pdf" in deleted)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
