from website import create_app, mail
from flask_mail import Message
from flask import Flask, render_template, request, jsonify, current_app, Response, flash, redirect, url_for
import openai
import io
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
)

#  Load env & config
load_dotenv(override=True)
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
openai.api_key = Config.OPENAI_API_KEY


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/resume-summarizer")
def resumesum():
    return render_template("resumesum.html")


@app.route('/resume-jobmatch', methods=['GET', 'POST'])
def resumemat():
    """Resume job-match page — handles both the form display and form submission."""
    result        = None
    score_value   = 0
    cleaned_result = None   # always defined — avoids NameError on GET
    job_text      = ''      # always defined

    if request.method == 'POST':
        job_text    = request.form.get('job_req', '')
        resume_file = request.files.get('resume')

        if not job_text.strip() or not resume_file:
            return render_template('resumemat.html', result=None, score_value=0, job_text=job_text)

        resume_bytes = resume_file.read()
        resume_file.stream.seek(0)

        resume_text = extract_text_from_file(resume_file.filename, resume_bytes)

        if not resume_text.strip():
            logger.error("Extracted resume text is empty")
            return render_template(
                'resumemat.html',
                result="Resume content could not be read. Please upload a readable PDF, DOCX, or TXT file.",
                score_value=0,
                job_text=job_text,
            )

        result = compare_resume_to_job(job_text, resume_text)

        # Persist raw result to disk (optional)
        with open("resumemat.txt", "w", encoding="utf-8") as f:
            f.write(result)

        # Extract numeric score (first number in 0-100 range)
        for num in re.findall(r"\b(\d{1,3})\b", result):
            val = int(num)
            if 0 <= val <= 100:
                score_value = val
                break

        cleaned_result = result.replace('*', '')

    return render_template(
        'resumemat.html',
        result=cleaned_result,
        score_value=score_value,
        job_text=job_text,
    )


@app.route("/generate-profile-resume", methods=["POST"])
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


@app.route("/resume-chat", methods=["POST"])
def resume_chat():
    """Chat endpoint — resume coaching powered by GPT-4o."""
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
        "Your role is to give the candidate actionable tips and guidance so they can improve their own resume — "
        "not to write or rewrite it for them."
        + (f"\n\nYou have the following context:\n{context_block}" if context_block else "")
        + "\n\nGuidelines:\n"
        "- Give specific, honest tips: explain WHAT is weak and WHY, and HOW to fix it — but do not write the fix.\n"
        "- NEVER write, rewrite, or generate resume bullets, summaries, job descriptions, or any resume content on behalf of the candidate. "
        "If asked to write a section or bullet, decline and instead explain what makes a strong version of that content so they can write it themselves.\n"
        "- Do not produce text the candidate could copy-paste directly into their resume. "
        "The goal is to coach their thinking and writing, not to do it for them.\n"
        "- Be honest about gaps and weaknesses, and encouraging about genuine strengths.\n"
        "- Keep advice concrete and easy to act on — suggest structure, keywords, specificity, or metrics they should add.\n"
        "- Politely redirect anything unrelated to resume improvement or career advice."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-20:]:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=800,
            temperature=0.7,
        )
        return jsonify({"reply": response.choices[0].message.content.strip()})
    except Exception as e:
        logger.error(f"Resume chat API error: {str(e)}")
        return jsonify({"error": "Failed to get a response. Please try again."}), 500


@app.route("/speak", methods=["POST"])
def speak_summary():
    from google.cloud import texttospeech
    data         = request.get_json()
    summary_text = data.get("text", "").strip()

    if not summary_text:
        return jsonify({"error": "No summary text provided"}), 400

    try:
        audio_folder = os.path.join(current_app.root_path, "website", "audio")
        os.makedirs(audio_folder, exist_ok=True)
        filename = f"{uuid.uuid4()}.mp3"
        filepath = os.path.join(audio_folder, filename)

        tts_client  = texttospeech.TextToSpeechClient()
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
    except Exception as e:
        logger.error(f"Google TTS error: {str(e)}", exc_info=True)
        return jsonify({"error": "Audio generation failed"}), 500


@app.route("/audio/<filename>")
def serve_audio(filename):
    audio_folder = os.path.join(current_app.root_path, "website", "audio")
    return send_from_directory(audio_folder, filename)


@app.route("/export/summary-pdf", methods=["POST"])
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


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            flash("All fields are required.", "error")
            return render_template("contact.html")

        try:
            # Notify us
            msg = Message(
                subject=f"Contact form: {name}",
                recipients=["bazarirhythm@gmail.com"],
                reply_to=email,
                body=f"From: {name} <{email}>\n\n{message}",
            )
            mail.send(msg)

            # Auto-reply to sender
            from email_utils import send_branded_email
            send_branded_email(
                to_email=email,
                subject="We received your message",
                heading=f"Thanks for reaching out, {name.split()[0]}!",
                body_html=(
                    f"We got your message and will get back to you shortly.<br><br>"
                    f"<strong>Your message:</strong><br>"
                    f"<div style='background:#f7f7f8;border-radius:8px;padding:14px 16px;margin-top:8px;"
                    f"font-size:13px;color:#3a3c42;'>{message}</div>"
                ),
                plain_text=(
                    f"Hi {name},\n\nWe received your message and will get back to you soon.\n\n"
                    f"Your message:\n{message}\n\nAvants AI Team"
                ),
            )

            flash("Message sent! Check your email for confirmation.", "success")
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
