from datetime import datetime, timezone
from flask import Blueprint, render_template, request, flash, redirect, url_for, session, make_response
from .models import User, Company, CompanyMember, FREE_EMAIL_DOMAINS
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


def _safe_next(next_url):
    """Only allow same-site relative redirects for ?next= - rejects absolute/
    protocol-relative URLs so this can't be used as an open redirect."""
    if not next_url or not next_url.startswith('/') or next_url.startswith('//'):
        return None
    return next_url


def _provision_employer(user, email_domain, company_name, signature_name=None):
    """Get-or-join a Company for a brand-new employer user. Coworkers signing
    up from the same work email domain land in the same hub (as a
    CompanyMember) instead of each spinning up a separate company - only the
    first signup from a domain actually creates the Company, and only that
    founding signup signs the Employer Platform Agreement on the Company's
    behalf."""
    existing = None
    if email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        existing = Company.query.filter_by(email_domain=email_domain).first()

    if existing:
        if not CompanyMember.query.filter_by(company_id=existing.id, user_id=user.id).first():
            db.session.add(CompanyMember(company_id=existing.id, user_id=user.id))
            db.session.commit()
        return existing

    signature_name = (signature_name or '').strip()
    if not company_name or not signature_name:
        # No real e-signature collected yet (e.g. a Supabase session syncing
        # in via login()/auth_confirm() without ever hitting the sign-up
        # form's checkbox) - don't stamp acceptance on a Company that was
        # never actually signed for. employer_required's existing redirect to
        # complete_employer_profile is the only path that creates the Company
        # from here, and that route already collects the checkbox + signature.
        return None

    company = Company(
        owner_user_id=user.id,
        name=company_name,
        email_domain=email_domain or None,
        terms_accepted_at=datetime.now(timezone.utc),
        terms_accepted_ip=request.remote_addr,
        terms_version='1.0',
        terms_signature_name=signature_name,
    )
    db.session.add(company)
    db.session.commit()
    return company

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
    next_url = _safe_next(request.values.get('next'))

    if current_user.is_authenticated:
        return redirect(next_url or url_for('views.home'))

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
                if next_url:
                    resp.set_cookie("pending_next", next_url, max_age=900, httponly=True, samesite="Lax")
                return resp
            flash("Invalid email or password.", "error")
            return render_template("login.html", next=next_url)

        if not hasattr(res, 'user') or not res.user:
            flash("Invalid email or password.", "error")
            return render_template("login.html", next=next_url)

        if not res.user.email_confirmed_at:
            flash("Please verify your email before signing in. Check your inbox for a confirmation link.", "error")
            resp = make_response(redirect(url_for("auth.verify_email_prompt")))
            resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
            if next_url:
                resp.set_cookie("pending_next", next_url, max_age=900, httponly=True, samesite="Lax")
            return resp

        # Ensure local user exists for Flask-Login
        local_user = User.query.filter_by(email=email).first()
        if not local_user:
            meta = res.user.user_metadata or {}
            role = meta.get("role", "candidate")
            company_name = meta.get("company_name", "").strip()
            email_domain = email.split('@')[-1].lower() if '@' in email else ''

            if role == 'employer' and email_domain in FREE_EMAIL_DOMAINS:
                role = 'candidate'

            local_user = User(
                email=email,
                first_name=meta.get("full_name", ""),
                password=None,
                role=role,
                email_verified=True,
            )
            db.session.add(local_user)
            db.session.commit()

            if role == 'employer':
                _provision_employer(local_user, email_domain, company_name)

        login_user(local_user, remember=True)
        flash("Logged in successfully!", "success")
        return redirect(next_url or url_for('views.home'))

    return render_template("login.html", next=next_url)

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

# ---------------- SIGN-UP ----------------
@auth.route('/sign-up', methods=['GET', 'POST'])
@limiter.limit("10 per hour")
def sign_up():
    next_url = _safe_next(request.values.get('next'))

    if current_user.is_authenticated:
        return redirect(next_url or url_for('views.home'))

    if request.method == 'POST':
        email = request.form.get('email')
        first_name = request.form.get('firstName')
        password1 = request.form.get('password1')
        password2 = request.form.get('password2')
        role = request.form.get('role', 'candidate')
        company_name = request.form.get('companyName', '').strip()
        signature_name = request.form.get('signatureName', '').strip()
        employer_agreement = request.form.get('employer_agreement') == 'on'

        email_domain = email.split('@')[-1].lower() if email and '@' in email else ''

        existing_hub = None
        if role == 'employer' and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
            existing_hub = Company.query.filter_by(email_domain=email_domain).first()

        if not email:
            flash("Email is required.", "error")
        elif password1 != password2:
            flash("Passwords do not match.", "error")
        elif len(password1) < 7:
            flash("Password must be at least 7 characters.", "error")
        elif role == 'employer' and not company_name and not existing_hub:
            flash("Company name is required for employer accounts.", "error")
        elif role == 'employer' and email_domain in FREE_EMAIL_DOMAINS:
            flash("Please sign up with your company email address, not a personal email provider.", "error")
        elif role == 'employer' and not existing_hub and not signature_name:
            flash("Type your full legal name to sign the Employer Platform Agreement.", "error")
        elif role == 'employer' and not existing_hub and not employer_agreement:
            flash("You must agree to the Employer Platform Agreement to create an employer account.", "error")
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
                db.session.commit()

                if role == 'employer':
                    _provision_employer(local_user, email_domain, company_name, signature_name)

                try:
                    send_verification_email(local_user)
                except Exception as mail_err:
                    print("Verification email send failed:", mail_err)

                resp = make_response(redirect(url_for("auth.verify_email_prompt")))
                resp.set_cookie("pending_verify_email", email, max_age=900, httponly=True, samesite="Lax")
                if next_url:
                    resp.set_cookie("pending_next", next_url, max_age=900, httponly=True, samesite="Lax")
                return resp

            except Exception as e:
                err = str(e).lower()
                if "already registered" in err or "already been registered" in err or "duplicate" in err:
                    flash("Email already exists. Try logging in instead.", "error")
                    return redirect(url_for("auth.login", next=next_url) if next_url else url_for("auth.login"))

                print("Signup unexpected error:", e)
                flash("Something went wrong. Try again.", "error")
                return redirect(url_for("auth.sign_up", next=next_url) if next_url else url_for("auth.sign_up"))

    return render_template("sign_up.html", next=next_url)

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

            if role == 'employer':
                _provision_employer(local_user, email_domain, company_name)
        else:
            local_user.email_verified = True
            db.session.commit()

        login_user(local_user, remember=True)
        flash("Email confirmed! Welcome to Avants.", "success")

        has_company = (
            Company.query.filter_by(owner_user_id=local_user.id).first()
            or CompanyMember.query.filter_by(user_id=local_user.id).first()
        )
        if is_new and local_user.role == 'employer' and not has_company:
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

    next_url = _safe_next(request.cookies.get("pending_next"))

    has_company = (
        Company.query.filter_by(owner_user_id=user.id).first()
        or CompanyMember.query.filter_by(user_id=user.id).first()
    )
    if user.role == 'employer' and not has_company:
        target = url_for("auth.complete_employer_profile")
    else:
        target = next_url or url_for("views.home")

    resp = make_response(redirect(target))
    resp.delete_cookie("pending_next")
    return resp


# ------------------------------------------------ #
# ---------------- GOOGLE OAUTH ------------------ #
# ------------------------------------------------ #


@auth.route('/login/google')
def login_google():
    next_url = _safe_next(request.args.get("next"))

    # Employers sign up with their company email/password only - Google gives
    # no chance to collect a company name or verify a work domain up front,
    # so a fresh employer account can't be created through this route.
    if request.args.get("role") == "employer":
        flash("Employer accounts sign up with a work email and password - Google sign-up is for candidates only.", "error")
        return redirect(url_for("auth.sign_up", next=next_url) if next_url else url_for("auth.sign_up"))

    session["oauth_role"] = "candidate"
    session["oauth_next"] = next_url

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
                # Don't silently create the wrong kind of account - Google only
                # gave us a personal-domain email, but they asked for employer.
                # Let them explicitly choose instead of trapping them as a
                # candidate with no way back.
                session['oauth_pending_email'] = email
                session['oauth_pending_name'] = name
                return redirect(url_for('auth.employer_email_conflict'))

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
        next_url = _safe_next(session.pop("oauth_next", None))

        if is_new_user and local_user.role == 'employer':
            return redirect(url_for("auth.complete_employer_profile"))

        return redirect(next_url or url_for("views.home"))

    except Exception as e:
        print("Google OAuth error:", e)
        flash("Google login failed.", "error")
        return redirect(url_for("auth.login"))


@auth.route("/employer-email-conflict", methods=["GET", "POST"])
def employer_email_conflict():
    """Landing spot for a Google sign-up that asked for an employer account
    but only offered a personal-domain email - see login_google_finish."""
    email = session.get('oauth_pending_email')
    name = session.get('oauth_pending_name')
    if not email:
        return redirect(url_for('auth.sign_up'))

    if request.method == "POST":
        choice = request.form.get('choice')
        session.pop('oauth_pending_email', None)
        session.pop('oauth_pending_name', None)
        next_url = _safe_next(session.pop('oauth_next', None))

        if choice == 'candidate':
            local_user = User.query.filter_by(email=email).first()
            if not local_user:
                local_user = User(email=email, first_name=name, password=None, role='candidate', email_verified=True)
                db.session.add(local_user)
                db.session.commit()
            login_user(local_user, remember=True)
            flash("Signed up as a candidate account.", "success")
            return redirect(next_url or url_for('views.home'))

        return redirect(url_for('auth.sign_up', next=next_url) if next_url else url_for('auth.sign_up'))

    return render_template("employer_email_conflict.html", email=email)


@auth.route("/complete-employer-profile", methods=["GET", "POST"])
@login_required
def complete_employer_profile():
    """Google OAuth has no form step, so a brand-new employer signup lands
    here to give their company a name before they can post jobs."""
    if current_user.role != 'employer':
        return redirect(url_for("views.home"))

    if Company.query.filter_by(owner_user_id=current_user.id).first():
        return redirect(url_for("views.home"))

    if CompanyMember.query.filter_by(user_id=current_user.id).first():
        return redirect(url_for("views.home"))

    email_domain = current_user.email.split('@')[-1].lower() if current_user.email and '@' in current_user.email else ''
    existing_hub = None
    if email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        existing_hub = Company.query.filter_by(email_domain=email_domain).first()

    if existing_hub:
        db.session.add(CompanyMember(company_id=existing_hub.id, user_id=current_user.id))
        db.session.commit()
        flash(f"You've joined {existing_hub.name} - your teammates are already set up here.", "success")
        return redirect(url_for("views.home"))

    if request.method == "POST":
        company_name = request.form.get("companyName", "").strip()
        signature_name = request.form.get("signatureName", "").strip()
        agreed = request.form.get("agreement") == "on"

        if not company_name:
            flash("Company name is required.", "error")
            return render_template("complete_employer_profile.html")
        if not signature_name or not agreed:
            flash("Type your full legal name and agree to the Employer Platform Agreement to continue.", "error")
            return render_template("complete_employer_profile.html")

        company = Company(
            owner_user_id=current_user.id,
            name=company_name,
            email_domain=email_domain or None,
            terms_accepted_at=datetime.now(timezone.utc),
            terms_accepted_ip=request.remote_addr,
            terms_version='1.0',
            terms_signature_name=signature_name,
        )
        db.session.add(company)
        db.session.commit()

        flash("Company profile saved! You're all set.", "success")
        return redirect(url_for("views.home"))

    return render_template("complete_employer_profile.html")
