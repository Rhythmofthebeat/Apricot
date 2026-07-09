from datetime import datetime, timezone
from flask import Blueprint, render_template, request, flash, redirect, url_for, session, make_response
from .models import User, Company, FREE_EMAIL_DOMAINS
from werkzeug.security import generate_password_hash, check_password_hash
from . import db, limiter
from flask_login import login_user, login_required, logout_user, current_user
from supabase import create_client
import os

from email_utils import send_verification_email, verify_verification_token, public_base_url

# ---------------- Supabase Client ---------------- #
# Anon-role client for user-facing auth flows (login, sign-up, password reset request).

supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_ANON_KEY"],
)

auth = Blueprint('auth', __name__)

# ------------------------------------------------ #
# ---------------- PASSWORD RESET ---------------- #
# ------------------------------------------------ #

@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")

        supabase.auth.reset_password_for_email(
            email,
            {"redirect_to": public_base_url() + "/reset-password"}
        )

        flash("Password reset email sent!", "success")
        return redirect(url_for("auth.login"))

    return render_template("forgot_password.html")



@auth.route("/reset-password", methods=["GET"])
def reset_password():
    return render_template(
        "reset_password.html",
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_anon_key=os.environ["SUPABASE_ANON_KEY"],
    )


@auth.route("/reset-password/store", methods=["POST"])
def store_reset_token():
    data = request.get_json()
    session["supabase_access_token"] = data.get("access_token")
    session["supabase_refresh_token"] = data.get("refresh_token")
    return {"status": "ok"}


@auth.route("/reset-password/form", methods=["GET", "POST"])
def reset_password_form():
    if request.method == "POST":
        new_password = request.form.get("password")
        access_token = session.get("supabase_access_token")
        refresh_token = session.get("supabase_refresh_token")

        if not access_token:
            flash("Reset session expired. Try again.", "error")
            return redirect(url_for("auth.login"))

        if len(new_password) < 7:
            flash("Password must be at least 7 characters.", "error")
            return redirect(request.url)
        
        

        try:
            admin_supabase = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            )

            authed_supabase = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_ANON_KEY"],
            )
            authed_supabase.auth.set_session(
                access_token,
                refresh_token
            )
            
            
              # Get Supabase user
            user_response = authed_supabase.auth.get_user(access_token)
            supa_user = user_response.user
            email = supa_user.email

            # Get local user
            user = User.query.filter_by(email=email).first()

            if user:
                # Block reusing old password
                if check_password_hash(user.password, new_password):
                    flash("You cannot use your old password.", "error")
                    return redirect(request.url)

            admin_supabase.auth.admin.update_user_by_id(
                supa_user.id,
                {"password": new_password}
            )

        except Exception as e:
            print("Password update error:", e)
            flash("Error updating password.", "error")
            return redirect(request.url)

        session.pop("supabase_access_token", None)
        flash("Password updated successfully!", "success")
        return redirect(url_for("auth.login"))

    return render_template("reset_password_form.html")





# ------------------------------------------------ #
# ---------------- EMAIL LOGIN ------------------- #
# ------------------------------------------------ #

@auth.route('/login', methods=['GET', 'POST'])
@limiter.limit("20 per minute; 5 per 10 seconds")
def login():
    if current_user.is_authenticated:
        return redirect(url_for('views.home'))

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            res = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
        except Exception as e:
            err = str(e).lower()
            print("Supabase login error:", e)
            if "email not confirmed" in err or "not confirmed" in err:
                flash("Please verify your email before signing in. Check your inbox for a confirmation link.", "error")
                resp = make_response(redirect(url_for("auth.verify_email_prompt")))
                resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
                return resp
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        if not hasattr(res, 'user') or not res.user:
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        if not res.user.email_confirmed_at:
            flash("Please verify your email before signing in. Check your inbox for a confirmation link.", "error")
            resp = make_response(redirect(url_for("auth.verify_email_prompt")))
            resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
            return resp

        # Ensure local user exists for Flask-Login
        local_user = User.query.filter_by(email=email).first()
        if not local_user:
            local_user = User(
                email=email,
                first_name=res.user.user_metadata.get("full_name", ""),
                password=None
            )
            db.session.add(local_user)
            db.session.commit()

        login_user(local_user, remember=True)
        flash("Logged in successfully!", "success")
        return redirect(url_for('views.home'))

    return render_template("login.html")

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

# ---------------- SIGN-UP ----------------
@auth.route('/sign-up', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def sign_up():
    if current_user.is_authenticated:
        return redirect(url_for('views.home'))

    if request.method == 'POST':
        email = request.form.get('email')
        first_name = request.form.get('firstName')
        password1 = request.form.get('password1')
        password2 = request.form.get('password2')
        role = request.form.get('role', 'candidate')
        company_name = request.form.get('companyName', '').strip()

        email_domain = email.split('@')[-1].lower() if email and '@' in email else ''

        if not email:
            flash("Email is required.", "error")
        elif password1 != password2:
            flash("Passwords do not match.", "error")
        elif len(password1) < 7:
            flash("Password must be at least 7 characters.", "error")
        elif role == 'employer' and not company_name:
            flash("Company name is required for employer accounts.", "error")
        elif role == 'employer' and email_domain in FREE_EMAIL_DOMAINS:
            flash("Please sign up with your company email address, not a personal email provider.", "error")
        else:
            try:
                admin_supa = create_client(
                    os.environ["SUPABASE_URL"],
                    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                )
                res = admin_supa.auth.admin.create_user({
                    "email": email,
                    "password": password1,
                    "user_metadata": {"full_name": first_name, "role": role, "company_name": company_name},
                    "email_confirm": False,
                })

                if not res.user:
                    flash("Something went wrong. Try again.", "error")
                    return redirect(url_for("auth.sign_up"))

                local_user = User(
                    email=email,
                    first_name=first_name,
                    password=generate_password_hash(password1),
                    role=role,
                    email_verified=False,
                    supabase_uid=res.user.id,
                )
                db.session.add(local_user)
                if role == 'employer' and company_name and email_domain not in FREE_EMAIL_DOMAINS:
                    company = Company(
                        owner_user_id=local_user.id,
                        name=company_name,
                        terms_accepted_at=datetime.now(timezone.utc),
                        terms_accepted_ip=request.remote_addr,
                        terms_version='1.0',
                    )
                    db.session.add(company)
                db.session.commit()

                try:
                    send_verification_email(local_user)
                except Exception as mail_err:
                    print("Verification email send failed:", mail_err)

                resp = make_response(redirect(url_for("auth.verify_email_prompt")))
                resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
                return resp

            except Exception as e:
                err = str(e).lower()
                if "already registered" in err or "already been registered" in err or "duplicate" in err:
                    flash("Email already exists. Try logging in instead.", "error")
                    return redirect(url_for("auth.login"))

                print("Signup unexpected error:", e)
                flash("Something went wrong. Try again.", "error")
                return redirect(url_for("auth.sign_up"))

    return render_template("sign_up.html")

# ------------------------------------------------ #
# ----------- VERIFY EMAIL PROMPT PAGE ------------ #
# ------------------------------------------------ #

@auth.route("/verify-email")
def verify_email_prompt():
    email = request.cookies.get("pending_verify_email", "")
    return render_template("verify_email.html", email=email)


@auth.route("/resend-verification", methods=["POST"])
def resend_verification():
    email = request.form.get("email", "").strip()
    if email:
        user = User.query.filter_by(email=email).first()
        if user and not user.email_verified:
            try:
                send_verification_email(user)
            except Exception:
                pass
    flash("If that email is registered, a new confirmation link has been sent.", "success")
    resp = make_response(redirect(url_for("auth.verify_email_prompt")))
    resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
    return resp

# ------------------------------------------------ #
# --------- SUPABASE EMAIL CONFIRMATION ----------- #
# ------------------------------------------------ #

@auth.route("/auth/confirm")
def auth_confirm():
    code = request.args.get("code")
    if not code:
        flash("Invalid confirmation link.", "error")
        return redirect(url_for("auth.login"))

    try:
        res = supabase.auth.exchange_code_for_session({"auth_code": code})
        supa_user = res.user
        if not supa_user:
            flash("Confirmation failed. Please try signing up again.", "error")
            return redirect(url_for("auth.sign_up"))

        email = supa_user.email
        meta = supa_user.user_metadata or {}
        role = meta.get("role", "candidate")
        first_name = meta.get("full_name", "")
        company_name = meta.get("company_name", "").strip()
        email_domain = email.split('@')[-1].lower() if '@' in email else ''

        local_user = User.query.filter_by(email=email).first()
        is_new = local_user is None

        if not local_user:
            if role == 'employer' and email_domain in FREE_EMAIL_DOMAINS:
                role = 'candidate'

            local_user = User(
                email=email,
                first_name=first_name,
                password=None,
                role=role,
                email_verified=True,
            )
            db.session.add(local_user)
            db.session.commit()

            if role == 'employer' and company_name:
                company = Company(
                    owner_user_id=local_user.id,
                    name=company_name,
                    terms_accepted_at=datetime.now(timezone.utc),
                    terms_accepted_ip=request.remote_addr,
                    terms_version='1.0',
                )
                db.session.add(company)
                db.session.commit()
        else:
            local_user.email_verified = True
            db.session.commit()

        login_user(local_user, remember=True)
        flash("Email confirmed! Welcome to Avants.", "success")

        if is_new and local_user.role == 'employer' and not Company.query.filter_by(owner_user_id=local_user.id).first():
            return redirect(url_for("auth.complete_employer_profile"))

        return redirect(url_for("views.home"))

    except Exception as e:
        print("Email confirmation error:", e)
        flash("Confirmation failed. Try signing in directly.", "error")
        return redirect(url_for("auth.login"))


# ------------------------------------------------ #
# ------------- EMAIL VERIFICATION ---------------- #
# ------------------------------------------------ #

@auth.route("/verify-email/<token>")
def verify_email(token):
    email = verify_verification_token(token)
    if not email:
        flash("That verification link is invalid or has expired.", "error")
        return redirect(url_for("auth.verify_email_prompt"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("auth.login"))

    if not user.email_verified:
        try:
            admin_supa = create_client(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            )
            if user.supabase_uid:
                admin_supa.auth.admin.update_user_by_id(
                    user.supabase_uid, {"email_confirm": True}
                )
        except Exception as e:
            print("Supabase email confirm error:", e)

        user.email_verified = True
        db.session.commit()

    login_user(user, remember=True)
    flash("Email verified! Welcome to Avants.", "success")

    if user.role == 'employer' and not Company.query.filter_by(owner_user_id=user.id).first():
        return redirect(url_for("auth.complete_employer_profile"))

    return redirect(url_for("views.home"))


# ------------------------------------------------ #
# ---------------- GOOGLE OAUTH ------------------ #
# ------------------------------------------------ #


@auth.route('/login/google')
def login_google():
    # Stash the intended role (only relevant for brand-new signups — see
    # login_google_finish) so an employer choosing "Continue with Google"
    # doesn't silently end up as a candidate.
    role = request.args.get("role", "candidate")
    session["oauth_role"] = role if role in ("candidate", "employer") else "candidate"

    # Redirect user to Supabase Google OAuth
    res = supabase.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {
            # This must match your route that serves the JS
            "redirect_to": request.host_url.rstrip("/") + "/login/google/finish"
        }
    })
    return redirect(res.url)


@auth.route("/login/google/finish")
def login_google_finish():
    code = request.args.get("code")

    if not code:
        flash("Google login failed.", "error")
        return redirect(url_for("auth.login"))

    try:
        # Exchange code for session
        res = supabase.auth.exchange_code_for_session({"auth_code": code})

        supa_user = res.user

        if not supa_user:
            flash("Google login failed.", "error")
            return redirect(url_for("auth.login"))

        email = supa_user.email
        name = supa_user.user_metadata.get("full_name", "Google User")

        local_user = User.query.filter_by(email=email).first()
        is_new_user = local_user is None

        if not local_user:
            role = session.pop("oauth_role", "candidate")
            email_domain = email.split('@')[-1].lower() if '@' in email else ''

            if role == 'employer' and email_domain in FREE_EMAIL_DOMAINS:
                role = 'candidate'
                flash("Signed up as a candidate — employer accounts need a company email, not a personal one.", "error")

            local_user = User(
                email=email,
                first_name=name,
                password=None,
                role=role,
                email_verified=True,
            )
            db.session.add(local_user)
            db.session.commit()

        login_user(local_user, remember=True)

        if is_new_user and local_user.role == 'employer':
            return redirect(url_for("auth.complete_employer_profile"))

        return redirect(url_for("views.home"))

    except Exception as e:
        print("Google OAuth error:", e)
        flash("Google login failed.", "error")
        return redirect(url_for("auth.login"))


@auth.route("/complete-employer-profile", methods=["GET", "POST"])
@login_required
def complete_employer_profile():
    """Google OAuth has no form step, so a brand-new employer signup lands
    here to give their company a name before they can post jobs."""
    if current_user.role != 'employer':
        return redirect(url_for("views.home"))

    existing_company = Company.query.filter_by(owner_user_id=current_user.id).first()
    if existing_company:
        return redirect(url_for("views.home"))

    if request.method == "POST":
        company_name = request.form.get("companyName", "").strip()
        if not company_name:
            flash("Company name is required.", "error")
            return render_template("complete_employer_profile.html")

        company = Company(owner_user_id=current_user.id, name=company_name)
        db.session.add(company)
        db.session.commit()

        flash("Company profile saved! You're all set.", "success")
        return redirect(url_for("views.home"))

    return render_template("complete_employer_profile.html")
