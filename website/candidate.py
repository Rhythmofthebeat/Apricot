import io
import json
import logging
import os
import re
import httpx
from concurrent.futures import ThreadPoolExecutor
import uuid

from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, abort, send_from_directory, Response
from flask_login import login_required, current_user
from datetime import datetime, timedelta
from sqlalchemy import or_, func
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet

from . import db, limiter
from .models import Job, Application, CandidateProfile, Company, CompanyMember, Notification, SavedJob, Message, MeetingRoom, MeetingProposal, User, Feedback, JOB_CATEGORIES, JOB_LOCATIONS, COUNTRIES
from .matching import keyword_set, overlap_score, NOTIFY_SCORE_THRESHOLD, NOTIFY_MAX_RECIPIENTS
from email_utils import send_branded_email, external_url
from video_storage import (
    get_signed_video_url, delete_interview_videos,
    create_interview_answer_upload_url, download_interview_answer_to_file,
)

from ai_services import (
    extract_text_from_file,
    compare_resume_to_job,
    run_interview_turn,
    transcribe_audio,
    extract_audio_for_transcription,
    audio_file_extension,
    AudioTooLargeError,
    synthesize_speech,
    generate_resume_from_profile,
    suggest_jobs_for_profile,
    find_similar_jobs,
    generate_interview_coaching,
    generate_skills_roadmap,
    analyze_ai_likelihood,
    generate_interview_question_preview,
)

candidate = Blueprint('candidate', __name__)

logger = logging.getLogger(__name__)

SUGGESTION_JOB_POOL = 40

AUDIO_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio", "interviews")


def _audio_dir_for_application(application_id):
    path = os.path.join(AUDIO_BASE_DIR, str(application_id))
    os.makedirs(path, exist_ok=True)
    return path


def _notify_employers_of_open_to_work(profile):
    """Cheap keyword-match nudge to employers whose open jobs overlap a
    candidate who just opted into the talent pool - mirrors
    employer.notify_candidates_of_new_job, same reasoning: no real AI call
    per profile save."""
    candidate_words = keyword_set(profile.skills, profile.target_roles, profile.headline)
    if not candidate_words:
        return

    from sqlalchemy.orm import joinedload
    jobs = (
        Job.query.filter_by(status='open')
        .options(joinedload(Job.company))
        .limit(300)
        .all()
    )

    # Keep only the best-matching job per company, so a company with many
    # open roles doesn't get notified once per role.
    scored_by_company = {}
    for job in jobs:
        owner = job.company.owner_user_id if job.company else None
        if not owner:
            continue
        job_words = keyword_set(job.title, job.category, (job.description_text or '')[:500])
        score = overlap_score(candidate_words, job_words)
        if score < NOTIFY_SCORE_THRESHOLD:
            continue
        best = scored_by_company.get(job.company_id)
        if not best or score > best[0]:
            scored_by_company[job.company_id] = (score, job, owner)

    ranked = sorted(scored_by_company.values(), key=lambda x: x[0], reverse=True)
    for _, job, owner_id in ranked[:NOTIFY_MAX_RECIPIENTS]:
        owner = User.query.get(owner_id)
        if not owner or not owner.notifications_enabled:
            continue
        db.session.add(Notification(
            user_id=owner_id,
            message=f"A candidate who fits {job.title} just joined the talent pool.",
            link=url_for('employer.talent_pool'),
        ))


def _company_recipients(company):
    """Owner plus every team member of a company - used for notifications that
    matter to whoever is actually handling hiring day-to-day, not just the
    account that originally signed up."""
    recipients = []
    owner = User.query.get(company.owner_user_id)
    if owner:
        recipients.append(owner)
    member_ids = [m.user_id for m in CompanyMember.query.filter_by(company_id=company.id).all()]
    if member_ids:
        recipients.extend(User.query.filter(User.id.in_(member_ids)).all())
    return recipients


def _can_access_application_audio(application):
    if current_user.id == application.candidate_user_id:
        return True
    if current_user.role == 'employer':
        job = Job.query.get(application.job_id)
        if job:
            owns = Company.query.filter_by(owner_user_id=current_user.id, id=job.company_id).first()
            member = CompanyMember.query.filter_by(user_id=current_user.id, company_id=job.company_id).first()
            if owns or member:
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
    city = request.args.get('city', '').strip()
    sort = request.args.get('sort', 'newest').strip()

    jobs_query = Job.query.filter_by(status='open')
    if query:
        jobs_query = jobs_query.filter(
            or_(Job.title.ilike(f"%{query}%"), Job.description.ilike(f"%{query}%"))
        )
    if category in JOB_CATEGORIES:
        jobs_query = jobs_query.filter_by(category=category)
    if location:
        jobs_query = jobs_query.filter(Job.location.ilike(f"{location}%"))
    if city:
        # City isn't its own column - Hybrid/On-site postings store it appended
        # to location as "Hybrid - New York, NY" (see employer.post_job), so
        # match it as a substring rather than a separate field.
        jobs_query = jobs_query.filter(Job.location.ilike(f"%{city}%"))

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

    # Distinct cities actually in use, for the city filter's suggestions -
    # parsed from the "<work type> - <city>" format post_job.html stores
    # (e.g. "Hybrid - New York, NY"); plain "Remote (Worldwide)"/"On-site"
    # values with no " - " have no city to extract.
    all_locations = {
        loc for (loc,) in Job.query.filter_by(status='open')
        .filter(Job.location.isnot(None)).with_entities(Job.location).distinct().all()
    }
    available_cities = sorted({
        loc.split(' - ', 1)[1].strip() for loc in all_locations if ' - ' in loc
    })

    applied_job_ids = set()
    saved_job_ids = set()
    if current_user.is_authenticated and current_user.role == 'candidate':
        applied_job_ids = {
            a.job_id for a in Application.query.filter_by(
                candidate_user_id=current_user.id, withdrawn=False
            ).with_entities(Application.job_id).all()
        }
        saved_job_ids = {
            s.job_id for s in SavedJob.query.filter_by(user_id=current_user.id).with_entities(SavedJob.job_id).all()
        }

    return render_template(
        "jobs_browse.html",
        jobs=jobs,
        query=query,
        category=category,
        location=location,
        city=city,
        sort=sort,
        categories=JOB_CATEGORIES,
        locations=["Remote (Worldwide)", "Remote (US Only)", "Hybrid", "On-site"],
        available_cities=available_cities,
        applied_job_ids=applied_job_ids,
        saved_job_ids=saved_job_ids,
    )


CHAT_SEARCH_STOPWORDS = {
    "a", "an", "the", "is", "are", "am", "be", "do", "does", "did", "you", "your",
    "have", "has", "any", "for", "in", "on", "at", "with", "and", "or", "to", "of",
    "i", "me", "my", "we", "us", "job", "jobs", "role", "roles", "position", "positions",
    "looking", "look", "find", "search", "want", "need", "that", "this", "match",
    "matches", "matching", "please", "hi", "hello", "hey", "can", "could", "would",
    "there", "here", "what", "which", "some", "get", "give", "show", "see",
}


def search_jobs_by_text(text, limit=5):
    """Cheap keyword search against open jobs, for surfacing relevant listings
    from free-text chat input (no AI call - just the same ilike pattern
    browse_jobs() above uses). Returns [] if nothing meaningful to search on."""
    words = re.findall(r"[a-zA-Z0-9+#]+", (text or "").lower())
    keywords = [w for w in words if len(w) > 2 and w not in CHAT_SEARCH_STOPWORDS]
    if not keywords:
        return []

    conditions = []
    for kw in keywords[:8]:
        pattern = f"%{kw}%"
        conditions.extend([
            Job.title.ilike(pattern),
            Job.description.ilike(pattern),
            Job.location.ilike(pattern),
            Job.category.ilike(pattern),
        ])

    return (
        Job.query.filter_by(status="open")
        .filter(or_(*conditions))
        .order_by(Job.created_at.desc())
        .limit(limit)
        .all()
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

    is_saved = (
        SavedJob.query.filter_by(user_id=current_user.id, job_id=job.id).first() is not None
        if current_user.is_authenticated else False
    )

    is_own_posting = False
    if current_user.is_authenticated and current_user.role == 'employer':
        is_owner = job.company.owner_user_id == current_user.id
        is_member = CompanyMember.query.filter_by(company_id=job.company_id, user_id=current_user.id).first() is not None
        is_own_posting = is_owner or is_member

    return render_template("job_detail.html", job=job, existing=existing,
                           rank=rank, total_applicants=total_applicants, percentile=percentile,
                           is_saved=is_saved, is_own_posting=is_own_posting)


@candidate.route('/jobs/<int:job_id>/save', methods=['POST'])
@login_required
def toggle_save_job(job_id):
    job = Job.query.get_or_404(job_id)
    saved = SavedJob.query.filter_by(user_id=current_user.id, job_id=job.id).first()
    if saved:
        db.session.delete(saved)
        db.session.commit()
        return jsonify({"saved": False})
    db.session.add(SavedJob(user_id=current_user.id, job_id=job.id))
    db.session.commit()
    return jsonify({"saved": True})


@candidate.route('/jobs/<int:job_id>/apply', methods=['GET'])
@login_required
def apply(job_id):
    if current_user.role != 'candidate':
        abort(403)
    job = Job.query.get_or_404(job_id)

    existing = Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
    if existing:
        flash("You've already applied to this job.", "error")
        return redirect(url_for('candidate.job_detail', job_id=job.id))

    profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    saved_resume_text = (profile.resume_text or '').strip() if profile else ''

    return render_template("apply.html", job=job, countries=COUNTRIES, saved_resume_text=saved_resume_text or None)


@candidate.route('/extract-resume-text', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def extract_resume_text():
    """Pull text out of an uploaded resume file - used by the apply wizard's
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
@limiter.limit("20 per hour")
def preview_match(job_id):
    """Score a resume against the job WITHOUT creating an application - lets
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
@limiter.limit("10 per hour")
def submit_application(job_id):
    if current_user.role != 'candidate':
        abort(403)
    job = Job.query.get_or_404(job_id)

    existing = Application.query.filter_by(job_id=job.id, candidate_user_id=current_user.id).first()
    if existing:
        return jsonify({"error": "You've already applied to this job."}), 400

    data = request.get_json() or {}
    resume_text = (data.get('resume_text') or '').strip()
    work_authorization = data.get('work_authorization')

    if not resume_text:
        return jsonify({"error": "Add your resume content first."}), 400

    try:
        result = compare_resume_to_job(job.description_text, resume_text)
    except Exception as e:
        return jsonify({"error": "Resume analysis failed - please try again."}), 500

    score = _extract_score(result)

    try:
        application = Application(
            job_id=job.id,
            candidate_user_id=current_user.id,
            resume_text=resume_text,
            work_authorization=json.dumps(work_authorization) if work_authorization else None,
            match_score=score,
            match_explanation=result.replace('*', ''),
        )
        db.session.add(application)

        try:
            profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
            if not profile:
                profile = CandidateProfile(user_id=current_user.id)
                db.session.add(profile)
            profile.resume_text = resume_text
        except Exception:
            db.session.rollback()

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Could not save your application - please try again."}), 500

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


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/interview-answer-upload-url', methods=['POST'])
@login_required
@limiter.limit("120 per hour")
def interview_answer_upload_url(job_id, application_id):
    """Hands back a one-time signed URL the browser uploads a recorded
    answer to directly in Supabase Storage, instead of sending it through
    this app's own /interview-chat route. Needed because large uploads to
    avants.ai itself get rejected at the edge well under what a longer
    answer needs - a direct-to-storage upload bypasses that entirely since
    the big transfer never touches this app's domain."""
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)

    audio_log = json.loads(application.interview_audio_log) if application.interview_audio_log else []
    turn_number = len(audio_log) + 1
    content_type = (request.get_json(silent=True) or {}).get('content_type') or 'video/webm'

    try:
        upload_url, path = create_interview_answer_upload_url(application_id, turn_number, content_type)
    except Exception:
        logger.exception(f"Failed to create a signed interview-answer upload URL for application {application_id}")
        return jsonify({"error": "Couldn't prepare the upload. Please try again."}), 500

    return jsonify({
        "upload_url": upload_url,
        "path": path,
        "anon_key": os.environ.get("SUPABASE_ANON_KEY"),
    })


@candidate.route('/jobs/<int:job_id>/apply/<int:application_id>/interview-chat', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def interview_chat(job_id, application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    job = Job.query.get_or_404(job_id)

    history = json.loads(request.form.get("history", "[]"))
    wrap_up = request.form.get("wrap_up", "false").lower() == "true"
    video_path_uploaded = request.form.get("video_path") or None
    has_video = request.form.get("has_video", "false").lower() == "true"

    audio_log = json.loads(application.interview_audio_log) if application.interview_audio_log else []
    audio_dir = _audio_dir_for_application(application_id)

    user_message = ""
    if video_path_uploaded:
        # The browser already uploaded the recording directly to Supabase
        # Storage (see interview_answer_upload_url above) - this is just a
        # throwaway local copy of that same file for transcription. The
        # permanent recording candidates/employers play back later is the
        # direct-to-storage upload itself, never touched by anything below.
        ext = os.path.splitext(video_path_uploaded)[1] or ".webm"
        tmp_path = os.path.join(audio_dir, f"_tmp_{uuid.uuid4().hex}{ext}")

        try:
            download_interview_answer_to_file(video_path_uploaded, tmp_path)
        except httpx.TimeoutException:
            logger.exception(f"Timed out fetching uploaded interview answer for application {application_id}")
            return jsonify({"error": "That answer took too long to fetch for processing. Please try again."}), 400
        except Exception:
            logger.exception(f"Failed to fetch uploaded interview answer for application {application_id}")
            return jsonify({"error": "Couldn't find your uploaded answer. Please try again."}), 400

        # Strips the video down to just its audio track before Whisper -
        # the picture data (not the speech) is almost all of a video
        # answer's size, so this is what actually lets a long recording fit
        # under Whisper's fixed 25MB cap instead of the roughly-a-minute
        # ceiling raw video hit before. No-ops back to tmp_path if ffmpeg
        # isn't available.
        audio_path = extract_audio_for_transcription(tmp_path)
        try:
            user_message = transcribe_audio(audio_path)
        except AudioTooLargeError as e:
            os.remove(tmp_path)
            if audio_path != tmp_path and os.path.exists(audio_path):
                os.remove(audio_path)
            return jsonify({"error": str(e)}), 400
        except Exception:
            os.remove(tmp_path)
            if audio_path != tmp_path and os.path.exists(audio_path):
                os.remove(audio_path)
            return jsonify({"error": "Couldn't transcribe your answer. Please try again."}), 500
        os.remove(tmp_path)
        if audio_path != tmp_path and os.path.exists(audio_path):
            os.remove(audio_path)

        audio_log.append({
            "role": "user", "text": user_message,
            "video_path": video_path_uploaded, "has_video": has_video,
        })

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

    if wrap_up and not verdict_match:
        # The model was told to conclude but didn't return a parseable verdict
        # line - retry once with the same context before falling back to
        # "Pending", so a single off-format reply doesn't permanently strand
        # this application with no usable grade (see job_applicants.html's
        # "interviewing"-styled Pending badge for how that's surfaced).
        logger.warning(f"Interview conclude for application {application_id} returned no parseable verdict on first try - retrying")
        try:
            retry_reply = run_interview_turn(
                job.title, job.description_text, user_message, history, force_conclude=True,
                interview_length=job.interview_length, interview_focus=job.interview_focus,
                interview_questions=interview_questions,
            )
            retry_match = re.search(r"Verdict:\s*(Strong Yes|Yes|No|Strong No)", retry_reply, re.IGNORECASE)
            if retry_match:
                reply, verdict_match = retry_reply, retry_match
        except Exception:
            logger.exception(f"Verdict retry failed for application {application_id}")
        if not verdict_match:
            logger.error(f"Application {application_id} concluded with no parseable verdict after retry - grading falls back to resume-only")

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
                    f"Your AI interview for {job.title} is complete - verdict: {verdict}."
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


@candidate.route('/applications/<int:application_id>/interview-video/<int:log_index>')
@login_required
def application_video(application_id, log_index):
    """Redirects to a freshly-generated signed URL for a recorded answer,
    rather than storing/exposing the signed URL itself - those expire, and
    regenerating one per request means it's never stale."""
    application = Application.query.get_or_404(application_id)
    if not _can_access_application_audio(application):
        abort(403)
    log = json.loads(application.interview_audio_log) if application.interview_audio_log else []
    if log_index < 0 or log_index >= len(log) or not log[log_index].get('video_path'):
        abort(404)
    try:
        signed_url = get_signed_video_url(log[log_index]['video_path'])
    except Exception:
        logger.exception(f"Failed to generate signed video URL for application {application_id}")
        abort(502)
    return redirect(signed_url)


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

    from datetime import date as _date
    today = _date.today().isoformat()
    app_ids = [a.id for a in applications]
    try:
        upcoming_interviews = (
            MeetingRoom.query
            .filter(MeetingRoom.application_id.in_(app_ids))
            .filter(MeetingRoom.scheduled_date >= today)
            .order_by(MeetingRoom.scheduled_date, MeetingRoom.scheduled_time)
            .all()
        ) if app_ids else []
        pending_proposals = (
            MeetingProposal.query
            .filter(MeetingProposal.application_id.in_(app_ids))
            .filter_by(status='pending')
            .count()
        ) if app_ids else 0
    except Exception:
        db.session.rollback()
        upcoming_interviews = []
        pending_proposals = 0

    given_feedback = {
        (f.application_id, f.context)
        for f in Feedback.query.filter_by(user_id=current_user.id).filter(Feedback.application_id.in_(app_ids)).all()
    } if app_ids else set()

    # One feedback prompt per application - prefer the resolution prompt over
    # the interview one if both would apply, since it's the more final moment.
    feedback_prompts = {}
    for app in applications:
        if app.status in ('hired', 'rejected') and (app.id, 'application_resolved') not in given_feedback:
            feedback_prompts[app.id] = 'application_resolved'
        elif app.interview_verdict and app.interview_verdict != 'Pending' and (app.id, 'interview_completed') not in given_feedback:
            feedback_prompts[app.id] = 'interview_completed'

    return render_template(
        "candidate_dashboard.html",
        applications=applications,
        meetings=meetings,
        has_profile=has_profile,
        roadmaps=roadmaps,
        upcoming_interviews=upcoming_interviews,
        pending_proposals=pending_proposals,
        feedback_prompts=feedback_prompts,
    )


@candidate.route('/candidate/proposals/<int:proposal_id>/accept', methods=['POST'])
@login_required
def accept_slot(proposal_id):
    from daily_utils import create_room as _create_room
    from flask import current_app

    proposal = MeetingProposal.query.get_or_404(proposal_id)
    application = Application.query.get_or_404(proposal.application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    if proposal.status != 'pending':
        flash("This scheduling request is no longer active.", "error")
        return redirect(url_for('candidate.interviews'))

    slot_index = request.form.get('slot')
    slots = json.loads(proposal.slots)
    try:
        slot = slots[int(slot_index)]
    except (TypeError, IndexError, ValueError):
        flash("Please select a time slot.", "error")
        return redirect(url_for('candidate.interviews'))

    api_key = current_app.config.get('DAILY_API_KEY')
    if not api_key:
        flash("Video rooms are not configured yet - contact support.", "error")
        return redirect(url_for('candidate.interviews'))

    try:
        room_data = _create_room(api_key)
    except Exception as e:
        flash(f"Could not create room: {e}", "error")
        return redirect(url_for('candidate.interviews'))

    room = MeetingRoom(
        application_id=application.id,
        employer_user_id=proposal.employer_user_id,
        room_name=room_data['name'],
        room_url=room_data['url'],
        scheduled_date=slot['date'],
        scheduled_time=slot['time'],
        notes=proposal.notes,
    )
    db.session.add(room)

    proposal.status = 'accepted'
    proposal.accepted_slot = json.dumps(slot)

    join_url = external_url('views.join_meeting', room_name=room_data['name'])
    application.scheduled_meeting = json.dumps({
        'type': 'virtual', 'date': slot['date'], 'time': slot['time'],
        'location_or_url': join_url, 'notes': proposal.notes or '', 'in_app': True,
    })

    employer = proposal.employer
    notif = Notification(
        user_id=employer.id,
        message=f"{current_user.first_name or current_user.email} confirmed {slot['date']} at {slot['time']} for {application.job.title}.",
        link=url_for('employer.employer_meetings'),
    )
    db.session.add(notif)
    db.session.commit()

    try:
        send_branded_email(
            to_email=employer.email,
            subject=f"Interview confirmed - {application.job.title}",
            heading="Interview time confirmed",
            body_html=(
                f"<strong>{current_user.first_name or current_user.email}</strong> has confirmed "
                f"<strong>{slot['date']}</strong> at <strong>{slot['time']}</strong> "
                f"for <strong>{application.job.title}</strong>.<br><br>"
                "The interview room is ready."
            ),
            plain_text=(
                f"{current_user.first_name or current_user.email} confirmed the interview:\n"
                f"Job: {application.job.title}\nDate: {slot['date']}\nTime: {slot['time']}\n\nJoin: {join_url}\n"
            ),
            cta_label="Join Interview Room",
            cta_url=join_url,
        )
    except Exception:
        pass

    flash(f"Interview confirmed for {slot['date']} at {slot['time']}. The room is ready!", "success")
    return redirect(url_for('candidate.interviews'))


@candidate.route('/candidate/interviews')
@login_required
def interviews():
    """Candidate-facing page showing all their scheduled meetings."""
    from datetime import date as _date
    applications = Application.query.filter_by(candidate_user_id=current_user.id).all()
    app_ids = [a.id for a in applications]
    app_map = {a.id: a for a in applications}

    rooms = (
        MeetingRoom.query
        .filter(MeetingRoom.application_id.in_(app_ids))
        .order_by(MeetingRoom.scheduled_date.asc(), MeetingRoom.scheduled_time.asc())
        .all()
    )

    today = _date.today().isoformat()
    upcoming = [r for r in rooms if (r.scheduled_date or '') >= today]
    past     = [r for r in rooms if (r.scheduled_date or '') < today]

    pending_proposals = (
        MeetingProposal.query
        .filter(MeetingProposal.application_id.in_(app_ids))
        .filter_by(status='pending')
        .order_by(MeetingProposal.created_at.desc())
        .all()
    ) if app_ids else []

    return render_template(
        'candidate_interviews.html',
        upcoming=upcoming,
        past=past,
        app_map=app_map,
        pending_proposals=pending_proposals,
    )


@candidate.route('/applications/<int:application_id>/platform-feedback', methods=['POST'])
@login_required
def submit_platform_feedback(application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)

    context = request.form.get('context', '')
    if context not in ('interview_completed', 'application_resolved'):
        flash("Something went wrong - please try again.", "error")
        return redirect(request.referrer or url_for('candidate.dashboard'))

    rating = request.form.get('rating', type=int)
    if not rating or rating < 1 or rating > 5:
        flash("Please select a rating from 1 to 5.", "error")
        return redirect(request.referrer or url_for('candidate.dashboard'))

    comment = (request.form.get('comment') or '').strip()

    existing = Feedback.query.filter_by(
        user_id=current_user.id, application_id=application_id, context=context
    ).first()
    if not existing:
        db.session.add(Feedback(
            user_id=current_user.id,
            application_id=application_id,
            role='candidate',
            context=context,
            rating=rating,
            comment=comment or None,
        ))
        db.session.commit()

    flash("Thanks for the feedback!", "success")
    return redirect(request.referrer or url_for('candidate.dashboard'))


@candidate.route('/profile')
@login_required
def profile():
    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    return render_template("profile.html", profile=candidate_profile)


@candidate.route('/profile/save', methods=['POST'])
@login_required
def save_profile():
    data = request.get_json() or {}

    first_name = (data.get('first_name') or '').strip()
    if first_name:
        current_user.first_name = first_name

    candidate_profile = CandidateProfile.query.filter_by(user_id=current_user.id).first()
    if not candidate_profile:
        candidate_profile = CandidateProfile(user_id=current_user.id)
        db.session.add(candidate_profile)

    was_open_to_work = candidate_profile.open_to_work

    candidate_profile.headline       = (data.get('headline') or '').strip() or None
    candidate_profile.linkedin_url   = (data.get('linkedin_url') or '').strip() or None
    candidate_profile.target_roles   = (data.get('target_roles') or '').strip() or None
    candidate_profile.skills         = (data.get('skills') or '').strip() or None
    candidate_profile.work_preference = (data.get('work_preference') or '').strip() or None
    candidate_profile.availability   = (data.get('availability') or '').strip() or None
    candidate_profile.years_experience = (data.get('years_experience') or '').strip() or None
    candidate_profile.location       = (data.get('location') or '').strip() or None
    candidate_profile.open_to_work   = bool(data.get('open_to_work', False))

    resume_text = (data.get('resume_text') or '').strip()
    if resume_text:
        candidate_profile.resume_text = resume_text

    db.session.commit()

    if candidate_profile.open_to_work and not was_open_to_work:
        _notify_employers_of_open_to_work(candidate_profile)
        db.session.commit()

    return jsonify({"status": "success"})


@candidate.route('/profile/generate-resume', methods=['POST'])
@login_required
@limiter.limit("15 per hour")
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


@candidate.route('/candidates/<int:user_id>')
def public_profile(user_id):
    from .models import User
    user = User.query.get_or_404(user_id)
    prof = CandidateProfile.query.filter_by(user_id=user_id).first_or_404()
    if not prof.open_to_work:
        abort(404)

    # employer invite-to-apply logic
    employer_jobs = []
    if current_user.is_authenticated and current_user.role == 'employer':
        from .models import Company
        company = Company.query.filter_by(owner_user_id=current_user.id).first()
        if company:
            employer_jobs = Job.query.filter_by(company_id=company.id, status='open').all()

    return render_template("candidate_public.html", profile=prof, user=user, employer_jobs=employer_jobs)


@candidate.route('/candidates/<int:user_id>/invite', methods=['POST'])
@login_required
def invite_candidate(user_id):
    from .models import User, Company
    if current_user.role != 'employer':
        return jsonify({"error": "Employers only"}), 403
    target_user = User.query.get_or_404(user_id)
    job_id = request.get_json(silent=True, force=True).get('job_id') if request.data else None

    company = Company.query.filter_by(owner_user_id=current_user.id).first()
    company_name = company.name if company else "An employer"
    job = Job.query.get(job_id) if job_id else None
    msg = f"{company_name} is interested in your profile"
    link = url_for('candidate.job_detail', job_id=job.id) if job else url_for('employer.company_page', company_id=company.id) if company else None
    if job:
        msg += f" and invited you to apply for {job.title}"
    db.session.add(Notification(user_id=target_user.id, message=msg + ".", link=link))
    db.session.commit()
    return jsonify({"status": "invited"})


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
@limiter.limit("30 per hour")
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
    """Post-interview coaching report - private to the candidate."""
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
    if current_user.role != 'candidate':
        abort(403)
    job = Job.query.get_or_404(job_id)
    if job.status != 'open':
        flash("This job is no longer accepting applications.", "info")
        return redirect(url_for('candidate.browse_jobs'))
    return render_template('practice_interview.html', job=job)


@candidate.route('/jobs/<int:job_id>/practice-chat', methods=['POST'])
@login_required
@limiter.limit("40 per hour")
def practice_interview_chat(job_id):
    """Stateless practice interview turn - no DB writes, returns coaching at conclusion."""
    job = Job.query.get_or_404(job_id)

    history = json.loads(request.form.get("history", "[]"))
    wrap_up = request.form.get("wrap_up", "false").lower() == "true"
    answer_audio = request.files.get("answer_audio")
    text_answer = (request.form.get("text_answer") or "").strip()

    user_message = text_answer  # text mode takes priority if provided
    if not user_message and answer_audio and answer_audio.filename:
        import tempfile, os as _os
        ext = audio_file_extension(answer_audio.filename)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(answer_audio.read())
            tmp_path = tmp.name
        # See interview_chat() above for why this runs before transcribing -
        # strips video down to just audio so long answers fit under
        # Whisper's 25MB cap instead of the much smaller ceiling raw video hits.
        audio_path = extract_audio_for_transcription(tmp_path)
        try:
            user_message = transcribe_audio(audio_path)
        except AudioTooLargeError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            return jsonify({"error": "Couldn't transcribe your answer. Please try again."}), 500
        finally:
            _os.unlink(tmp_path)
            if audio_path != tmp_path and _os.path.exists(audio_path):
                _os.unlink(audio_path)

    secs = job.description_sections or {}
    coaching = None

    if wrap_up:
        # force_conclude=True guarantees this turn ends in a verdict, not
        # another question - safe to kick off the coaching report
        # immediately instead of waiting for the verdict call to finish
        # first. That serial wait was the main reason "stop early" felt slow.
        transcript_lines = [f"{m.get('role')}: {m.get('content')}" for m in history]
        if user_message:
            transcript_lines.append(f"user: {user_message}")
        full_transcript = "\n".join(transcript_lines)

        with ThreadPoolExecutor(max_workers=2) as pool:
            # Never pass the real employer questions - practice uses generated
            # ones so the actual interview remains a surprise for the candidate.
            reply_future = pool.submit(
                run_interview_turn, job.title, job.description_text, user_message, history,
                force_conclude=True, interview_length=job.interview_length,
                interview_focus=job.interview_focus, interview_questions=None, practice_mode=True,
            )
            coaching_future = pool.submit(
                generate_interview_coaching, transcript=full_transcript, job_title=job.title,
                requirements=secs.get('requirements', ''), verdict=None, match_score=None,
            )
            try:
                reply = reply_future.result()
            except Exception:
                return jsonify({"error": "Failed to get a response. Please try again."}), 500
            try:
                coaching = coaching_future.result()
            except Exception:
                coaching = None
        should_conclude = True
    else:
        try:
            # Never pass the real employer questions - practice uses generated
            # ones so the actual interview remains a surprise for the candidate.
            reply = run_interview_turn(
                job.title, job.description_text, user_message, history, force_conclude=False,
                interview_length=job.interview_length, interview_focus=job.interview_focus,
                interview_questions=None, practice_mode=True,
            )
        except Exception:
            return jsonify({"error": "Failed to get a response. Please try again."}), 500

        verdict_match = re.search(r"Verdict:\s*(Strong Yes|Yes|No|Strong No)", reply, re.IGNORECASE)
        should_conclude = bool(verdict_match)
        if should_conclude:
            transcript_lines = [f"{m.get('role')}: {m.get('content')}" for m in history]
            transcript_lines.append(f"assistant: {reply}")
            full_transcript = "\n".join(transcript_lines)
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


@candidate.route('/jobs/<int:job_id>/practice-questions', methods=['POST'])
@login_required
@limiter.limit("15 per hour")
def practice_questions(job_id):
    """On-demand sample question list for a role, generated fresh each time -
    an alternative to the live back-and-forth practice chat for a candidate
    who'd rather just see what kinds of questions to expect and prepare
    answers on their own time."""
    if current_user.role != 'candidate':
        abort(403)
    job = Job.query.get_or_404(job_id)
    try:
        questions = generate_interview_question_preview(
            job.title, job.description_text,
            interview_length=job.interview_length, interview_focus=job.interview_focus,
        )
    except Exception:
        return jsonify({"error": "Couldn't generate questions right now. Please try again."}), 500
    return jsonify({"questions": questions})


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


@candidate.route('/applications/<int:application_id>/confirm-hire', methods=['POST'])
@login_required
def confirm_hire(application_id):
    """Candidate-side verification that a hire actually happened - the employer's
    placement fee invoice only fires after this, not the moment they click 'hired'."""
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    if application.status != 'hired':
        abort(400)

    if not application.candidate_confirmed_hire:
        application.candidate_confirmed_hire = True
        application.hire_confirmed_at = func.now()
        application.hire_salary_disputed = False  # confirming clears any earlier dispute
        db.session.commit()

        job = Job.query.get(application.job_id)
        company = Company.query.get(job.company_id)
        employer_user = User.query.get(company.owner_user_id)

        for recipient in _company_recipients(company):
            db.session.add(Notification(
                user_id=recipient.id,
                message=f"{current_user.first_name} confirmed their hire for {job.title}.",
                link=url_for('employer.applicants', job_id=job.id),
            ))
        db.session.commit()

        if employer_user:
            from .employer import send_hire_fee_invoice
            send_hire_fee_invoice(application, job, company, current_user, employer_user)

    flash("Thanks for confirming! We've let the employer know.", "success")
    return redirect(url_for('candidate.dashboard'))


@candidate.route('/applications/<int:application_id>/dispute-hire', methods=['POST'])
@login_required
def dispute_hire(application_id):
    """Candidate says the compensation the employer entered isn't accurate.
    Flags the application instead of confirming - the placement fee invoice
    stays on hold until the employer corrects it and the candidate confirms."""
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)
    if application.status != 'hired':
        abort(400)

    application.hire_salary_disputed = True
    application.candidate_confirmed_hire = False
    application.hire_confirmed_at = None

    from .employer import _save_hire_evidence
    evidence_filename = _save_hire_evidence(application.id, request.files.get('hire_evidence'))
    if evidence_filename:
        application.hire_evidence_filename = evidence_filename

    db.session.commit()

    job = Job.query.get(application.job_id)
    company = Company.query.get(job.company_id)

    for recipient in _company_recipients(company):
        db.session.add(Notification(
            user_id=recipient.id,
            message=f"{current_user.first_name} disputed the hire compensation for {job.title} - the placement fee invoice is on hold.",
            link=url_for('employer.applicants', job_id=job.id),
        ))
    db.session.commit()

    for recipient in _company_recipients(company):
        try:
            send_branded_email(
                to_email=recipient.email,
                subject=f"Hire compensation disputed - {job.title}",
                heading="Candidate flagged the compensation figure",
                body_html=(
                    f"<strong>{current_user.first_name}</strong> says the compensation entered for their hire as "
                    f"<strong>{job.title}</strong> isn't accurate. The placement fee invoice is on hold until you "
                    f"correct it and they re-confirm."
                ),
                plain_text=(
                    f"{current_user.first_name} disputed the hire compensation for {job.title}. "
                    f"The placement fee invoice is on hold until you correct it and they re-confirm."
                ),
                cta_label="Review Applicant",
                cta_url=external_url('employer.applicants', job_id=job.id),
            )
        except Exception as e:
            print("Dispute notification email failed:", e)

    flash("We've flagged this for the employer to review. The placement fee won't be invoiced until it's corrected.", "info")
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


# ------------------------------------------------------------------ #
#  In-App Messaging (candidate side)
# ------------------------------------------------------------------ #

@candidate.route('/candidate/applications/<int:application_id>/messages.json')
@login_required
def candidate_messages_json(application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)

    # Mark employer messages as read
    Message.query.filter_by(application_id=application_id, read=False).filter(
        Message.sender_user_id != current_user.id
    ).update({"read": True})
    db.session.commit()

    msgs = Message.query.filter_by(application_id=application_id).order_by(Message.created_at).all()
    return jsonify([{
        "id": m.id,
        "body": m.body,
        "sender_name": m.sender.first_name or m.sender.email,
        "is_mine": m.sender_user_id == current_user.id,
        "time": m.created_at.strftime("%b %d, %H:%M") if m.created_at else "",
    } for m in msgs])


@candidate.route('/candidate/applications/<int:application_id>/message', methods=['POST'])
@login_required
def candidate_send_message(application_id):
    application = Application.query.get_or_404(application_id)
    if application.candidate_user_id != current_user.id:
        abort(403)

    body = (request.json or {}).get('body', '').strip() if request.is_json else request.form.get('body', '').strip()
    if not body:
        return jsonify({"error": "Message body is required"}), 400

    msg = Message(application_id=application_id, sender_user_id=current_user.id, body=body)
    db.session.add(msg)

    # Notify employer
    job = application.job
    employer_company = job.company
    owner = db.session.get(type(current_user), employer_company.owner_user_id)
    if owner:
        notif = Notification(
            user_id=owner.id,
            message=f"{current_user.first_name or current_user.email} sent a message about their {job.title} application.",
            link=url_for('employer.applicants', job_id=job.id),
        )
        db.session.add(notif)
    db.session.commit()

    return jsonify({
        "id": msg.id,
        "body": msg.body,
        "sender_name": current_user.first_name or current_user.email,
        "is_mine": True,
        "time": msg.created_at.strftime("%b %d, %H:%M") if msg.created_at else "",
    })


# ------------------------------------------------------------------ #
#  Account deletion (GDPR right to erasure)
# ------------------------------------------------------------------ #

@candidate.route('/account/delete', methods=['GET', 'POST'])
@login_required
def delete_account():
    if current_user.role != 'candidate':
        abort(403)

    if request.method == 'POST':
        confirm = request.form.get('confirm', '').strip().lower()
        if confirm != 'delete my account':
            flash("Type exactly 'delete my account' to confirm.", "error")
            return render_template('delete_account.html')

        user = current_user
        from flask_login import logout_user as _logout

        # Wipe personal data from all applications
        apps = Application.query.filter_by(candidate_user_id=user.id).all()
        for app in apps:
            delete_interview_videos(app.id)
            app.resume_text = '[deleted]'
            app.interview_transcript = None
            app.interview_audio_log = None
            app.work_authorization = None
            app.match_explanation = None
            app.skills_roadmap = None
            app.interview_saved_session = None

        # Delete profile
        profile = CandidateProfile.query.filter_by(user_id=user.id).first()
        if profile:
            db.session.delete(profile)

        # Delete notifications, saved jobs, messages
        Notification.query.filter_by(user_id=user.id).delete()
        SavedJob.query.filter_by(user_id=user.id).delete()
        Message.query.filter_by(sender_user_id=user.id).delete()

        # Anonymise the user record
        user.first_name = 'Deleted'
        user.email = f'deleted_{user.id}@deleted.invalid'
        user.password = None
        user.email_verified = False

        db.session.commit()
        _logout()
        flash("Your account and personal data have been deleted.", "success")
        return redirect(url_for('views.home'))

    return render_template('delete_account.html')
