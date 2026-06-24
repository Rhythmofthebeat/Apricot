import io
import json
import os
import re
import uuid

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, abort, send_from_directory, Response
from flask_login import login_required, current_user
from sqlalchemy import or_
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet

from . import db
from .models import Job, Application, CandidateProfile, Company, JOB_CATEGORIES, JOB_LOCATIONS, COUNTRIES

from ai_services import (
    extract_text_from_file,
    compare_resume_to_job,
    run_interview_turn,
    transcribe_audio,
    synthesize_speech,
    generate_resume_from_profile,
    suggest_jobs_for_profile,
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
@login_required
def browse_jobs():
    query = request.args.get('q', '').strip()
    category = request.args.get('category', '').strip()
    location = request.args.get('location', '').strip()

    jobs_query = Job.query.filter_by(status='open')
    if query:
        jobs_query = jobs_query.filter(
            or_(Job.title.ilike(f"%{query}%"), Job.description.ilike(f"%{query}%"))
        )
    if category in JOB_CATEGORIES:
        jobs_query = jobs_query.filter_by(category=category)
    if location in JOB_LOCATIONS:
        jobs_query = jobs_query.filter_by(location=location)

    jobs = jobs_query.order_by(Job.created_at.desc()).all()
    return render_template(
        "jobs_browse.html",
        jobs=jobs,
        query=query,
        category=category,
        location=location,
        categories=JOB_CATEGORIES,
        locations=JOB_LOCATIONS,
    )


@candidate.route('/jobs/<int:job_id>')
@login_required
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    existing = Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
    return render_template("job_detail.html", job=job, existing=existing)


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
        result = compare_resume_to_job(job.description, resume_text)
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

    result = compare_resume_to_job(job.description, resume_text)
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
    return render_template("ai_interview.html", job=job, application=application)


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
            job.title, job.description, user_message, history, force_conclude=wrap_up,
            interview_length=job.interview_length, interview_focus=job.interview_focus,
            interview_questions=interview_questions,
        )
    except Exception:
        return jsonify({"error": "Failed to get a response. Please try again."}), 500

    # The AI decides for itself when it has enough signal — conclusion is
    # detected from what it actually said, not from a precomputed flag.
    verdict_match = re.search(r"Verdict:\s*(Strong Yes|Yes|No|Strong No)", reply, re.IGNORECASE)
    should_conclude = bool(verdict_match)

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
        application.interview_verdict = verdict_match.group(1).title()
        application.status = 'interviewing'

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
    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    has_profile = bool(candidate_profile and (candidate_profile.resume_text or '').strip())
    return render_template(
        "candidate_dashboard.html",
        applications=applications,
        meetings=meetings,
        has_profile=has_profile,
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
            "description": job.description,
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
