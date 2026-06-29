import io
import json
import os
import re
import uuid

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, abort, send_from_directory, Response
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import or_, func
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet

from . import db
from .models import Job, Application, CandidateProfile, Company, Notification, JOB_CATEGORIES, JOB_LOCATIONS, COUNTRIES

from ai_services import (
    extract_text_from_file,
    compare_resume_to_job,
    run_interview_turn,
    transcribe_audio,
    synthesize_speech,
    generate_resume_from_profile,
    suggest_jobs_for_profile,
    find_similar_jobs,
    generate_interview_coaching,
    generate_skills_roadmap,
    analyze_ai_likelihood,
)

candidate = Blueprint('candidate', __name__)

SUGGESTION_JOB_POOL = 40

AUDIO_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio", "interviews")


def _audio_dir_for_application(application_id):
    path = os.path.join(AUDIO_BASE_DIR, str(application_id))
    os.makedirs(path, exist_ok=True)
    return path


def _can_access_application_audio(application):
    if current_user.id == application.candidate_user_id:
        return True
    if current_user.role == 'employer':
        job = Job.query.get(application.job_id)
        company = Company.query.filter_by(owner_user_id=current_user.id).first()
        if job and company and job.company_id == company.id:
            return True
    return False


def _extract_score(result_text):
    for num in re.findall(r"\b(\d{1,3})\b", result_text):
        val = int(num)
        if 0 <= val <= 100:
            return val
    return 0


@candidate.route('/jobs')
def browse_jobs():
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    location = request.args.get('location', '').strip()
    sort = request.args.get('sort', 'newest').strip()

    jobs_query = Job.query.filter_by(status='open')
    if query:
        jobs_query = jobs_query.filter(
            or_(Job.title.ilike(f"%{query}%"), Job.description.ilike(f"%{query}%"))
        )
    if category in JOB_CATEGORIES:
        jobs_query = jobs_query.filter_by(category=category)
    if location in JOB_LOCATIONS:
        jobs_query = jobs_query.filter_by(location=location)

    if sort == 'priority':
        applicant_count_subq = (
            db.session.query(func.count(Application.id))
            .filter(Application.job_id == Job.id)
            .correlate(Job)
            .scalar_subquery()
        )
        jobs_query = jobs_query.order_by(applicant_count_subq.desc())
    elif sort == 'trending':
        week_ago = datetime.utcnow() - timedelta(days=7)
        recent_count_subq = (
            db.session.query(func.count(Application.id))
            .filter(Application.job_id == Job.id, Application.created_at >= week_ago)
            .correlate(Job)
            .scalar_subquery()
        )
        jobs_query = jobs_query.order_by(recent_count_subq.desc())
    elif sort == 'pay':
        jobs_query = jobs_query.order_by(Job.salary_usd.desc().nullslast())
    else:
        jobs_query = jobs_query.order_by(Job.created_at.desc())

    jobs = jobs_query.all()

    applied_job_ids = set()
    if current_user.is_authenticated and current_user.role == 'candidate':
        applied_job_ids = {
            a.job_id for a in Application.query.filter_by(
                candidate_user_id=current_user.id, withdrawn=False
            ).with_entities(Application.job_id).all()
        }

    return render_template(
        "jobs_browse.html",
        jobs=jobs,
        query=query,
        category=category,
        location=location,
        sort=sort,
        categories=JOB_CATEGORIES,
        locations=JOB_LOCATIONS,
        applied_job_ids=applied_job_ids,
    )


@candidate.route('/jobs/<int:job_id>')
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    existing = (
        Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
        if current_user.is_authenticated else None
    )

    # Compute live rank for this candidate
    rank = None
    total_applicants = 0
    percentile = None
    if existing and not existing.withdrawn and existing.match_score is not None:
        all_apps = (
            Application.query.filter_by(job_id=job.id, withdrawn=False)
            .with_entities(Application.match_score)
            .all()
        )
        scores = sorted([a.match_score or 0 for a in all_apps], reverse=True)
        total_applicants = len(scores)
        my_score = existing.match_score
        rank = next((i + 1 for i, s in enumerate(scores) if s <= my_score), total_applicants)
        percentile = round((total_applicants - rank) / total_applicants * 100) if total_applicants > 1 else 100

    return render_template("job_detail.html", job=job, existing=existing,
                           rank=rank, total_applicants=total_applicants, percentile=percentile)


@candidate.route('/jobs/<int:job_id>/apply', methods=['GET'])
@login_required
def apply(job_id):
    job = Job.query.get_or_404(job_id)

    existing = Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
    if existing:
        flash("You've already applied to this job.", "error")
        return redirect(url_for('candidate.job_detail', job_id=job.id))

    return render_template("apply.html", job=job, countries=COUNTRIES)


@candidate.route('/extract-resume-text', methods=['POST'])
@login_required
def extract_resume_text():
    """Pull text out of an uploaded resume file — used by the apply wizard's
    first step before the candidate moves on to checking their match score."""
    resume_file = request.files.get('resume')
    if not resume_file or not resume_file.filename:
        return jsonify({"error": "No file uploaded."}), 400

    resume_text = extract_text_from_file(resume_file.filename, resume_file.read())
    if not resume_text.strip():
        return jsonify({"error": "Couldn't read that file. Try a different PDF, DOCX, or TXT."}), 400

    return jsonify({"resume_text": resume_text})


@candidate.route('/jobs/<int:job_id>/preview-match', methods=['POST'])
@login_required
def preview_match(job_id):
    """Score a resume against the job WITHOUT creating an application — lets
    the candidate revise their resume and re-check as many times as they want
    before committing to a submission."""
    job = Job.query.get_or_404(job_id)
    data = request.get_json() or {}
    resume_text = (data.get('resume_text') or '').strip()

    if not resume_text:
        return jsonify({"error": "Add your resume content first."}), 400

    try:
        result = compare_resume_to_job(job.description_text, resume_text)
    except Exception:
        return jsonify({"error": "Failed to score your resume. Please try again."}), 500

    score = _extract_score(result)
    return jsonify({"score": score, "explanation": result.replace('*', '')})


@candidate.route('/jobs/<int:job_id>/apply', methods=['POST'])
@login_required
def submit_application(job_id):
    job = Job.query.get_or_404(job_id)

    existing = Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
    if existing:
        return jsonify({"error": "You've already applied to this job."}), 400

    data = request.get_json() or {}
    resume_text = (data.get('resume_text') or '').strip()
    work_authorization = data.get('work_authorization')

    if not resume_text:
        return jsonify({"error": "Add your resume content first."}), 400

    result = compare_resume_to_job(job.description_text, resume_text)
    score = _extract_score(result)

    application = Application(
        job_id=job.id,
        candidate_user_id=current_user.id,
        resume_text=resume_text,
        work_authorization=json.dumps(work_authorization) if work_authorization else None,
        match_score=score,
        match_explanation=result.replace('*', ''),
    )
    db.session.add(application)

    profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    if not profile:
        profile = CandidateProfile(user_id=current_user.id)
        db.session.add(profile)
    profile.resume_text = resume_text

    db.session.commit()

    return jsonify({
        "redirect_url": url_for('candidate.apply_next_steps', job_id=job.id, application_id=application.id)
    })


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/next')
@login_required
def apply_next_steps(job_id, application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    job = Job.query.get_or_404(job_id)
    return render_template("apply_next_steps.html", job=job, application=application)


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/interview')
@login_required
def interview(job_id, application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    job = Job.query.get_or_404(job_id)
    already_done = bool(application.interview_transcript)
    saved_session = None
    if not already_done and application.interview_saved_session:
        try:
            saved_session = json.loads(application.interview_saved_session)
        except (ValueError, TypeError):
            pass
    return render_template("ai_interview.html", job=job, application=application,
                           already_done=already_done, saved_session=saved_session)


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/save-session', methods=['POST'])
@login_required
def save_interview_session(job_id, application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    data = request.get_json() or {}
    history = data.get('history', [])
    application.interview_saved_session = json.dumps(history)
    db.session.commit()
    return jsonify({'status': 'saved'})


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/interview-chat', methods=['POST'])
@login_required
def interview_chat(job_id, application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    job = Job.query.get_or_404(job_id)

    history = json.loads(request.form.get("history", "[]"))
    wrap_up = request.form.get("wrap_up", "false").lower() == "true"
    answer_audio = request.files.get("answer_audio")

    audio_log = json.loads(application.interview_audio_log) if application.interview_audio_log else []
    audio_dir = _audio_dir_for_application(application_id)

    user_message = ""
    if answer_audio and answer_audio.filename:
        answer_filename = f"a_{len(audio_log) + 1}.webm"
        answer_audio.save(os.path.join(audio_dir, answer_filename))
        try:
            user_message = transcribe_audio(os.path.join(audio_dir, answer_filename))
        except Exception:
            return jsonify({"error": "Couldn't transcribe your answer. Please try again."}), 500
        audio_log.append({"role": "user", "text": user_message, "audio_filename": answer_filename})

    try:
        interview_questions = json.loads(job.interview_questions) if job.interview_questions else None
        reply = run_interview_turn(
            job.title, job.description_text, user_message, history, force_conclude=wrap_up,
            interview_length=job.interview_length, interview_focus=job.interview_focus,
            interview_questions=interview_questions,
        )
    except Exception:
        return jsonify({"error": "Failed to get a response. Please try again."}), 500

    verdict_match = re.search(r"Verdict:\s*(Strong Yes|Yes|No|Strong No)", reply, re.IGNORECASE)
    should_conclude = bool(verdict_match) or wrap_up

    question_filename = f"q_{len(audio_log) + 1}.mp3"
    try:
        synthesize_speech(reply, os.path.join(audio_dir, question_filename))
        reply_audio_url = url_for('candidate.application_audio', application_id=application_id, filename=question_filename)
    except Exception:
        reply_audio_url = None

    audio_log.append({"role": "assistant", "text": reply, "audio_filename": question_filename if reply_audio_url else None})
    application.interview_audio_log = json.dumps(audio_log)

    if should_conclude:
        transcript_lines = [f"{m.get('role')}: {m.get('content')}" for m in history]
        transcript_lines.append(f"assistant: {reply}")
        application.interview_transcript = "\n".join(transcript_lines)
        application.interview_saved_session = None  # clear saved session on completion
        verdict = verdict_match.group(1).title() if verdict_match else "Pending"
        application.interview_verdict = verdict
        application.status = 'interviewing'
        try:
            ai_result = analyze_ai_likelihood(application.interview_transcript, job.title)
            application.ai_likelihood = json.dumps(ai_result)
        except Exception:
            pass

        if current_user.notifications_enabled:
            job = Job.query.get(job_id)
            positive = "Yes" in verdict
            notif = Notification(
                user_id=current_user.id,
                message=(
                    f"Your AI interview for {job.title} is complete — verdict: {verdict}."
                    if positive else
                    f"Your AI interview for {job.title} is complete. The employer will review your result."
                ),
                link=url_for('candidate.dashboard', _external=False),
            )
            db.session.add(notif)

    db.session.commit()

    return jsonify({
        "reply": reply,
        "reply_audio_url": reply_audio_url,
        "transcribed_answer": user_message,
        "concluded": should_conclude,
    })


@candidate.route('/applications/<int:application_id>/audio/<filename>')
@login_required
def application_audio(application_id, filename):
    application = Application.query.get_or_404(application_id)
    if not _can_access_application_audio(application):
        abort(403)
    return send_from_directory(_audio_dir_for_application(application_id), filename)


@candidate.route('/candidate/dashboard')
@login_required
def dashboard():
    applications = (
        Application.query.filter_by(candidate_user_id=current_user.id)
        .order_by(Application.created_at.desc())
        .all()
    )
    meetings = {
        app.id: json.loads(app.scheduled_meeting) if app.scheduled_meeting else None
        for app in applications
    }

    # Lazily generate skills roadmap for low-scoring applications that don't have one yet
    roadmaps_generated = False
    for app in applications:
        if (app.match_score is not None and app.match_score < 65
                and not app.skills_roadmap and app.status != 'hired'):
            try:
                secs = app.job.description_sections or {}
                steps = generate_skills_roadmap(
                    job_title=app.job.title,
                    match_explanation=app.match_explanation or '',
                    match_score=app.match_score,
                    requirements=secs.get('requirements', ''),
                )
                app.skills_roadmap = json.dumps(steps)
                roadmaps_generated = True
            except Exception:
                pass
    if roadmaps_generated:
        db.session.commit()

    # Parse roadmaps for template
    roadmaps = {}
    for app in applications:
        if app.skills_roadmap:
            try:
                roadmaps[app.id] = json.loads(app.skills_roadmap)
            except Exception:
                pass

    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    has_profile = bool(candidate_profile and (candidate_profile.resume_text or '').strip())
    return render_template(
        "candidate_dashboard.html",
        applications=applications,
        meetings=meetings,
        has_profile=has_profile,
        roadmaps=roadmaps,
    )


@candidate.route('/profile')
@login_required
def profile():
    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    return render_template("profile.html", profile=candidate_profile)


@candidate.route('/profile/save', methods=['POST'])
@login_required
def save_profile():
    data = request.get_json() or {}
    headline = (data.get('headline') or '').strip()
    linkedin_url = (data.get('linkedin_url') or '').strip()
    target_roles = (data.get('target_roles') or '').strip()
    resume_text = (data.get('resume_text') or '').strip()

    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    if not candidate_profile:
        candidate_profile = CandidateProfile(user_id=current_user.id)
        db.session.add(candidate_profile)

    candidate_profile.headline = headline or None
    candidate_profile.linkedin_url = linkedin_url or None
    candidate_profile.target_roles = target_roles or None
    if resume_text:
        candidate_profile.resume_text = resume_text

    db.session.commit()
    return jsonify({"status": "success"})


@candidate.route('/profile/generate-resume', methods=['POST'])
@login_required
def generate_profile_resume():
    """Intake pasted LinkedIn profile content and turn it into a structured resume."""
    data = request.get_json() or {}
    linkedin_url = (data.get('linkedin_url') or '').strip()
    profile_text = (data.get('profile_text') or '').strip()

    if not profile_text:
        return jsonify({"error": "Paste your LinkedIn About/Experience/Education content first."}), 400

    try:
        resume_text = generate_resume_from_profile(linkedin_url, profile_text)
    except Exception:
        return jsonify({"error": "Failed to generate resume. Please try again."}), 500

    return jsonify({"resume_text": resume_text})


@candidate.route('/profile/resume.pdf')
@login_required
def download_profile_resume():
    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    if not candidate_profile or not (candidate_profile.resume_text or '').strip():
        abort(404)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER)
    styles = getSampleStyleSheet()
    story = [Paragraph("<b>Resume</b>", styles["Title"]), Spacer(1, 12)]
    for paragraph in candidate_profile.resume_text.split("\n\n"):
        clean = paragraph.strip().replace("\n", "<br/>")
        if clean:
            story.append(Paragraph(clean, styles["Normal"]))
            story.append(Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return Response(
        buffer,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=resume.pdf"},
    )


@candidate.route('/profile/suggested-jobs')
@login_required
def suggested_jobs():
    """AI-ranked open jobs/companies on the platform that fit this candidate's profile."""
    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    if not candidate_profile or not (candidate_profile.resume_text or '').strip():
        return jsonify({"suggestions": []})

    already_applied_ids = {
        app.job_id for app in Application.query.filter_by(candidate_user_id=current_user.id).all()
    }
    open_jobs = (
        Job.query.filter_by(status='open')
        .order_by(Job.created_at.desc())
        .limit(SUGGESTION_JOB_POOL)
        .all()
    )
    open_jobs = [job for job in open_jobs if job.id not in already_applied_ids]
    if not open_jobs:
        return jsonify({"suggestions": []})

    jobs_payload = [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company.name,
            "category": job.category,
            "description": job.description_text,
        }
        for job in open_jobs
    ]

    try:
        ranked = suggest_jobs_for_profile(candidate_profile.resume_text, candidate_profile.target_roles, jobs_payload)
    except Exception:
        return jsonify({"error": "Failed to generate suggestions. Please try again."}), 500

    jobs_by_id = {job.id: job for job in open_jobs}
    suggestions = []
    for item in ranked[:8]:
        job = jobs_by_id.get(item["job_id"])
        if not job:
            continue
        suggestions.append({
            "job_id": job.id,
            "title": job.title,
            "company": job.company.name,
            "score": item["score"],
            "reason": item["reason"],
            "url": url_for('candidate.job_detail', job_id=job.id),
        })

    return jsonify({"suggestions": suggestions})


SIMILAR_JOB_POOL = 50

@candidate.route('/jobs/<int:job_id>/similar')
def similar_jobs(job_id):
    """AI-powered similar jobs based on role title + description, not category."""
    current_job = Job.query.get_or_404(job_id)

    other_jobs = (
        Job.query.filter(Job.status == 'open', Job.id != job_id)
        .order_by(Job.created_at.desc())
        .limit(SIMILAR_JOB_POOL)
        .all()
    )
    if not other_jobs:
        return jsonify({"similar": []})

    current_payload = {
        "id": current_job.id,
        "title": current_job.title,
        "description": current_job.description_text,
    }
    others_payload = [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company.name,
            "description": j.description_text,
        }
        for j in other_jobs
    ]

    try:
        ranked = find_similar_jobs(current_payload, others_payload)
    except Exception:
        return jsonify({"similar": []})

    jobs_by_id = {j.id: j for j in other_jobs}
    similar = []
    for item in ranked:
        job = jobs_by_id.get(item["job_id"])
        if not job:
            continue
        similar.append({
            "job_id": job.id,
            "title": job.title,
            "company": job.company.name,
            "similarity": item["similarity"],
            "reason": item["reason"],
            "url": url_for('candidate.job_detail', job_id=job.id),
            "salary": None,
        })

    return jsonify({"similar": similar})


@candidate.route('/applications/<int:application_id>/coaching')
@login_required
def interview_coaching(application_id):
    """Post-interview coaching report — private to the candidate."""
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    if not application.interview_transcript:
        flash("Complete your AI interview first to unlock the coaching report.", "info")
        return redirect(url_for('candidate.dashboard'))
    job = Job.query.get_or_404(application.job_id)
    secs = job.description_sections or {}
    try:
        coaching = generate_interview_coaching(
            transcript=application.interview_transcript,
            job_title=job.title,
            requirements=secs.get('requirements', ''),
            verdict=application.interview_verdict,
            match_score=application.match_score,
        )
    except Exception:
        flash("Coaching report temporarily unavailable. Please try again.", "error")
        return redirect(url_for('candidate.dashboard'))
    return render_template('interview_coaching.html', application=application, job=job, coaching=coaching)


@candidate.route('/jobs/<int:job_id>/practice')
@login_required
def practice_interview(job_id):
    """Practice the AI interview for a job without it counting as your real attempt."""
    job = Job.query.get_or_404(job_id)
    if job.status != 'open':
        flash("This job is no longer accepting applications.", "info")
        return redirect(url_for('candidate.browse_jobs'))
    return render_template('practice_interview.html', job=job)


@candidate.route('/jobs/<int:job_id>/practice-chat', methods=['POST'])
@login_required
def practice_interview_chat(job_id):
    """Stateless practice interview turn — no DB writes, returns coaching at conclusion."""
    job = Job.query.get_or_404(job_id)

    history = json.loads(request.form.get("history", "[]"))
    wrap_up = request.form.get("wrap_up", "false").lower() == "true"
    answer_audio = request.files.get("answer_audio")
    text_answer = (request.form.get("text_answer") or "").strip()

    user_message = text_answer  # text mode takes priority if provided
    if not user_message and answer_audio and answer_audio.filename:
        try:
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp.write(answer_audio.read())
                tmp_path = tmp.name
            user_message = transcribe_audio(tmp_path)
            _os.unlink(tmp_path)
        except Exception:
            return jsonify({"error": "Couldn't transcribe your answer. Please try again."}), 500

    try:
        # Never pass the real employer questions — practice uses generated ones
        # so the actual interview remains a surprise for the candidate.
        reply = run_interview_turn(
            job.title, job.description_text, user_message, history, force_conclude=wrap_up,
            interview_length=job.interview_length, interview_focus=job.interview_focus,
            interview_questions=None,
        )
    except Exception:
        return jsonify({"error": "Failed to get a response. Please try again."}), 500

    verdict_match = re.search(r"Verdict:\s*(Strong Yes|Yes|No|Strong No)", reply, re.IGNORECASE)
    should_conclude = bool(verdict_match)

    coaching = None
    if should_conclude:
        transcript_lines = [f"{m.get('role')}: {m.get('content')}" for m in history]
        transcript_lines.append(f"assistant: {reply}")
        full_transcript = "\n".join(transcript_lines)
        secs = job.description_sections or {}
        try:
            coaching = generate_interview_coaching(
                transcript=full_transcript,
                job_title=job.title,
                requirements=secs.get('requirements', ''),
                verdict=None,
                match_score=None,
            )
        except Exception:
            coaching = None

    return jsonify({
        "reply": reply,
        "reply_audio_url": None,
        "transcribed_answer": user_message,
        "text_answer": text_answer,
        "concluded": should_conclude,
        "coaching": coaching,
    })


@candidate.route('/applications/<int:application_id>/withdraw', methods=['POST'])
@login_required
def withdraw_application(application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    if application.status == 'hired':
        flash("You can't withdraw from a position you've already been hired for.", "error")
        return redirect(url_for('candidate.dashboard'))
    application.withdrawn = True
    db.session.commit()
    flash("Application withdrawn.", "success")
    return redirect(url_for('candidate.dashboard'))


@candidate.route('/notifications')
@login_required
def notifications():
    notifs = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(30)
        .all()
    )
    unread = sum(1 for n in notifs if not n.read)
    return jsonify({
        "notifications": [
            {
                "id": n.id,
                "message": n.message,
                "link": n.link,
                "read": n.read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifs
        ],
        "unread_count": unread,
        "notifications_enabled": current_user.notifications_enabled,
    })


@candidate.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, read=False).update({"read": True})
    db.session.commit()
    return jsonify({"status": "ok"})


@candidate.route('/notifications/preferences', methods=['POST'])
@login_required
def notification_preferences():
    data = request.get_json() or {}
    current_user.notifications_enabled = bool(data.get("enabled", True))
    db.session.commit()
    return jsonify({"notifications_enabled": current_user.notifications_enabled})
