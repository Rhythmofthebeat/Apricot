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
    # Supabase redirects here from email
    return """
<script>
const hash = window.location.hash.substring(1);
const params = new URLSearchParams(hash);

const access_token = params.get("access_token");
const refresh_token = params.get("refresh_token");

if (!access_token || !refresh_token) {
  alert("Invalid or expired reset link.");
  window.location = "/login";
} else {
  fetch("/reset-password/store", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ access_token, refresh_token })
  }).then(() => {
    window.location = "/reset-password/form";
  });
}
</script>

"""


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
                # Attempt signup
                res = supabase.auth.sign_up({
                    "email": email,
                    "password": password1,
                    "options": {"data": {"full_name": first_name}}
                })

                if res.user:
                    # Signup successful
                    flash("Account created successfully!", "success")
                    supa_user = res.user
                else:
                    # If no user returned, fallback to generic error
                    flash("Something went wrong. Try again.", "error")
                    return redirect(url_for("auth.sign_up"))

                # Ensure local Flask-Login user exists
                local_user = User.query.filter_by(email=email).first()
                if not local_user:
                    local_user = User(
                        email=email,
                        first_name=supa_user.user_metadata.get("full_name", first_name),
                        password=None,
                        role=role,
                        email_verified=(role != 'employer'),
                    )
                    db.session.add(local_user)
                    db.session.commit()

                    if role == 'employer':
                        company = Company(owner_user_id=local_user.id, name=company_name)
                        db.session.add(company)
                        db.session.commit()

                        try:
                            send_verification_email(local_user)
                            flash("Account created! Check your company email to verify your account before posting jobs.", "success")
                        except Exception as e:
                            print("Verification email send failed:", e)
                            flash(
                                "Account created, but the verification email couldn't be sent right now. "
                                "You'll need to verify your email before posting jobs — try resending it from your dashboard.",
                                "error",
                            )

                # Log in the user
                login_user(local_user, remember=True)
                return redirect(url_for('views.home'))

            except Exception as e:
                # Detect "already registered" from Supabase
                if "already registered" in str(e):
                    flash("Email already exists. Try logging in instead.", "error")
                    return redirect(url_for("auth.login"))

                # Other unexpected errors
                print("Signup/login unexpected error:", e)
                flash("Something went wrong. Try again.", "error")
                return redirect(url_for("auth.sign_up"))

    return render_template("sign_up.html")

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
