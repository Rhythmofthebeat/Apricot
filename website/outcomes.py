"""Hiring-outcome logging and the employer-facing data products built on it:
score calibration, platform benchmarks, retention check-ins and the applicant
CSV export.

The reports read the Application table directly, so they work on every
historical application today. ApplicationEvent only adds what Application
can't hold: *when* a decision happened and what the scores were at that
moment, plus 30/90-day retention check-ins.
"""
import csv
import io
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from . import db
from .models import Application, ApplicationEvent, Job, Company

logger = logging.getLogger(__name__)

# A benchmark is only shown when it's built from enough distinct companies
# that no single employer's numbers can be backed out of it.
MIN_BENCHMARK_COMPANIES = 5
MIN_BENCHMARK_APPLICATIONS = 30

# Decided applications needed in a score band before its hire rate is called
# reliable rather than anecdotal.
MIN_RELIABLE_BAND_SIZE = 5

SCORE_BANDS = [(0, 39, "0-39"), (40, 59, "40-59"), (60, 79, "60-79"), (80, 100, "80-100")]
CHECKIN_WINDOWS = {"30d": 30, "90d": 90}
CHECKIN_RESULTS = ("still_employed", "left")


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def composite_score(application):
    """The employer-facing composite (resume + interview + authenticity), or
    None when the application has no score of any kind yet."""
    from .employer import compute_composite_score  # lazy: employer imports this module's siblings

    if (application.match_score is None and application.interview_verdict is None
            and application.manual_interview_score is None):
        return None
    try:
        ai_data = json.loads(application.ai_likelihood) if application.ai_likelihood else None
    except (ValueError, TypeError):
        ai_data = None
    return compute_composite_score(application, ai_data)


# ------------------------------------------------------------------ #
#  Logging
# ------------------------------------------------------------------ #

def log_event(application, job, event_type, *, from_status=None, to_status=None,
              detail=None, checkin_window=None):
    """Stage an ApplicationEvent on the session (the caller's commit persists
    it). Never raises - outcome logging must not be able to break the hiring
    action it is recording."""
    try:
        db.session.add(ApplicationEvent(
            application_id=application.id,
            job_id=job.id,
            company_id=job.company_id,
            job_category=job.category,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            match_score=application.match_score,
            interview_verdict=application.interview_verdict,
            manual_interview_score=application.manual_interview_score,
            composite_score=composite_score(application),
            checkin_window=checkin_window,
            detail=detail,
        ))
    except Exception:
        logger.exception("Could not log %s event for application %s", event_type, getattr(application, 'id', None))


def record_retention_checkin(application, job, window, result):
    """Idempotent per (application, window): a second submission replaces the
    first rather than double-counting. Returns True when recorded."""
    if window not in CHECKIN_WINDOWS or result not in CHECKIN_RESULTS:
        return False
    ApplicationEvent.query.filter_by(
        application_id=application.id, event_type='retention_checkin', checkin_window=window,
    ).delete(synchronize_session=False)
    log_event(application, job, 'retention_checkin', detail=result, checkin_window=window)
    return True


# ------------------------------------------------------------------ #
#  Reports
# ------------------------------------------------------------------ #

def funnel(applications):
    return {
        "total": len(applications),
        "interviewed": sum(1 for a in applications if a.interview_transcript),
        "hired": sum(1 for a in applications if a.status == 'hired'),
        "rejected": sum(1 for a in applications if a.status == 'rejected'),
    }


def calibration(applications):
    """Hire rate per composite-score band, over applications that reached a
    final decision (hired or rejected). This is the number that says whether
    the AI score actually predicts this employer's hiring decisions."""
    bands = {label: {"band": label, "decided": 0, "hired": 0} for _, _, label in SCORE_BANDS}
    for a in applications:
        if a.status not in ('hired', 'rejected'):
            continue
        score = composite_score(a)
        if score is None:
            continue
        for lo, hi, label in SCORE_BANDS:
            if lo <= score <= hi:
                bands[label]["decided"] += 1
                if a.status == 'hired':
                    bands[label]["hired"] += 1
                break

    rows = []
    for _, _, label in SCORE_BANDS:
        row = bands[label]
        row["hire_rate"] = round(row["hired"] / row["decided"] * 100) if row["decided"] else None
        row["reliable"] = row["decided"] >= MIN_RELIABLE_BAND_SIZE
        rows.append(row)

    reliable = [r for r in rows if r["reliable"]]
    # Predictive only if the highest reliable band out-hires the lowest one;
    # with fewer than two reliable bands there is nothing to compare yet.
    predictive = None
    if len(reliable) >= 2:
        predictive = reliable[-1]["hire_rate"] > reliable[0]["hire_rate"]
    return {"bands": rows, "predictive": predictive, "decided": sum(r["decided"] for r in rows)}


def median_days_to_hire(company_id):
    """Median days from application to the hired decision, from logged
    events (so only hires made since outcome logging shipped are counted)."""
    events = (
        ApplicationEvent.query
        .filter_by(company_id=company_id, event_type='status_changed', to_status='hired')
        .all()
    )
    if not events:
        return None
    app_ids = [e.application_id for e in events]
    created = {a.id: a.created_at for a in Application.query.filter(Application.id.in_(app_ids)).all()}
    days = []
    for e in events:
        start, end = _utc(created.get(e.application_id)), _utc(e.created_at)
        if start and end and end >= start:
            days.append((end - start).total_seconds() / 86400)
    return round(statistics.median(days), 1) if days else None


def retention_summary(company_id):
    out = {}
    for window in CHECKIN_WINDOWS:
        rows = (
            db.session.query(ApplicationEvent.detail, func.count(ApplicationEvent.id))
            .filter(
                ApplicationEvent.company_id == company_id,
                ApplicationEvent.event_type == 'retention_checkin',
                ApplicationEvent.checkin_window == window,
            )
            .group_by(ApplicationEvent.detail)
            .all()
        )
        counts = dict(rows)
        total = sum(counts.values())
        out[window] = {
            "total": total,
            "still_employed": counts.get('still_employed', 0),
            "rate": round(counts.get('still_employed', 0) / total * 100) if total else None,
        }
    return out


def checkins_due(applications, company_id):
    """Hired applications old enough for a 30d/90d retention check-in that
    hasn't been answered yet. Returns [(application, window)]."""
    hired_ids = [a.id for a in applications if a.status == 'hired']
    if not hired_ids:
        return []
    events = ApplicationEvent.query.filter(
        ApplicationEvent.application_id.in_(hired_ids),
        ApplicationEvent.company_id == company_id,
    ).all()
    hired_at, answered = {}, set()
    for e in events:
        if e.event_type == 'status_changed' and e.to_status == 'hired':
            hired_at[e.application_id] = _utc(e.created_at)
        elif e.event_type == 'retention_checkin':
            answered.add((e.application_id, e.checkin_window))

    now = datetime.now(timezone.utc)
    due = []
    for a in applications:
        if a.status != 'hired':
            continue
        start = hired_at.get(a.id) or _utc(a.hire_confirmed_at)
        if not start:
            continue
        for window, days in CHECKIN_WINDOWS.items():
            if (a.id, window) not in answered and now - start >= timedelta(days=days):
                due.append((a, window))
    return due


def platform_benchmark(company_id):
    """Interview, hire and match-score averages across every OTHER company,
    or None until enough distinct companies contribute (k-anonymity floor).
    Aggregate only - no per-company or per-candidate values leave this
    function."""
    base = (
        db.session.query(Application)
        .join(Job, Job.id == Application.job_id)
        .filter(Job.company_id != company_id)
    )
    total = base.count()
    companies = (
        db.session.query(func.count(func.distinct(Job.company_id)))
        .join(Application, Application.job_id == Job.id)
        .filter(Job.company_id != company_id)
        .scalar() or 0
    )
    if companies < MIN_BENCHMARK_COMPANIES or total < MIN_BENCHMARK_APPLICATIONS:
        return None

    interviewed = base.filter(Application.interview_transcript.isnot(None), Application.interview_transcript != '').count()
    hired = base.filter(Application.status == 'hired').count()
    avg_score = (
        db.session.query(func.avg(Application.match_score))
        .join(Job, Job.id == Application.job_id)
        .filter(Job.company_id != company_id, Application.match_score.isnot(None))
        .scalar()
    )
    return {
        "companies": companies,
        "applications": total,
        "interview_rate": round(interviewed / total * 100),
        "hire_rate": round(hired / total * 100),
        "avg_score": round(avg_score) if avg_score is not None else None,
    }


# ------------------------------------------------------------------ #
#  Applicant export
# ------------------------------------------------------------------ #

EXPORT_COLUMNS = [
    "application_id", "job_title", "candidate_name", "candidate_email", "status",
    "applied_at", "resume_match_score", "ai_interview_verdict", "manual_interview_score",
    "ai_answer_likelihood", "composite_score", "hire_compensation", "hire_pay_type",
]


def _csv_safe(value):
    """Neutralise spreadsheet formula injection - an applicant controls their
    own name, and this file is opened in Excel by the employer."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def applicants_csv(applications, jobs_by_id, blind_mode=False):
    """CSV text of every applicant the employer can already see in the UI.
    Blind mode withholds name and email exactly as the applicants page does."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    for a in applications:
        try:
            ai = json.loads(a.ai_likelihood) if a.ai_likelihood else None
        except (ValueError, TypeError):
            ai = None
        job = jobs_by_id.get(a.job_id)
        candidate = a.candidate
        writer.writerow([_csv_safe(v) for v in [
            a.id,
            job.title if job else "",
            "" if blind_mode else (candidate.first_name or ""),
            "" if blind_mode else (candidate.email or ""),
            a.status,
            a.created_at.strftime("%Y-%m-%d") if a.created_at else "",
            a.match_score,
            a.interview_verdict,
            a.manual_interview_score,
            ai.get("label") if isinstance(ai, dict) else "",
            composite_score(a),
            a.hire_salary_usd,
            a.hire_pay_type,
        ]])
    return buf.getvalue()
