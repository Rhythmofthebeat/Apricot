"""Employer email verification — token generation/validation plus the actual
send. Token is stateless (signed + timestamped), so no extra DB table is
needed; the email send depends on MAIL_USERNAME/MAIL_PASSWORD being set in
.env (Gmail SMTP + App Password) — until then, sending raises and callers
should surface a clear "couldn't send" message rather than pretending it worked.
"""
from itsdangerous import URLSafeTimedSerializer
from flask import current_app, url_for
from flask_mail import Message

from website import mail

VERIFICATION_SALT = "employer-email-verification"
VERIFICATION_MAX_AGE = 60 * 60 * 24  # 24 hours


LOGO_URL = "https://apricot-u9ap.onrender.com/static/Avants.png"


def render_branded_email(heading, body_html, cta_label=None, cta_url=None, accent="#4f46e5"):
    """Shared HTML shell for every transactional email — inline styles only."""
    cta_block = ""
    if cta_label and cta_url:
        cta_block = f"""
        <tr>
            <td style="padding: 8px 40px 32px;">
                <a href="{cta_url}" style="display:inline-block; background:{accent}; color:#ffffff;
                    text-decoration:none; font-weight:600; font-size:14px; padding:12px 28px;
                    border-radius:8px; font-family:Arial,Helvetica,sans-serif;">{cta_label}</a>
            </td>
        </tr>
        """
    logo_html = (
        f'<img src="{LOGO_URL}" alt="Avants AI" style="height:32px; display:block;" />'
    )

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f8; padding:32px 0;">
        <tr>
            <td align="center">
                <table role="presentation" width="560" cellpadding="0" cellspacing="0"
                    style="background:#ffffff; border-radius:12px; overflow:hidden; border:1px solid #e6e7eb;
                    font-family:Arial,Helvetica,sans-serif;">
                    <tr>
                        <td style="background:{accent}; padding:20px 40px;">
                            {logo_html}
                        </td>
                    </tr>
                    <tr>
                        <td style="padding:32px 40px 8px;">
                            <h1 style="margin:0 0 16px; font-size:20px; color:#15161a;">{heading}</h1>
                            <div style="font-size:14px; line-height:1.6; color:#3a3c42;">{body_html}</div>
                        </td>
                    </tr>
                    {cta_block}
                    <tr>
                        <td style="padding:20px 40px; border-top:1px solid #e6e7eb; font-size:12px; color:#6b7280;">
                            Sent by Avants AI &mdash; the AI-vetted hiring marketplace.
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
    """


def send_branded_email(to_email, subject, heading, body_html, plain_text, cta_label=None, cta_url=None):
    """Sends a styled HTML email with a plain-text fallback. Raises on failure."""
    msg = Message(
        subject=subject,
        recipients=[to_email],
        body=plain_text,
        html=render_branded_email(heading, body_html, cta_label, cta_url),
    )
    mail.send(msg)


def generate_verification_token(email):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    return serializer.dumps(email, salt=VERIFICATION_SALT)


def verify_verification_token(token):
    """Returns the email the token was issued for, or None if invalid/expired."""
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        return serializer.loads(token, salt=VERIFICATION_SALT, max_age=VERIFICATION_MAX_AGE)
    except Exception:
        return None


def send_verification_email(user):
    """Raises if MAIL_USERNAME/MAIL_PASSWORD aren't configured or sending fails."""
    if not current_app.config.get("MAIL_USERNAME") or not current_app.config.get("MAIL_PASSWORD"):
        raise RuntimeError("Email sending is not configured (MAIL_USERNAME/MAIL_PASSWORD missing).")

    token = generate_verification_token(user.email)
    verify_url = url_for("auth.verify_email", token=token, _external=True)

    send_branded_email(
        to_email=user.email,
        subject="Confirm your email — Avants AI",
        heading="Confirm your email address",
        body_html=(
            f"Hi {user.first_name or 'there'},<br><br>"
            "Click the button below to verify your email and activate your Avants account."
        ),
        plain_text=(
            f"Hi {user.first_name or 'there'},\n\n"
            f"Verify your email to activate your account:\n\n"
            f"{verify_url}\n\n"
            f"This link expires in 24 hours."
        ),
        cta_label="Verify Email",
        cta_url=verify_url,
    )
