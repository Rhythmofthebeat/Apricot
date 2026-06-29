import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    DAILY_API_KEY = os.getenv("DAILY_API_KEY")
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
    STRIPE_STARTER_PRICE_ID = os.getenv("STRIPE_STARTER_PRICE_ID")   # monthly recurring price
    STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID")
    PLATFORM_FEE_PERCENT = 12.5
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = ("Avants AI", os.getenv("MAIL_USERNAME")) if os.getenv("MAIL_USERNAME") else None
    GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
    AUDIO_FOLDER = os.getenv("AUDIO_FOLDER", "audio")
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

