from website import create_app, mail, limiter, db
from community_stats import resume_total, record_resume_processed
from website.candidate import search_jobs_by_text
from flask_mail import Message
from flask import Flask, render_template, request, jsonify, current_app, Response, flash, redirect, url_for
from flask_login import login_required, current_user
import openai
import io
import json
import re
import os
import logging
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from openai._exceptions import RateLimitError, AuthenticationError, APIConnectionError
from config import Config
from flask_cors import CORS
from werkzeug.utils import secure_filename
from PIL import Image
import pytesseract
from flask import send_from_directory
import uuid
from dotenv import load_dotenv

from ai_services import (
    extract_pdf_text,
    extract_docx_text,
    extract_text_from_file,
    compare_resume_to_job,
    generate_summary,
    generate_basic_analysis,
    generate_resume_from_profile,
    run_interview_turn,
    transcribe_audio,
    generate_interview_question_preview,
    generate_interview_coaching,
)

#  Load env & config
# override=False (the dotenv default) - see website/__init__.py for why this
# matters: it must not silently overwrite an env var a caller set on purpose.
load_dotenv(override=False)
if Config.GOOGLE_APPLICATION_CREDENTIALS:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = Config.GOOGLE_APPLICATION_CREDENTIALS

# App setup
app = create_app()
app.config.from_object(Config)
CORS(app)

# Logging (must come before any function that uses logger)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# OpenAI
client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Short-lived handoff for /resume-jobmatch's Post/Redirect/Get flow. Pasted
# job descriptions can easily exceed the ~4KB Flask session cookie can hold,
# so results are stashed here (keyed by a one-time token in the redirect
# URL) instead of in the session. Safe only because gunicorn runs a single
# worker (see Procfile) - an in-memory dict wouldn't be shared across
# workers if that ever changes.
_RESUME_MATCH_CACHE = {}
_RESUME_MATCH_CACHE_MAX = 200  # evict oldest if a token is never consumed


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/community-resume-count")
def community_resume_count():
    try:
        response = jsonify(total=resume_total(db.engine))
    except Exception:
        logger.exception("Community resume count unavailable")
        response = jsonify(error="Count temporarily unavailable")
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return response


def _record_community_resume():
    # A metrics failure must not discard a completed analysis for the user.
    try:
        record_resume_processed(db.engine)
    except Exception:
        logger.exception("Failed to record completed Community Edition resume")


@app.route("/resume-summarizer")
def resumesum():
    return render_template("resumesum.html")


@app.route('/resume-jobmatch', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=["POST"])
def resumemat():
    """Resume job-match page - handles both the form display and form submission.

    POST redirects back to this same GET route (Post/Redirect/Get) instead of
    rendering the result directly, so reloading the results page just re-GETs
    it instead of resubmitting the form - which used to re-run the AI call
    (burning quota) and drop a duplicate entry into the saved-results list
    every time, since the AI's wording differs slightly on each run.
    """
    def _stash_and_redirect(result, score_value, job_text):
        if len(_RESUME_MATCH_CACHE) >= _RESUME_MATCH_CACHE_MAX:
            _RESUME_MATCH_CACHE.pop(next(iter(_RESUME_MATCH_CACHE)), None)
        token = uuid.uuid4().hex
        _RESUME_MATCH_CACHE[token] = {"result": result, "score_value": score_value, "job_text": job_text}
        return redirect(url_for('resumemat', rid=token))

    if request.method == 'POST':
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login', next=request.path))

        job_text    = request.form.get('job_req', '')
        resume_file = request.files.get('resume')

        if not job_text.strip() or not resume_file:
            return _stash_and_redirect(None, 0, job_text)

        resume_bytes = resume_file.read()
        resume_file.stream.seek(0)

        resume_text = extract_text_from_file(resume_file.filename, resume_bytes)

        if not resume_text.strip():
            logger.error("Extracted resume text is empty")
            return _stash_and_redirect(
                "Resume content could not be read. Please upload a readable PDF, DOCX, or TXT file.",
                0, job_text,
            )

        try:
            result = compare_resume_to_job(job_text, resume_text)
        except RateLimitError:
            return _stash_and_redirect("Our AI service is busy right now. Please try again in a minute.", 0, job_text)
        except AuthenticationError:
            logger.error("OpenAI authentication failed in /resume-jobmatch")
            return _stash_and_redirect("Something went wrong on our end. Please try again shortly.", 0, job_text)
        except Exception as e:
            logger.error(f"compare_resume_to_job failed: {str(e)}")
            return _stash_and_redirect("We couldn't analyze your resume right now. Please try again shortly.", 0, job_text)

        # Persist raw result to disk (optional)
        with open("resumemat.txt", "w", encoding="utf-8") as f:
            f.write(result)

        # Extract numeric score (first number in 0-100 range)
        score_value = 0
        for num in re.findall(r"\b(\d{1,3})\b", result):
            val = int(num)
            if 0 <= val <= 100:
                score_value = val
                break

        if result and result.strip():
            _record_community_resume()
        return _stash_and_redirect(result.replace('*', ''), score_value, job_text)

    saved = _RESUME_MATCH_CACHE.pop(request.args.get('rid'), None) or {}
    return render_template(
        'resumemat.html',
        result=saved.get('result'),
        score_value=saved.get('score_value', 0),
        job_text=saved.get('job_text', ''),
    )


VALID_INTERVIEW_LENGTHS = ("short", "standard", "thorough")


@app.route("/practice-interview-hub")
def practice_interview_hub():
    """Community Edition standalone practice interview - not tied to any job
    posting or application. Anyone can build a practice session either from
    an AI-generated question set (paste a job description) or from their own
    manually-written requirements/questions. Page itself is open to everyone;
    actually running a session (below) requires login since it costs AI calls."""
    return render_template("practice_hub.html")


@app.route("/practice-interview-hub/chat", methods=["POST"])
@limiter.limit("40 per hour")
def practice_hub_chat():
    """Stateless practice interview turn for the standalone hub - mirrors
    candidate.practice_interview_chat but takes the role/description/
    questions straight from the request instead of a Job row, since this
    session isn't tied to any job posting."""
    if not current_user.is_authenticated:
        return jsonify({"error": "Please log in to start a practice interview.", "login_required": True}), 401

    title = (request.form.get("title") or "Practice Interview").strip()[:200]
    description = (request.form.get("description") or "").strip()
    requirements_raw = request.form.get("requirements") or ""
    manual_questions = [q.strip() for q in requirements_raw.splitlines() if q.strip()][:15] or None
    interview_length = request.form.get("interview_length") or "standard"
    if interview_length not in VALID_INTERVIEW_LENGTHS:
        interview_length = "standard"
    interview_focus = (request.form.get("interview_focus") or "").strip()[:2000] or None

    history = json.loads(request.form.get("history", "[]"))
    wrap_up = request.form.get("wrap_up", "false").lower() == "true"
    answer_audio = request.files.get("answer_audio")
    text_answer = (request.form.get("text_answer") or "").strip()

    user_message = text_answer  # text mode takes priority if provided
    if not user_message and answer_audio and answer_audio.filename:
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                tmp.write(answer_audio.read())
                tmp_path = tmp.name
            user_message = transcribe_audio(tmp_path)
            os.unlink(tmp_path)
        except Exception:
            return jsonify({"error": "Couldn't transcribe your answer. Please try again."}), 500

    try:
        reply = run_interview_turn(
            title, description, user_message, history, force_conclude=wrap_up,
            interview_length=interview_length, interview_focus=interview_focus,
            interview_questions=manual_questions, practice_mode=True,
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
        try:
            coaching = generate_interview_coaching(
                transcript=full_transcript,
                job_title=title,
                requirements=description or requirements_raw,
                verdict=None,
                match_score=None,
            )
        except Exception:
            coaching = None

    return jsonify({
        "reply": reply,
        "transcribed_answer": user_message,
        "text_answer": text_answer,
        "concluded": should_conclude,
        "coaching": coaching,
    })


@app.route("/practice-interview-hub/questions", methods=["POST"])
@limiter.limit("15 per hour")
def practice_hub_questions():
    """On-demand sample question list for the standalone hub's AI-generated
    mode, same idea as candidate.practice_questions but built from the
    title/description the visitor typed in rather than an existing Job row."""
    if not current_user.is_authenticated:
        return jsonify({"error": "Please log in to generate questions.", "login_required": True}), 401

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not title or not description:
        return jsonify({"error": "Add a job title and description first."}), 400
    interview_length = request.form.get("interview_length") or "standard"
    if interview_length not in VALID_INTERVIEW_LENGTHS:
        interview_length = "standard"
    interview_focus = (request.form.get("interview_focus") or "").strip() or None

    try:
        questions = generate_interview_question_preview(
            title, description, interview_length=interview_length, interview_focus=interview_focus,
        )
    except Exception:
        return jsonify({"error": "Couldn't generate questions right now. Please try again."}), 500
    return jsonify({"questions": questions})


@app.route("/generate-profile-resume", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def generate_profile_resume():
    """Build a resume from pasted LinkedIn profile text for candidates without one."""
    data = request.get_json() or {}
    linkedin_url = data.get("linkedin_url", "").strip()
    profile_text = data.get("profile_text", "").strip()

    if not profile_text:
        return jsonify({"error": "Paste your LinkedIn About/Experience/Education content first."}), 400

    try:
        resume_text = generate_resume_from_profile(linkedin_url, profile_text)
        return jsonify({"status": "success", "resume_text": resume_text})
    except RateLimitError:
        return jsonify({"error": "AI service is busy. Please try again shortly."}), 429
    except AuthenticationError:
        return jsonify({"error": "API authentication failed."}), 500
    except Exception as e:
        logger.error(f"Profile resume generation failed: {str(e)}")
        return jsonify({"error": "Failed to generate resume. Please try again."}), 500


@app.route("/export/profile-resume-pdf", methods=["POST"])
def export_profile_resume_pdf():
    data = request.get_json() or {}
    resume_text = (data.get("resume_text") or "").strip()
    if not resume_text:
        return "No resume content provided", 400

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("<b>Resume</b>", styles["Title"]),
        Spacer(1, 12),
    ]
    for paragraph in resume_text.split("\n\n"):
        clean = paragraph.strip().replace("\n", "<br/>")
        if clean:
            story.append(Paragraph(clean, styles["Normal"]))
            story.append(Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return Response(
        buffer,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=generated_resume.pdf"},
    )


@app.route("/uploads", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def upload_resume():
    """Handle resume uploads for the summariser page."""
    logger.info("Upload endpoint hit")

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    filename = secure_filename(file.filename)
    ext      = filename.lower()
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    try:
        file.save(filepath)
        with open(filepath, "rb") as f:
            file_stream = io.BytesIO(f.read())

        if ext.endswith(".pdf"):
            text = extract_pdf_text(file_stream)
        elif ext.endswith(".docx"):
            text = extract_docx_text(file_stream)
        elif ext.endswith(".txt"):
            text = file_stream.getvalue().decode("utf-8")
        elif ext.endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff")):
            text = pytesseract.image_to_string(Image.open(file_stream))
        else:
            return jsonify({"error": "Unsupported file type"}), 400

        if not text.strip():
            return jsonify({"status": "partial", "error": "File appears empty or unreadable",
                            **generate_basic_analysis(text)}), 200

        logger.info(f"Extracted {len(text)} characters")

        try:
            text_summary = generate_summary(text)
            if text_summary and text_summary.strip():
                _record_community_resume()
            return jsonify({"status": "success", "text_summary": text_summary, "analysis_type": "AI"})
        except RateLimitError:
            return jsonify({"status": "rate_limited", "error": "api_rate_limited",
                            "retry_after": 60, "analysis_type": "basic",
                            **generate_basic_analysis(text)}), 429
        except AuthenticationError:
            return jsonify({"status": "error", "error": "api_authentication_failed",
                            "analysis_type": "basic", **generate_basic_analysis(text)}), 500
        except Exception as e:
            logger.error(f"OpenAI API failed: {str(e)}")
            return jsonify({"status": "partial", "error": "AI analysis unavailable",
                            "analysis_type": "basic", **generate_basic_analysis(text)}), 200

    except Exception as e:
        logger.error(f"Unexpected error in /upload: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

    finally:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                logger.error(f"Failed to delete temp file: {str(e)}")


def _matching_jobs_payload(user_message):
    """Cheap keyword-matched open jobs for a chat message, serialized for the
    frontend to render as clickable links alongside the AI's reply."""
    jobs = search_jobs_by_text(user_message)
    return [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company.name if job.company else "Company",
            "location": job.location,
            "url": url_for("candidate.job_detail", job_id=job.id),
        }
        for job in jobs
    ]


@app.route("/resume-chat", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def resume_chat():
    """Chat endpoint - resume coaching powered by GPT-4o."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    user_message    = data.get("message", "").strip()
    job_description = data.get("job_description", "").strip()
    resume_analysis = data.get("resume_analysis", "").strip()
    history         = data.get("history", [])

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    context_block = ""
    if job_description:
        context_block += f"\n=== JOB DESCRIPTION ===\n{job_description[:2000]}\n"
    if resume_analysis:
        context_block += f"\n=== MATCH ANALYSIS ===\n{resume_analysis[:2000]}\n"

    system_prompt = (
        "You are an expert career coach and resume advisor. "
        "Your role is to give the candidate actionable tips and guidance so they can improve their own resume - "
        "not to write or rewrite it for them."
        + (f"\n\nYou have the following context:\n{context_block}" if context_block else "")
        + "\n\nGuidelines:\n"
        "- Give specific, honest tips: explain WHAT is weak and WHY, and HOW to fix it - but do not write the fix.\n"
        "- NEVER write, rewrite, or generate resume bullets, summaries, job descriptions, or any resume content on behalf of the candidate. "
        "If asked to write a section or bullet, decline and instead explain what makes a strong version of that content so they can write it themselves.\n"
        "- Do not produce text the candidate could copy-paste directly into their resume. "
        "The goal is to coach their thinking and writing, not to do it for them.\n"
        "- Be honest about gaps and weaknesses, and encouraging about genuine strengths.\n"
        "- Keep advice concrete and easy to act on - suggest structure, keywords, specificity, or metrics they should add.\n"
        "- Politely redirect anything unrelated to resume improvement or career advice."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-20:]:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=800,
            temperature=0.7,
        )
        return jsonify({
            "reply": response.choices[0].message.content.strip(),
            "matching_jobs": _matching_jobs_payload(user_message),
        })
    except Exception as e:
        logger.error(f"Resume chat API error: {str(e)}")
        return jsonify({"error": "Failed to get a response. Please try again."}), 500


SITE_HELP_SYSTEM_PROMPT = """You are the Avants AI site guide - a help assistant embedded on every page of the \
Avants AI hiring platform. Your only job is helping people (candidates, employers, or visitors who haven't signed \
up yet) understand how to use the site and what things mean. You are not a career coach or a general chatbot.

=== WHAT AVANTS AI IS ===
An AI-powered hiring platform. Candidates apply to jobs, complete a real AI voice interview, and get an instant \
resume match score. Employers post jobs and every applicant is automatically resume-scored and voice-interviewed \
by AI before the employer reviews anyone, producing a ranked shortlist instead of a pile of resumes.

=== FOR CANDIDATES ===
- Browse Jobs: search/filter open roles by keyword, category, work type (Remote/Hybrid/On-site), and city.
- Apply: upload a resume (or build one from pasted LinkedIn text via AI) - get an instant 0-100 AI match score \
against the job before submitting.
- AI Interview: after applying, complete a structured voice interview tailored to that specific role. The AI asks \
follow-up questions and issues a verdict (Strong Yes/Yes/No/Strong No) employers see. Can be saved and resumed \
later, but only submitted once.
- Dashboard: track all applications, message employers, see interview status.
- If rejected: candidates may get AI coaching feedback and a personalized skills-gap roadmap.
- If hired: the employer enters the actual compensation figure; the candidate must confirm it's accurate (or \
dispute it if it's wrong) before anything is finalized.
- Community Edition (free, no application needed, open to everyone including employers): a standalone Resume Job \
Match tool (paste a job description + resume, get a score), a Resume Summarizer, and a Practice Interview Hub - \
a mock AI interview built either from a pasted job description (AI generates the questions) or from your own \
manually-typed requirements/questions, not tied to any real job posting or application. All three live under the \
"Resume Tools" menu; running an actual practice session or resume tool requires being logged in even though \
browsing the page doesn't.
- Earn: a separate paid marketplace where professionals rank, rate, fact-check, or write AI training data for \
money, unrelated to job hunting.

=== FOR EMPLOYERS ===
- Sign up with a company work email (personal providers like Gmail aren't accepted for employer accounts).
- Post a job for free - AI can help structure the description.
- Every applicant is automatically screened: resume-scored and voice-interviewed by AI before the employer sees \
anything.
- Review a ranked pipeline: match scores, AI interview verdicts and transcripts, schedule video or in-person \
meetings, message candidates, advance/reject with one click.
- To hire: mark the candidate "Hired" and enter their actual compensation (salary or hourly rate) - this is what \
the placement fee is based on, not whatever salary was posted on the job listing. The candidate must then confirm \
that figure before the fee invoice fires.
- Placement fee: 15% of first-year compensation, due within 30 days of the candidate's confirmed start date. \
Hourly hires bill this monthly for as long as the engagement stays active instead of one lump sum. Candidates are \
never charged anything.
- Hired/Rejected decisions are final once confirmed and can't be reversed by the employer afterward (only the \
hire's compensation figure can still be corrected).
- Employers can close a job (stops new applications, keeps history) or delete it outright only if it has zero \
applicants.
- Full rules live at /for-employers and the binding Employer Platform Agreement is Section 12 of /terms.

=== GENERAL ===
- A language switcher (top nav) machine-translates any page.
- Pricing, fees, and legal terms are covered on /terms and /for-employers - point people there for exact figures \
rather than guessing if you're unsure.

Guidelines:
- Be concise and concrete. Tell people exactly where to click/go (e.g. "Browse Jobs in the top nav", "your \
Dashboard").
- If asked something outside what you know about this site (general career advice, coding help, anything \
unrelated to Avants AI), politely say that's outside what you can help with here and redirect to what you can \
help with.
- Never invent features, prices, or policies you're not sure about - say you're not certain and point to /terms, \
/for-employers, or /faq instead of guessing.
- You cannot see which page the person is currently on or their account details - ask if it would help clarify \
your answer."""


@app.route("/site-help-chat", methods=["POST"])
@limiter.limit("30 per hour")
def site_help_chat():
    """Site-wide help chat - explains how Avants AI works and where to find things.
    Deliberately not @login_required so logged-out visitors can ask questions too."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    user_message = data.get("message", "").strip()
    history = data.get("history", [])

    if not user_message:
        return jsonify({"error": "No message provided"}), 400

    messages = [{"role": "system", "content": SITE_HELP_SYSTEM_PROMPT}]
    for msg in history[-20:]:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=500,
            temperature=0.4,
        )
        return jsonify({
            "reply": response.choices[0].message.content.strip(),
            "matching_jobs": _matching_jobs_payload(user_message),
        })
    except Exception as e:
        logger.error(f"Site help chat API error: {str(e)}")
        return jsonify({"error": "Failed to get a response. Please try again."}), 500


def _google_tts_credentials():
    """Explicit credentials built from GOOGLE_CREDENTIALS_JSON (the service
    account key pasted as a plain env var), if set. Preferred over
    GOOGLE_APPLICATION_CREDENTIALS's file-path/Secret-File mounting, which
    has repeatedly failed to actually land the file on disk in production.
    Returns None to fall back to the default file-based lookup."""
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        return None
    from google.oauth2 import service_account
    info = json.loads(raw)
    return service_account.Credentials.from_service_account_info(info)


@app.route("/speak", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def speak_summary():
    from google.cloud import texttospeech
    from google.auth.exceptions import DefaultCredentialsError
    data         = request.get_json()
    summary_text = data.get("text", "").strip()

    if not summary_text:
        return jsonify({"error": "No summary text provided"}), 400

    try:
        audio_folder = app.config["AUDIO_FOLDER"]
        filename = f"{uuid.uuid4()}.mp3"
        filepath = os.path.join(audio_folder, filename)

        credentials = _google_tts_credentials()
        tts_client = (
            texttospeech.TextToSpeechClient(credentials=credentials)
            if credentials else texttospeech.TextToSpeechClient()
        )
        input_text  = texttospeech.SynthesisInput(text=summary_text)
        voice_name  = data.get("voice", "en-US-Wavenet-D")
        voice       = texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name=voice_name,
            ssml_gender=texttospeech.SsmlVoiceGender.MALE,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=float(data.get("speed", 1.2)),
        )
        tts_response = tts_client.synthesize_speech(
            input=input_text, voice=voice, audio_config=audio_config
        )
        with open(filepath, "wb") as out:
            out.write(tts_response.audio_content)

        return jsonify({"audio_url": f"/audio/{filename}"})
    except DefaultCredentialsError as e:
        logger.error(f"Google TTS credentials not configured: {str(e)}")
        return jsonify({"error": "Text-to-speech isn't configured on the server (missing Google credentials)."}), 500
    except Exception as e:
        logger.error(f"Google TTS error: {str(e)}", exc_info=True)
        return jsonify({"error": "Audio generation failed. Please try again."}), 500


@app.route("/audio/<filename>")
def serve_audio(filename):
    return send_from_directory(app.config["AUDIO_FOLDER"], filename)


@app.route("/export/summary-pdf", methods=["POST"])
@login_required
def export_summary_pdf():
    data    = request.get_json()
    summary = (data or {}).get("summary", "").strip()
    if not summary:
        return "No summary provided", 400

    # Strip voice-name lines
    voice_names = ["Alex", "Rose", "Linda", "Tom"]
    lines   = [l for l in summary.split("\n") if l.strip() not in voice_names]
    summary = "\n".join(lines).strip()

    buffer = io.BytesIO()
    doc    = SimpleDocTemplate(buffer, pagesize=LETTER)
    styles = getSampleStyleSheet()
    story  = [
        Paragraph("<b>Resume Summary</b>", styles["Title"]),
        Spacer(1, 12),
    ]
    for paragraph in summary.split("\n\n"):
        clean = paragraph.strip().replace("\n", "<br/>")
        if clean:
            story.append(Paragraph(clean, styles["Normal"]))
            story.append(Spacer(1, 6))

    doc.build(story)
    buffer.seek(0)
    return Response(
        buffer,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=resume_summary.pdf"},
    )


@app.route("/faq")
def faq():
    return render_template("faq.html")


@app.route("/for-employers")
def for_employers():
    return render_template("for_employers.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/contact", methods=["GET", "POST"])
@limiter.limit("20 per hour", key_func=lambda: "contact-global", methods=["POST"])
@limiter.limit("5 per hour", methods=["POST"])
@limiter.limit("20 per hour", methods=["POST"], key_func=lambda: "contact-global")
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()
        honeypot = request.form.get("website", "").strip()
        not_robot = request.form.get("not_robot")

        if honeypot:
            # Bot filled in the hidden field - pretend it worked so it doesn't
            # adjust its behavior, but skip sending anything.
            flash("Thanks for reaching out! Your message has been sent.", "success")
            return redirect(url_for("contact"))

        if not name or not email or not message:
            flash("All fields are required.", "error")
            return render_template("contact.html")

        if not not_robot:
            flash("Please confirm you're not a robot.", "error")
            return render_template("contact.html")

        try:
            # Notify us only; no auto-reply is sent to the sender
            msg = Message(
                subject=f"Contact form: {name}",
                recipients=["bazarirhythm@gmail.com"],
                reply_to=email,
                body=f"From: {name} <{email}>\n\n{message}",
            )
            mail.send(msg)

            flash("Thanks for reaching out! Your message has been sent.", "success")
        except Exception as e:
            logger.error(f"Contact form send failed: {str(e)}")
            flash("Couldn't send your message right now. Please try again shortly.", "error")
            return render_template("contact.html")

        return redirect(url_for("contact"))

    return render_template("contact.html")


@app.route("/health")
def health_check():
    return jsonify({
        "status": "healthy",
        "services": {
            "openai": "enabled" if Config.OPENAI_API_KEY else "disabled",
            "storage": {
                "upload_folder": os.path.exists(app.config["UPLOAD_FOLDER"]),
                "audio_folder":  os.path.exists(app.config["AUDIO_FOLDER"]),
            },
        },
    })


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
