import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Real public domain used to build links sent in emails - never inferred
    # from the triggering request's Host header (see email_utils.external_url).
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://apricot-u9ap.onrender.com")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    DAILY_API_KEY = os.getenv("DAILY_API_KEY")
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
    # Data-marketplace subscription (Earn task exports sold to outside companies) -
    # separate price/webhook from the placement-fee flow above.
    STRIPE_DATA_PRICE_ID = os.getenv("STRIPE_DATA_PRICE_ID")
    STRIPE_DATA_WEBHOOK_SECRET = os.getenv("STRIPE_DATA_WEBHOOK_SECRET")
    # Employer data products (applicant export, score calibration, benchmarks)
    # are open to everyone until this is "true". Don't enable it before a
    # checkout flow that creates EmployerSubscription rows exists.
    DATA_PRODUCTS_PAID = os.getenv("DATA_PRODUCTS_PAID", "false").lower() == "true"
    # Live voice interviewer (OpenAI Realtime over WebRTC). Off by default - it
    # bills per minute of audio, unlike the record-then-reply interview.
    LIVE_INTERVIEW_ENABLED = os.getenv("LIVE_INTERVIEW_ENABLED", "false").lower() == "true"
    OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
    OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "marin")
    LIVE_INTERVIEW_MAX_SECONDS = int(os.getenv("LIVE_INTERVIEW_MAX_SECONDS", "1200"))
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = ("Avants AI", os.getenv("MAIL_USERNAME")) if os.getenv("MAIL_USERNAME") else None
    GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    # UPLOAD_FOLDER / AUDIO_FOLDER are set as absolute paths in create_app() -
    # not defined here so app.config.from_object(Config) doesn't clobber them.
    ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
    MAX_TEXT_LENGTH = 15000  # Characters to send to OpenAI
    OPENAI_TIMEOUT = 15
    OPENAI_RETRY_ATTEMPTS = 2
    OPENAI_RETRY_DELAY = 5
    SYSTEM_PROMPT = """
You are ResumeAnalyzerGPT, an expert system designed to analyze and summarize resumes with precision. 


Guidelines:
1. Formatting:
   1. Each section title in [brackets] (e.g., [Professional Experience])
   2. Only one empty line between each section
   3. No bullet points
   4. Each section contains a concise 2-sentence summary only
   5. Strict 100-word limit for the entire summary if possible

Example output:

[Professional Title]:
Robotics Engineer with 1 year of experience specializing in building FRC robots and designing mechanical and electrical systems.

[Experience]:
Worked on creating and assembling robotics components, wiring systems, and managing projects in STEM education. Demonstrated leadership in technical projects and mentoring.

[Core Skills]:
Experienced in CAD design, 3D printing, and robotics programming. Strong problem-solving and hands-on engineering abilities.

[Education]:
Pursuing/Completed Bachelor's degree in [Field] from [Institution], with focus on engineering and technology.

[Summary]:
Robotics engineer passionate about innovation and STEM outreach, with proven ability to lead and deliver technical projects. Strong commitment to continuous learning and industry impact.


2. Analysis Rules:
- Be factual, concise, and objective
- Do not exceed 2 sentences per section
- Avoid lists and technical jargon without explanation

3. Writing Style:
- Third person, professional, concise
- Clear structure, no bullet points
"""
    OPENAI_MODEL = "gpt-4o"

