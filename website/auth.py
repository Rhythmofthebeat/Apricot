from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from .models import User, Company, FREE_EMAIL_DOMAINS
from werkzeug.security import generate_password_hash, check_password_hash
from . import db
from flask_login import login_user, login_required, logout_user, current_user
from supabase import create_client
import os

from email_utils import send_verification_email, verify_verification_token

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
            {"redirect_to": request.host_url.rstrip("/") + "/reset-password"}
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
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        try:
            res = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
        except Exception as e:
            print("Supabase login error:", e)
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        if not hasattr(res, 'user') or not res.user:
            flash("Invalid email or password.", "error")
            return render_template("login.html")

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
                confirm_url = request.host_url.rstrip("/") + "/auth/confirm"
                res = supabase.auth.sign_up({
                    "email": email,
                    "password": password1,
                    "options": {
                        "data": {"full_name": first_name, "role": role, "company_name": company_name},
                        "email_redirect_to": confirm_url,
                    }
                })

                if not res.user:
                    flash("Something went wrong. Try again.", "error")
                    return redirect(url_for("auth.sign_up"))

                flash("Check your email and click the confirmation link to finish creating your account.", "success")
                return redirect(url_for("auth.login"))

            except Exception as e:
                if "already registered" in str(e):
                    flash("Email already exists. Try logging in instead.", "error")
                    return redirect(url_for("auth.login"))

                print("Signup/login unexpected error:", e)
                flash("Something went wrong. Try again.", "error")
                return redirect(url_for("auth.sign_up"))

    return render_template("sign_up.html")

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
                company = Company(owner_user_id=local_user.id, name=company_name)
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
        flash("That verification link is invalid or has expired. Request a new one from your dashboard.", "error")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found.", "error")
        return redirect(url_for("auth.login"))

    user.email_verified = True
    db.session.commit()
    flash("Email verified! You can now post jobs.", "success")
    return redirect(url_for("views.home"))


@auth.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    if current_user.email_verified:
        flash("Your email is already verified.", "success")
        return redirect(url_for("views.home"))

    try:
        send_verification_email(current_user)
        flash("Verification email sent — check your inbox.", "success")
    except Exception as e:
        print("Resend verification failed:", e)
        flash("Couldn't send the verification email right now. Try again shortly.", "error")

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
                email_verified=(role != 'employer'),
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

        try:
            send_verification_email(current_user)
            flash("Almost there — check your company email to verify your account before posting jobs.", "success")
        except Exception as e:
            print("Verification email send failed:", e)
            flash(
                "Company saved, but the verification email couldn't be sent right now. "
                "You'll need to verify your email before posting jobs — try resending it from your dashboard.",
                "error",
            )

        return redirect(url_for("views.home"))

    return render_template("complete_employer_profile.html")
