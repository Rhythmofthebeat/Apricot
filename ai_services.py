"""Shared OpenAI-backed helpers used by both app.py (top-level routes) and the
website/ blueprints. Pulled out of app.py so blueprints can import these
without circular-importing the Flask app itself."""
import io
import logging
import re
from collections import Counter

import openai
from PyPDF2 import PdfReader
from docx import Document
from openai._exceptions import RateLimitError, APIConnectionError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import Config

logger = logging.getLogger(__name__)

openai.api_key = Config.OPENAI_API_KEY


def extract_pdf_text(file_stream):
    reader = PdfReader(file_stream)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


def extract_docx_text(file_stream):
    try:
        file_stream.seek(0)
        document = Document(file_stream)
        text = "\n".join(para.text for para in document.paragraphs if para.text.strip())
        return text if text.strip() else "No text found in DOCX file."
    except Exception as e:
        logger.error(f"Error reading DOCX: {e}", exc_info=True)
        return f"Error reading DOCX: {str(e)}"


def extract_text_from_file(filename: str, resume_bytes: bytes):
    file_stream = io.BytesIO(resume_bytes)
    if filename.endswith('.pdf'):
        return extract_pdf_text(file_stream)
    elif filename.endswith('.docx'):
        return extract_docx_text(file_stream)
    elif filename.endswith('.txt'):
        return file_stream.getvalue().decode('utf-8')
    return ""


def compare_resume_to_job(job_description, resume):
    prompt = f"""
Given the following job description and resume, rate how well the resume matches the job description.

Job Description:
{job_description}

Resume:
{resume}

Respond EXACTLY in this format (no asterisks, no markdown bold):

Score: <number 0-100> (<Poor / Fair / Good / Excellent>)

Explanation:

What the resume MATCHES (prefix each bullet with +):
+ <specific match>
+ <specific match>

What the resume is MISSING or does not match (prefix each bullet with -):
- <specific gap>
- <specific gap>

Education:
+ or - <education match/gap>

Skills / Technical:
+ or - <skill match or gap>

Experience / Internships:
+ or - <experience match or gap>

Project Links (list ALL links found in the resume, no duplicates):
<link>

Rules:
- Use + at the start of every line that is a STRENGTH or MATCH
- Use - at the start of every line that is a GAP or MISSING requirement
- Remove all asterisks
- Be specific — mention exact skills, tools, and requirements from the job description
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=(
        retry_if_exception_type(RateLimitError)
        | retry_if_exception_type(APIConnectionError)
    ),
)
def generate_summary(text):
    """Generate AI resume summary with retry logic."""
    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You're a professional resume analyst. Create a 9-11 sentence summary "
                        "focusing on skills, experience, education, and other notable mentions. "
                        "Highlight KEY details and technical experience specifically. Be concise."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Summarize this resume:\n\n{text[:Config.MAX_TEXT_LENGTH]}",
                },
            ],
            max_tokens=600,
            timeout=Config.OPENAI_TIMEOUT,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"OpenAI API error: {str(e)}")
        raise


def generate_basic_analysis(text):
    """Fallback analysis when AI is unavailable."""
    try:
        words = [w for w in text.split() if len(w) > 3]
        word_counts = Counter(words)
        top_terms = [w[0] for w in word_counts.most_common(5)]
        return {
            "analysis": "Basic Text Analysis",
            "word_count": len(words),
            "top_terms": top_terms,
            "estimated_experience": "Junior" if len(words) < 300 else "Senior",
        }
    except Exception as e:
        logger.error(f"Basic analysis failed: {str(e)}")
        return {"error": "Could not generate basic analysis"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=(
        retry_if_exception_type(RateLimitError)
        | retry_if_exception_type(APIConnectionError)
    ),
)
def generate_resume_from_profile(linkedin_url, profile_text):
    """Turn pasted LinkedIn profile content into a structured resume — for
    candidates who have a LinkedIn presence but no resume on hand."""
    prompt = f"""
A candidate has no resume but has pasted their LinkedIn profile content below.
Turn this into a clean, professional resume.

LinkedIn URL: {linkedin_url or "Not provided"}

Pasted profile content (About / Experience / Education / Skills, in whatever
order the candidate pasted it):
{profile_text[:Config.MAX_TEXT_LENGTH]}

Respond EXACTLY in this format (no asterisks, no markdown bold):

[Name / Title]
<best-guess professional title based on experience>

[Professional Summary]
<3-4 sentence summary>

[Experience]
<Company — Role — Dates>
<2-3 bullet points per role, prefixed with ->

[Education]
<Degree, Institution, Dates>

[Skills]
<comma-separated list>

Rules:
- Only use information present in the pasted content — never invent companies, dates, or credentials.
- If a section has no information, write "Not provided" under that heading instead of guessing.
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


def suggest_jobs_for_profile(profile_text, target_roles, jobs):
    """Rank a candidate's profile/resume against the platform's open job
    postings and return the best-fitting ones. `jobs` is a list of dicts with
    id/title/company/category/description. Returns a list of dicts (id, score,
    reason), best match first, capped to the jobs the model considers a real fit."""
    if not jobs:
        return []

    listings_block = "\n\n".join(
        f"[Job ID: {job['id']}] {job['title']} — {job['company']} ({job['category']})\n"
        f"{job['description'][:500]}"
        for job in jobs
    )
    target_roles_block = f"\nThe candidate also says they're interested in: {target_roles[:300]}\n" if target_roles else ""

    prompt = f"""
A candidate is looking for their next role. Here is their profile/resume:
{profile_text[:Config.MAX_TEXT_LENGTH]}
{target_roles_block}
Here are the open job postings on the platform:
{listings_block}

Identify which of these jobs are a genuinely good fit for this candidate. Only include jobs that are a
reasonable match — do not pad the list with poor fits just to fill space.

Respond with one block per recommended job, ranked best fit first, EXACTLY in this format (no asterisks,
no markdown bold, no extra commentary):

Job ID: <id>
Score: <number 0-100>
Reason: <1-2 sentences, specific to this candidate's actual background and this job's requirements>
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()

    results = []
    for block in re.split(r"\n(?=Job ID:)", content):
        id_match = re.search(r"Job ID:\s*(\d+)", block)
        score_match = re.search(r"Score:\s*(\d{1,3})", block)
        reason_match = re.search(r"Reason:\s*(.+)", block, re.DOTALL)
        if not id_match:
            continue
        results.append({
            "job_id": int(id_match.group(1)),
            "score": int(score_match.group(1)) if score_match else 0,
            "reason": reason_match.group(1).strip() if reason_match else "",
        })
    return results


VERDICT_FORMAT = (
    "Verdict: <Strong Yes / Yes / No / Strong No>\n\n"
    "Why:\n"
    "+ <specific strength from the conversation>\n"
    "+ <specific strength from the conversation>\n"
    "- <specific concern or gap from the conversation>\n"
)


INTERVIEW_LENGTH_GUIDANCE = {
    "short": (
        "Keep this brief — aim for about 3-4 total exchanges. Cover background/motivation in 1 question, "
        "one hands-on experience deep-dive, and one weak-spot probe. Skip the behavioral question unless "
        "something concerning comes up that needs follow-up."
    ),
    "standard": (
        "A real recruiter screen for a role like this typically runs 5-8 questions across that structure, "
        "but use your own judgment of when you have enough to make a confident call, not a fixed count."
    ),
    "thorough": (
        "Go deeper than a typical screen — aim for roughly 8-12 exchanges. Spend more time on hands-on "
        "experience (3-4 deep-dives across different projects), ask more than one behavioral/situational "
        "question, and probe multiple potential weak spots relative to the role's requirements."
    ),
}


def run_interview_turn(role, job_description, user_message, history, force_conclude=False,
                        interview_length="standard", interview_focus=None, interview_questions=None):
    """One turn of the AI recruiter-screen interview. The model alone decides
    when it has enough signal to stop asking questions and issue a verdict —
    no fixed question cap, just a length steer (short/standard/thorough) the
    employer chose for this job. force_conclude is only set by an explicit
    candidate "wrap up" action. Returns the assistant's reply text; the caller
    detects conclusion by checking whether the reply contains a "Verdict:"
    line, not by a precomputed flag."""
    context_block = f"\n=== ROLE ===\n{role}\n"
    if job_description:
        context_block += f"\n=== JOB DESCRIPTION ===\n{job_description[:2000]}\n"
    if interview_focus:
        context_block += (
            "\n=== EMPLOYER'S GUIDANCE FOR THIS INTERVIEW (topics to make sure get covered, things to "
            "listen for, and any conditional instructions — e.g. 'if a candidate mentions X, dig into Y' — "
            f"follow these as written) ===\n{interview_focus[:2000]}\n"
        )
    if interview_questions:
        questions_block = "\n".join(f"- {q}" for q in interview_questions[:15])
        context_block += (
            "\n=== EMPLOYER-WRITTEN QUESTIONS (work these in verbatim or near-verbatim over "
            f"the course of the interview) ===\n{questions_block}\n"
        )

    length_guidance = INTERVIEW_LENGTH_GUIDANCE.get(interview_length, INTERVIEW_LENGTH_GUIDANCE["standard"])

    if force_conclude:
        decision_rule = (
            "- The candidate has asked to wrap up. Do NOT ask another question. "
            f"Output a final verdict in EXACTLY this format:\n\n{VERDICT_FORMAT}"
        )
    else:
        decision_rule = (
            f"- YOU alone decide when you have enough signal to evaluate the candidate. {length_guidance}\n"
            f"- Once you have enough signal, end the interview by outputting a final verdict in EXACTLY this format instead of another question:\n\n{VERDICT_FORMAT}\n"
            "- Otherwise, continue with exactly one focused follow-up question, following the structure above."
        )

    system_prompt = (
        "You are an experienced recruiter/hiring manager conducting a real screening interview for this "
        "specific role — not an interrogation, but a focused professional conversation. You ask ONE question "
        "at a time, push for concrete specifics and numbers instead of vague claims, and redirect rambling "
        "with a direct follow-up.\n"
        f"{context_block}\n"
        "Structure the interview the way a real recruiter screen actually runs:\n"
        "1. Open with a quick question on background/motivation for this specific role.\n"
        "2. Spend 2-3 questions probing concrete hands-on experience and a specific project/result tied to the role.\n"
        "3. Ask one behavioral/situational question (e.g. handling a conflict, a failure, a tight deadline).\n"
        "4. Probe one likely weak spot relative to the role's requirements.\n"
        "5. If the employer left guidance above, follow it exactly — cover any topics it names, listen for "
        "what it says to listen for, and act on any conditional instructions the moment they apply (e.g. if "
        "it says to dig deeper when a candidate mentions something specific, do that the instant they do).\n"
        "6. If the employer wrote specific questions above, work every one of them into the conversation "
        "(verbatim or lightly reworded to fit the flow) in addition to the structure above.\n\n"
        "Guidelines:\n"
        "- Ask exactly one question per turn. No preamble, no encouragement, no filler.\n"
        "- If the candidate's answer is vague, demand a specific number, example, or outcome before moving on.\n"
        f"{decision_rule}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-20:]:
        if msg.get("role") in ("user", "assistant") and msg.get("content"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    if user_message:
        messages.append({"role": "user", "content": user_message})
    elif not history:
        messages.append({"role": "user", "content": "Begin the interview."})

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=400,
        temperature=0.6,
    )
    return response.choices[0].message.content.strip()


QUESTION_BANK_SIZE = {
    "short": 8,
    "standard": 12,
    "thorough": 15,
}


def generate_interview_question_preview(title, description, interview_length="standard", interview_focus=None):
    """Generates a full starting question bank for an employer to review and
    edit while setting up a job posting. The employer can edit, remove, or
    add to this list — whatever they keep gets worked into the real AI
    interview (see run_interview_turn's interview_questions param), so this
    is no longer purely illustrative. Returns a list of question strings."""
    length_guidance = INTERVIEW_LENGTH_GUIDANCE.get(interview_length, INTERVIEW_LENGTH_GUIDANCE["standard"])
    target_count = QUESTION_BANK_SIZE.get(interview_length, QUESTION_BANK_SIZE["standard"])
    focus_block = f"\nThe employer's guidance for this interview: {interview_focus[:2000]}\n" if interview_focus else ""

    prompt = f"""
You are drafting a full bank of recruiter-screen interview questions for this role. The employer will review
this list and can edit, remove, or add questions — whatever they keep will actually be asked of every
candidate, so make each question concrete and worth asking, not filler.

Role: {title}
Job Description: {description[:2000]}
{focus_block}
Length guidance: {length_guidance}

List {target_count} questions covering: background/motivation, several hands-on experience questions tied
to specifics in the job description, behavioral/situational questions, and weak-spot probes relative to the
role's requirements{"" if not interview_focus else ", plus at least one question per employer-requested focus area"}.

Respond with ONLY the questions, one per line, no numbering, no extra commentary.
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.7,
    )
    lines = response.choices[0].message.content.strip().split("\n")
    return [line.strip().lstrip("-•0123456789. ").strip() for line in lines if line.strip()]


def transcribe_audio(file_path):
    """Speech-to-text for a candidate's recorded interview answer."""
    with open(file_path, "rb") as f:
        transcript = openai.audio.transcriptions.create(model="whisper-1", file=f)
    return transcript.text.strip()


def synthesize_speech(text, out_path):
    """Text-to-speech for an AI interview question — mirrors app.py's /speak route."""
    from google.cloud import texttospeech

    tts_client = texttospeech.TextToSpeechClient()
    input_text = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name="en-US-Wavenet-D",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
    )
    response = tts_client.synthesize_speech(input=input_text, voice=voice, audio_config=audio_config)
    with open(out_path, "wb") as out:
        out.write(response.audio_content)
