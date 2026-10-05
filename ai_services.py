"""Shared OpenAI-backed helpers used by both app.py (top-level routes) and the
website/ blueprints. Pulled out of app.py so blueprints can import these
without circular-importing the Flask app itself."""
import io
import os
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

client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


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
    response = client.chat.completions.create(
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
        response = client.chat.completions.create(
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
    response = client.chat.completions.create(
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
    response = client.chat.completions.create(
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


def suggest_candidates_for_jobs(jobs, candidates):
    """Reverse of suggest_jobs_for_profile: rank a company's open-to-work talent
    pool against its own open job postings and return the best-fitting
    candidates. `jobs` is a list of dicts with id/title/category/description.
    `candidates` is a list of dicts with id/headline/skills/target_roles/
    years_experience. Returns a list of dicts (candidate_id, score, reason),
    best match first, capped to candidates the model considers a real fit."""
    if not jobs or not candidates:
        return []

    jobs_block = "\n\n".join(
        f"- {job['title']} ({job['category']}): {job['description'][:400]}"
        for job in jobs
    )
    candidates_block = "\n\n".join(
        f"[Candidate ID: {c['id']}] {c.get('headline') or 'No headline given'}\n"
        f"Skills: {c.get('skills') or 'none listed'}\n"
        f"Target roles: {c.get('target_roles') or 'none listed'}\n"
        f"Experience: {c.get('years_experience') or 'unspecified'}"
        for c in candidates
    )

    prompt = f"""
A company has these open job postings:
{jobs_block}

Here is a pool of candidates who are currently open to work:
{candidates_block}

Identify which candidates are a genuinely good fit for at least one of these open roles. Only include
candidates who are a reasonable match - do not pad the list with poor fits just to fill space.

Respond with one block per recommended candidate, ranked best fit first, EXACTLY in this format (no
asterisks, no markdown bold, no extra commentary):

Candidate ID: <id>
Score: <number 0-100>
Reason: <1-2 sentences, specific to this candidate's actual background and which role they fit>
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()

    results = []
    for block in re.split(r"\n(?=Candidate ID:)", content):
        id_match = re.search(r"Candidate ID:\s*(\d+)", block)
        score_match = re.search(r"Score:\s*(\d{1,3})", block)
        reason_match = re.search(r"Reason:\s*(.+)", block, re.DOTALL)
        if not id_match:
            continue
        results.append({
            "candidate_id": int(id_match.group(1)),
            "score": int(score_match.group(1)) if score_match else 0,
            "reason": reason_match.group(1).strip() if reason_match else "",
        })
    return results


def find_similar_jobs(current_job, other_jobs):
    """Given a job dict (id, title, description) and a list of other open jobs,
    return the 3 most semantically similar ones. Similarity is based on role
    responsibilities and required skills, not category labels.

    Returns list of dicts: {job_id, similarity, reason}"""
    if not other_jobs:
        return []

    current_block = (
        f"Title: {current_job['title']}\n"
        f"Description: {current_job['description'][:800]}"
    )
    candidates_block = "\n\n".join(
        f"[Job ID: {j['id']}] {j['title']} — {j['company']}\n{j['description'][:500]}"
        for j in other_jobs
    )

    prompt = f"""You are a job similarity engine. Given a reference job and a list of other open roles, identify the 3 most similar jobs based on the actual role responsibilities, required skills, and seniority level — NOT by job category label.

Reference job:
{current_block}

Other open jobs:
{candidates_block}

Pick the 3 most similar jobs and respond EXACTLY in this format (no extra text, no markdown):

Job ID: <id>
Similarity: <number 0-100>
Reason: <one short sentence explaining the similarity>

Job ID: <id>
Similarity: <number 0-100>
Reason: <one short sentence>

Job ID: <id>
Similarity: <number 0-100>
Reason: <one short sentence>"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()

    results = []
    for block in re.split(r"\n(?=Job ID:)", content):
        id_match = re.search(r"Job ID:\s*(\d+)", block)
        sim_match = re.search(r"Similarity:\s*(\d{1,3})", block)
        reason_match = re.search(r"Reason:\s*(.+)", block, re.DOTALL)
        if not id_match:
            continue
        results.append({
            "job_id": int(id_match.group(1)),
            "similarity": int(sim_match.group(1)) if sim_match else 0,
            "reason": reason_match.group(1).strip() if reason_match else "",
        })
    return results[:3]


def generate_rejection_feedback(job_title, company_name, match_score, match_explanation, interview_verdict, interview_transcript):
    """Draft a short, honest, encouraging rejection feedback note from employer to candidate.
    The employer will review and optionally edit before sending."""
    score_context = f"Their match score was {match_score}/100." if match_score is not None else ""
    verdict_context = f"The AI interview verdict was: {interview_verdict}." if interview_verdict else "They did not complete an AI interview."
    explanation_context = match_explanation[:600] if match_explanation else ""
    transcript_snippet = (interview_transcript or "")[:800]

    prompt = f"""You are writing a brief, honest rejection feedback note on behalf of a hiring company to a job candidate. The note should:
- Be 3-5 sentences maximum
- Be warm and encouraging in tone
- Give specific, actionable feedback (what gaps existed, what would strengthen future applications)
- NOT be generic (avoid "we had many strong candidates")
- NOT mention the AI interview or match scoring system by name
- Be written as if from the employer directly to the candidate

Context:
- Role: {job_title} at {company_name}
- {score_context}
- {verdict_context}
- Match analysis: {explanation_context}
- Interview highlights: {transcript_snippet}

Write only the feedback message text. No subject line, no greeting, no sign-off."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=250,
        temperature=0.5,
    )
    return response.choices[0].message.content.strip()


def analyze_job_description(title, about, requirements, preferred, what_you_do, contract_terms, salary_provided, category):
    """Analyze a job description and return a quality score + specific improvement suggestions.
    Returns {score: int, issues: [{level: 'error'|'warning'|'tip', text: str}]}"""

    salary_note = "Salary range: NOT provided." if not salary_provided else "Salary range: provided."
    desc_block = "\n".join(filter(None, [
        f"Title: {title}",
        f"Category: {category}",
        salary_note,
        f"About the role:\n{about[:600]}" if about else "",
        f"Requirements:\n{requirements[:600]}" if requirements else "",
        f"Preferred:\n{preferred[:400]}" if preferred else "",
        f"What you'll do:\n{what_you_do[:400]}" if what_you_do else "",
        f"Contract terms:\n{contract_terms[:300]}" if contract_terms else "",
    ]))

    prompt = f"""You are an expert recruiter reviewing a job posting for quality and candidate appeal. Analyze the posting below and return:
1. An overall quality score from 0-100
2. A list of specific, actionable issues (errors, warnings, or tips)

Scoring guide:
- 85-100: Excellent — clear, complete, specific, competitive
- 70-84: Good — minor gaps
- 50-69: Fair — missing important elements
- Below 50: Needs work — major issues

Issue levels:
- error: Missing or critically wrong (e.g., no description at all, discriminatory language)
- warning: Important gap that reduces applications (e.g., no salary, vague requirements)
- tip: Improvement opportunity (e.g., requirements list too long, could be more specific)

Job posting to analyze:
{desc_block}

Respond EXACTLY in this format (no extra text):
Score: <number>
Issue: <level> | <specific actionable feedback, 1 sentence>
Issue: <level> | <specific actionable feedback, 1 sentence>
...

Include 3-7 issues total. Be specific to this actual posting — do not give generic advice."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()

    score = 70
    issues = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("Score:"):
            try:
                score = int(re.search(r"\d+", line).group())
            except Exception:
                pass
        elif line.startswith("Issue:"):
            rest = line[6:].strip()
            if "|" in rest:
                level, text = rest.split("|", 1)
                level = level.strip().lower()
                if level not in ("error", "warning", "tip"):
                    level = "tip"
                issues.append({"level": level, "text": text.strip()})

    return {"score": min(100, max(0, score)), "issues": issues}


def generate_interview_coaching(transcript, job_title, requirements, verdict, match_score, resume=None):
    """Analyze a candidate's interview transcript and produce a private coaching report.
    Returns a dict with: communication_score, strengths (list), improvements (list), tips (list).
    Raises ValueError if the model's output doesn't parse into a usable report - callers
    should treat that as "temporarily unavailable," not silently show a fabricated score.

    When a resume is supplied, the model is also asked to cross-check what was actually
    said against what the resume claims - flagging unsupported or inflated claims as an
    Improvement rather than just grading communication style in isolation."""
    resume_block = (
        f"\nCandidate's resume (verify their answers against this - flag anything they "
        f"claimed in the interview that isn't backed up here, or that contradicts it):\n"
        f"{resume[:2000]}\n"
    ) if resume else ""

    prompt = f"""You are a strict, critical communication coach analyzing a job interview. Your report is private — the candidate will use it to improve. Grade rigorously: most real candidates should NOT score above 75. Do not be encouraging or generous — an unspecific, rambling, or filler-heavy answer is a weak answer regardless of confidence or tone.

Job: {job_title}
Requirements: {(requirements or '')[:400]}
AI verdict: {verdict or 'Not yet determined'}
Match score: {match_score}/100 (0–100 scale)
{resume_block}
Interview transcript:
{transcript[:3000]}

Produce a coaching report with:
1. A Communication Score (0–100) based on confidence, clarity, depth, and specificity. Score anchors:
   - 90-100: exceptional — every answer specific, quantified, and directly relevant. Rare.
   - 70-89: solid — mostly specific with concrete examples, a few vague moments.
   - 50-69: mixed — some substance but noticeably vague, generic, or unfocused answers.
   - Below 50: weak — mostly vague, evasive, filler-heavy, or off-topic answers.
2. Exactly 2-3 Strengths — each MUST quote or closely paraphrase a specific moment from the transcript. Do not invent generic strengths ("good communicator") with no textual basis.
3. Exactly 2-3 Improvements — concrete, specific gaps tied to actual answers given, not generic advice like "be more confident."{" At least one Improvement MUST address any claim in the transcript that the resume doesn't support or contradicts, if one exists." if resume else ""}
4. Exactly 2-3 actionable Tips for their next interview, specific to the gaps you identified above.

If the transcript is too short or low-effort to support a real assessment, say so honestly with a low score rather than padding the report.

Respond EXACTLY in this format, with no extra commentary:
CommunicationScore: <number>
Strength: <1 sentence referencing something specific they said>
Strength: <1 sentence>
Improvement: <1 sentence identifying a specific gap>
Improvement: <1 sentence>
Tip: <1 sentence, concrete and actionable>
Tip: <1 sentence>
Tip: <1 sentence>"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()

    result = {
        "communication_score": None,
        "strengths": [],
        "improvements": [],
        "tips": [],
    }
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("CommunicationScore:"):
            match = re.search(r"\d+", line)
            if match:
                result["communication_score"] = max(0, min(100, int(match.group())))
        elif line.startswith("Strength:"):
            result["strengths"].append(line[9:].strip())
        elif line.startswith("Improvement:"):
            result["improvements"].append(line[12:].strip())
        elif line.startswith("Tip:"):
            result["tips"].append(line[4:].strip())

    if (result["communication_score"] is None or not result["strengths"]
            or not result["improvements"] or not result["tips"]):
        raise ValueError(f"Coaching report did not parse into a complete result: {content[:200]!r}")

    return result


def generate_practice_questions(job_description, resume=None):
    """Exactly 10 interview questions tailored to a job description (and,
    when provided, a candidate's resume) for the resume job-match page's
    video scan - shown after a scan completes so the candidate has
    something concrete to practice next, not a generic question bank."""
    resume_block = (
        f"\nCandidate's resume - tailor several questions to probe specific "
        f"experience it lists (depth, not just restating it):\n{resume[:2000]}\n"
    ) if resume else ""

    prompt = f"""Draft exactly 10 interview questions for this role, the kind a candidate should practice answering before a real interview.

Job Description: {(job_description or '')[:2000]}
{resume_block}
Cover a mix: a few background/motivation questions, several hands-on experience questions tied to specifics in the job description{" and the candidate's resume" if resume else ""}, at least one behavioral/situational question, and at least one question probing a likely weak spot relative to the role's requirements.

Respond with ONLY the 10 questions, one per line, no numbering, no extra commentary."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.7,
    )
    lines = response.choices[0].message.content.strip().split("\n")
    questions = [line.strip().lstrip("-•0123456789. ").strip() for line in lines if line.strip()]
    return questions[:10]


def generate_skills_roadmap(job_title, match_explanation, match_score, requirements):
    """Generate a specific 30-day action plan to close the skills gap for a role.
    Returns a list of {step, description, timeline} dicts."""
    prompt = f"""A candidate applied for {job_title} and scored {match_score}/100. Generate a specific, actionable 30-day plan to help them become competitive for this type of role.

Their match analysis:
{(match_explanation or '')[:800]}

Job requirements:
{(requirements or '')[:500]}

Create 4–5 specific steps. Each step must be concrete (name a specific skill, course type, or project to build — not just "learn Python"). Focus only on closing the actual gaps identified above.

Respond EXACTLY in this format:
Step: <short title (3-6 words)>
Description: <1-2 sentences — what exactly to do and why it closes a gap>
Timeline: <e.g. "Week 1–2" or "Days 1–7">

Step: ...
Description: ...
Timeline: ..."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()

    steps = []
    current = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("Step:"):
            if current.get("step"):
                steps.append(current)
            current = {"step": line[5:].strip(), "description": "", "timeline": ""}
        elif line.startswith("Description:"):
            current["description"] = line[12:].strip()
        elif line.startswith("Timeline:"):
            current["timeline"] = line[9:].strip()
    if current.get("step"):
        steps.append(current)
    return steps[:5]


VERDICT_CRITERIA = (
    "Grade strictly - most candidates should NOT get 'Strong Yes,' and plenty should not get 'Yes' either. "
    "A real recruiter screen filters out the majority of applicants; treat this the same way, not as a "
    "formality on the way to a job offer.\n"
    "- Strong Yes: exceptional, specific, quantified answers across every area probed, with no real "
    "weaknesses surfaced. Rare - reserved for candidates a hiring manager would want to fast-track.\n"
    "- Yes: solid, mostly specific answers backed by real relevant experience, with only minor gaps. Would "
    "genuinely move forward to the next round.\n"
    "- No: vague, generic, unconvincing, or off-target answers; real gaps relative to the role; rambling "
    "instead of concrete specifics. This is where most candidates who don't clearly stand out belong.\n"
    "- Strong No: evasive, contradictory, clearly unqualified, or repeatedly unable to give a concrete answer "
    "when pushed.\n"
    "Confidence and articulateness alone are NOT enough for a Yes - the content must be specific and "
    "substantiated. When genuinely torn between two verdicts, choose the stricter one."
)

VERDICT_FORMAT = (
    "Verdict: <Strong Yes / Yes / No / Strong No>\n\n"
    "Why:\n"
    "+ <specific strength from the conversation>\n"
    "+ <another specific strength, if there genuinely is one>\n"
    "- <specific concern or gap from the conversation>\n"
    "- <another specific concern or gap, if there genuinely is one>\n"
    "(List every substantive strength and every substantive concern you actually observed - don't omit a "
    "real one just to fit a fixed count, and don't pad with trivial or repetitive points to hit one either. "
    "Typically 2-5 of each is honest for a normal-length interview. Be complete about both sides, not just "
    "enough to justify the verdict.)"
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
                        interview_length="standard", interview_focus=None, interview_questions=None,
                        practice_mode=False):
    """One turn of the AI recruiter-screen interview. The model alone decides
    when it has enough signal to stop asking questions and issue a verdict —
    no fixed question cap, just a length steer (short/standard/thorough) the
    employer chose for this job. force_conclude is only set by an explicit
    candidate "wrap up" action. Returns the assistant's reply text; the caller
    detects conclusion by checking whether the reply contains a "Verdict:"
    line, not by a precomputed flag.

    practice_mode=True is used for the candidate-facing practice interview
    (no real employer questions passed in, nothing saved or scored for real).
    It steers toward more concrete, role-specific questions grounded in the
    actual job description instead of generic templates like "tell me about
    a time..." with no connection to the role, since the whole point of
    practice is rehearsing for what this specific interview will look like."""
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
            f"Output a final verdict in EXACTLY this format:\n\n{VERDICT_FORMAT}\n\n"
            f"How to decide the verdict itself:\n{VERDICT_CRITERIA}"
        )
    else:
        decision_rule = (
            f"- YOU alone decide when you have enough signal to evaluate the candidate. {length_guidance}\n"
            f"- Once you have enough signal, end the interview by outputting a final verdict in EXACTLY this format instead of another question:\n\n{VERDICT_FORMAT}\n\n"
            f"How to decide the verdict itself:\n{VERDICT_CRITERIA}\n\n"
            "- Otherwise, continue with exactly one focused follow-up question, following the structure above."
        )

    practice_note = (
        "\nThis is a PRACTICE run - the candidate is rehearsing before their real interview, nothing here is "
        "saved or scored for a real hiring decision. Still run it like a real screen, but make every question "
        "concrete and specific to THIS job description (name the actual tools, responsibilities, or requirements "
        "it mentions) rather than a generic template question that could apply to any role. A candidate "
        "practicing should walk away knowing what this specific interview will actually probe.\n"
    ) if practice_mode else ""

    system_prompt = (
        "You are an experienced recruiter/hiring manager conducting a real screening interview for this "
        "specific role — not an interrogation, but a focused professional conversation. You ask ONE question "
        "at a time, push for concrete specifics and numbers instead of vague claims, and redirect rambling "
        "with a direct follow-up.\n"
        f"{practice_note}"
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

    response = client.chat.completions.create(
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
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.7,
    )
    lines = response.choices[0].message.content.strip().split("\n")
    return [line.strip().lstrip("-•0123456789. ").strip() for line in lines if line.strip()]


def analyze_ai_likelihood(transcript, job_title):
    """Estimate the probability that a candidate's interview answers were
    AI-generated (e.g. typed into ChatGPT and read aloud) rather than authentic.
    Returns a dict: {score: 0-100, label: str, reasons: [str]}"""
    candidate_lines = []
    for line in transcript.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("user:"):
            candidate_lines.append(stripped[5:].strip())

    if not candidate_lines:
        return {"score": 0, "label": "No data", "reasons": []}

    candidate_text = "\n\n".join(candidate_lines)

    prompt = f"""You are a hiring expert analyzing job interview answers to detect whether they were AI-generated (e.g. the candidate used ChatGPT to write answers and read or pasted them) rather than authentic spoken responses.

Job: {job_title}

Candidate's answers only:
{candidate_text[:3000]}

Analyze for these specific AI signals:
1. Formulaic structure (e.g. "First I would X, then Y, finally Z" repeated across answers)
2. Dense corporate buzzwords used unnaturally ("leverage", "synergize", "holistic approach", "proactive mindset")
3. Suspiciously comprehensive coverage — real humans miss things; AI answers every angle
4. Missing personal specifics — no real names, company names, concrete dates, or actual numbers from memory
5. Zero natural speech disfluencies — no self-corrections, incomplete thoughts, or casual phrasing
6. Answers that parrot the question's exact wording back verbatim
7. Eerily consistent tone, vocabulary level, and answer length across all questions
8. Vague experience claims without supporting detail ("I have extensive experience in X")

Score 0-100 where 0 = clearly authentic human speech and 100 = almost certainly AI-generated or heavily AI-edited.
Give 3 specific reasons citing actual phrases or patterns from the text above.

Respond EXACTLY in this format (no extra text):
Score: <number>
Reason: <one specific observation — quote or name a phrase from the answers>
Reason: <another specific observation>
Reason: <another specific observation>"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=350,
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()

    result = {"score": 0, "label": "Human-like", "reasons": []}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("Score:"):
            try:
                result["score"] = min(100, max(0, int(re.search(r"\d+", line).group())))
            except Exception:
                pass
        elif line.startswith("Reason:"):
            result["reasons"].append(line[7:].strip())

    score = result["score"]
    if score < 35:
        result["label"] = "Human-like"
    elif score < 65:
        result["label"] = "Uncertain"
    else:
        result["label"] = "Likely AI-assisted"

    return result


def analyze_platform_feedback(feedback_rows):
    """Summarize a batch of candidate/employer feedback (rating + comment) into
    a short overview plus specific, actionable recommendations for admins to
    review - this only produces suggestions, it never changes anything itself.
    feedback_rows: list of {role, context, rating, comment} dicts.
    Returns {summary: str, recommendations: [{title, detail}]}."""
    if not feedback_rows:
        return {"summary": "No feedback yet.", "recommendations": []}

    avg_rating = sum(r["rating"] for r in feedback_rows) / len(feedback_rows)

    lines = []
    for r in feedback_rows[:200]:
        comment = (r.get("comment") or "").strip().replace("\n", " ")
        lines.append(f"[{r['role']} | {r['context']} | {r['rating']}/5] {comment or '(no comment)'}")
    feedback_text = "\n".join(lines)

    prompt = f"""You are analyzing user feedback for an AI-powered hiring platform (candidates apply, get AI-interviewed, \
get scored; employers post jobs and review AI-screened applicants). Below is a batch of {len(feedback_rows)} feedback \
entries from candidates and employers, average rating {avg_rating:.1f}/5.

{feedback_text[:8000]}

Identify real, recurring patterns - not one-off complaints. Write a short overall summary, then 2-5 specific, \
actionable recommendations. Each recommendation must name a concrete, viable change (not vague advice like "improve \
communication") and say which group it's based on (candidates, employers, or both). If the feedback is too sparse or \
inconsistent to support a confident recommendation, say so instead of inventing one.

Respond EXACTLY in this format:
Summary: <2-3 sentences on the overall picture>

Recommendation: <short title (4-8 words)>
Based on: <candidates / employers / both>
Detail: <1-2 sentences - the specific, concrete change and why the feedback supports it>

Recommendation: ...
Based on: ...
Detail: ..."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=700,
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()

    summary = ""
    recommendations = []
    current = None
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("Summary:"):
            summary = line[8:].strip()
        elif line.startswith("Recommendation:"):
            if current and current.get("title"):
                recommendations.append(current)
            current = {"title": line[16:].strip(), "based_on": "", "detail": ""}
        elif line.startswith("Based on:") and current is not None:
            current["based_on"] = line[9:].strip()
        elif line.startswith("Detail:") and current is not None:
            current["detail"] = line[7:].strip()
    if current and current.get("title"):
        recommendations.append(current)

    return {"summary": summary or content[:400], "recommendations": recommendations[:5]}


class AudioTooLargeError(Exception):
    """Raised when a recorded answer is too long/large for Whisper's 25MB
    cap (likely hit with the camera on, since video is far larger per
    second than audio-only) - callers should show a specific, actionable
    message instead of the generic transcription-failed one."""
    pass


def extract_audio_for_transcription(input_path):
    """Strips an uploaded video down to just its audio track, compressed to
    a small mono MP3, before handing it to Whisper - a video file's picture
    data is what actually eats into the 25MB cap, not the speech, so this
    lets much longer uploads fit under that same fixed limit. Falls back to
    returning the original path untouched (not raising) if ffmpeg isn't on
    PATH or the conversion fails for any reason - callers always get back a
    valid, existing file path, just not necessarily a smaller one."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return input_path

    out_path = f"{input_path}.audio.mp3"
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", input_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", out_path],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and os.path.getsize(out_path) > 0:
            return out_path
    except Exception:
        logger.exception("ffmpeg audio extraction failed - falling back to the original upload")

    if os.path.exists(out_path):
        try:
            os.unlink(out_path)
        except OSError:
            pass
    return input_path


def transcribe_audio(file_path):
    """Speech-to-text for a candidate's recorded interview answer."""
    with open(file_path, "rb") as f:
        try:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=f)
        except openai.BadRequestError as e:
            if "size" in str(e).lower() or "25 mb" in str(e).lower() or "maximum content" in str(e).lower():
                raise AudioTooLargeError(
                    "That answer was too long to transcribe - recordings are capped at 25MB, which "
                    "video answers can hit in roughly a minute or two depending on quality. Try a "
                    "shorter answer, or turn off your camera for longer responses."
                ) from e
            raise
    return transcript.text.strip()


_ALLOWED_AUDIO_EXTENSIONS = {".webm", ".mp4", ".ogg", ".m4a", ".wav", ".mp3"}


def audio_file_extension(uploaded_filename):
    """Picks the suffix for the temp file a recorded answer gets written to
    before transcription, from whatever filename the browser's upload sent.
    Matters because Whisper's API decides how to decode the file from this
    suffix - Safari (desktop and iOS) doesn't support WebM at all and records
    mp4 instead, so hardcoding ".webm" regardless of what was actually
    uploaded silently breaks transcription on any non-WebM browser."""
    import os as _os
    ext = _os.path.splitext(uploaded_filename or "")[1].lower()
    return ext if ext in _ALLOWED_AUDIO_EXTENSIONS else ".webm"


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
