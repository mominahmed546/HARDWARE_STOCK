from hmac import compare_digest
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app import app
from app.db import get_db_connection
from app.email_utils import (
    is_email_configured,
    send_password_reset_email,
    send_password_reset_whatsapp,
)
from app.wa_api import is_configured as is_whatsapp_configured
from app.validators import (
    ValidationErrors,
    clean_username,
    clean_password,
    clean_email,
    clean_phone,
)

RESET_TOKEN_SALT = "password-reset-salt"
RESET_TOKEN_MAX_AGE = 3600  # 1 hour


def _get_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"])


def generate_reset_token(user_id):
    return _get_serializer().dumps({"user_id": user_id}, salt=RESET_TOKEN_SALT)


def verify_reset_token(token, max_age=RESET_TOKEN_MAX_AGE):
    try:
        return _get_serializer().loads(token, salt=RESET_TOKEN_SALT, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _verify_password(stored_password, password):
    if not stored_password:
        return False, False

    stored_password = str(stored_password)

    try:
        if check_password_hash(stored_password, password):
            return True, False
    except ValueError:
        pass

    # Existing databases may contain plain-text passwords. Allow one login,
    # then replace the stored value with a hash.
    if compare_digest(stored_password, password):
        return True, True

    return False, False


def _normalize_email(value):
    return (value or "").strip().lower()


def _normalize_phone(value):
    return (value or "").strip()


def _phones_equal(left, right):
    def digits(value):
        return re.sub(r"\D", "", value or "")

    a = digits(left)
    b = digits(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith("0") and ("92" + a[1:]) == b:
        return True
    if b.startswith("0") and ("92" + b[1:]) == a:
        return True
    return a[-10:] == b[-10:] and len(a) >= 10 and len(b) >= 10


def _user_display_name(user_row):
    return getattr(user_row, "Username", None) or getattr(user_row, "UserName", "") or ""


def _validate_recovery_input(user_row, email, phone, errors):
    provided_email = _normalize_email(email)
    provided_phone = _normalize_phone(phone)

    if not provided_email and not provided_phone:
        errors.add("email", "Enter the email or phone number registered on your account.")
        return

    if user_row is None:
        return

    stored_email = _normalize_email(getattr(user_row, "Email", None))
    stored_phone = _normalize_phone(getattr(user_row, "Phone", None))

    # Legacy account with no contact — accept provided email/phone to attach + send.
    if not stored_email and not stored_phone:
        return

    if provided_email and stored_email and provided_email != stored_email:
        errors.add("email", "Email does not match this account.")
    if provided_phone and stored_phone and not _phones_equal(provided_phone, stored_phone):
        errors.add("phone", "Phone number does not match this account.")

    email_ok = provided_email and stored_email and provided_email == stored_email
    phone_ok = provided_phone and stored_phone and _phones_equal(provided_phone, stored_phone)
    if not email_ok and not phone_ok:
        if stored_email and stored_phone:
            errors.add("email", "Enter your registered email or phone number.")
        elif stored_email:
            errors.add("email", "Enter your registered email address.")
        elif stored_phone:
            errors.add("phone", "Enter your registered phone number.")


def _attach_missing_contact(cursor, user_row, email, phone):
    """For legacy accounts with no contact, save the provided email/phone."""
    stored_email = _normalize_email(getattr(user_row, "Email", None))
    stored_phone = _normalize_phone(getattr(user_row, "Phone", None))
    if stored_email or stored_phone:
        return user_row

    new_email = _normalize_email(email) or None
    new_phone = _normalize_phone(phone) or None
    cursor.execute(
        "UPDATE Users SET Email = COALESCE(?, Email), Phone = COALESCE(?, Phone) WHERE UserID = ?",
        (new_email, new_phone, user_row.UserID),
    )

    class _User:
        pass

    refreshed = _User()
    refreshed.UserID = user_row.UserID
    refreshed.Username = _user_display_name(user_row)
    refreshed.Email = new_email
    refreshed.Phone = new_phone
    return refreshed


def _deliver_password_reset(user_row, reset_url, prefer_email=None, prefer_phone=None):
    username = _user_display_name(user_row)
    stored_email = _normalize_email(getattr(user_row, "Email", None))
    stored_phone = _normalize_phone(getattr(user_row, "Phone", None))
    target_email = _normalize_email(prefer_email) or stored_email
    target_phone = _normalize_phone(prefer_phone) or stored_phone

    provided_email = _normalize_email(prefer_email)
    provided_phone = _normalize_phone(prefer_phone)

    if provided_email and not provided_phone:
        try_email, try_phone = True, False
    elif provided_phone and not provided_email:
        try_email, try_phone = False, True
    else:
        try_email, try_phone = bool(target_email), bool(target_phone)

    sent_via = []
    failures = []

    if try_email and target_email:
        if not is_email_configured(app):
            failures.append("Email is not configured on this server.")
        else:
            try:
                send_password_reset_email(app, target_email, username, reset_url)
                sent_via.append("email")
            except Exception as exc:
                app.logger.exception("Failed to send password reset email")
                failures.append(f"Email failed: {exc}")

    if try_phone and target_phone and not sent_via:
        if not is_whatsapp_configured():
            failures.append("WhatsApp/phone delivery is not configured on this server.")
        else:
            ok, err = send_password_reset_whatsapp(target_phone, username, reset_url)
            if ok:
                sent_via.append("WhatsApp")
            else:
                failures.append(f"WhatsApp failed: {err}")

    # If preferred channel failed, try the other channel when available.
    if not sent_via and try_email and target_phone and is_whatsapp_configured():
        ok, err = send_password_reset_whatsapp(target_phone, username, reset_url)
        if ok:
            sent_via.append("WhatsApp")
        elif err:
            failures.append(f"WhatsApp failed: {err}")

    if not sent_via and try_phone and target_email and is_email_configured(app):
        try:
            send_password_reset_email(app, target_email, username, reset_url)
            sent_via.append("email")
        except Exception as exc:
            app.logger.exception("Failed to send password reset email")
            failures.append(f"Email failed: {exc}")

    if sent_via:
        channels = " and ".join(sent_via)
        flash(
            f"A password reset link has been sent to your {channels}. "
            "Check your inbox or WhatsApp and open the link within 1 hour.",
            "success",
        )
        return redirect(url_for("auth.login"))

    detail = " ".join(failures) if failures else "No email or phone delivery method is available."
    flash(f"Could not send the reset link. {detail}", "danger")
    return redirect(url_for("auth.forgot_password"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    errors = ValidationErrors()
    form_data = {}

    if request.method == "POST":
        form_data = request.form.to_dict()
        username = clean_username(request.form.get("username"), errors)
        password = clean_password(request.form.get("password"), errors)

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template("auth/login.html", errors=errors.errors, form_data=form_data)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()
            cursor.execute(
                "SELECT UserID, Username, Password FROM Users WHERE Username = ?",
                (username,),
            )
            result = cursor.fetchone()
            if result:
                password_valid, needs_rehash = _verify_password(result[2], password)
                if password_valid:
                    if needs_rehash:
                        cursor.execute(
                            "UPDATE Users SET Password = ? WHERE UserID = ?",
                            (generate_password_hash(password), result[0]),
                        )
                        db.commit()
                    cursor.close()
                    user = app.User(result[0], result[1])
                    login_user(user)
                    flash("Logged in successfully", "success")
                    return redirect(url_for("dashboard.dashboard"))
            cursor.close()
            flash("Invalid username or password", "danger")
        except Exception as e:
            flash(f"Login error: {str(e)}", "danger")

    return render_template("auth/login.html", errors=errors.errors, form_data=form_data)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    errors = ValidationErrors()
    form_data = {}

    if request.method == "POST":
        form_data = request.form.to_dict()
        username = clean_username(request.form.get("username"), errors)
        email = clean_email(request.form.get("email"), "email", errors, required=False)
        phone = clean_phone(request.form.get("phone"), "phone", errors, required=False)
        password = clean_password(request.form.get("password"), errors)

        if errors.valid and not email and not phone:
            errors.add("email", "Enter an email or phone number for password recovery.")

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template("auth/register.html", errors=errors.errors, form_data=form_data)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()

            cursor.execute("SELECT UserID FROM Users WHERE Username = ?", (username,))
            if cursor.fetchone():
                cursor.close()
                errors.add("username", "Username is already taken.")
                flash(errors.first(), "danger")
                return render_template("auth/register.html", errors=errors.errors, form_data=form_data)

            if email:
                cursor.execute("SELECT UserID FROM Users WHERE Email = ?", (email,))
                if cursor.fetchone():
                    cursor.close()
                    errors.add("email", "Email is already registered.")
                    flash(errors.first(), "danger")
                    return render_template("auth/register.html", errors=errors.errors, form_data=form_data)

            cursor.execute(
                "INSERT INTO Users (UserName, Email, Phone, Password) VALUES (?, ?, ?, ?)",
                (username, email, phone, generate_password_hash(password)),
            )
            db.commit()
            cursor.close()
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            flash(f"Registration error: {str(e)}", "danger")

    return render_template("auth/register.html", errors=errors.errors, form_data=form_data)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Logged out successfully", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    errors = ValidationErrors()
    form_data = {}

    if request.method == "POST":
        form_data = request.form.to_dict()
        username = clean_username(request.form.get("username"), errors)
        email = clean_email(request.form.get("email"), "email", errors, required=False)
        phone = clean_phone(request.form.get("phone"), "phone", errors, required=False)

        user_row = None
        if errors.valid and username:
            db = get_db_connection(app)
            cursor = db.cursor()
            cursor.execute(
                "SELECT UserID, Username, Email, Phone FROM Users WHERE Username = ?",
                (username,),
            )
            user_row = cursor.fetchone()
            cursor.close()
            _validate_recovery_input(user_row, email, phone, errors)

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template("auth/forgot_password.html", errors=errors.errors, form_data=form_data)

        try:
            if user_row is None:
                flash(
                    "If the details you entered match our records, a password reset "
                    "link has been sent to your email or WhatsApp.",
                    "success",
                )
                return redirect(url_for("auth.login"))

            db = get_db_connection(app)
            cursor = db.cursor()
            user_row = _attach_missing_contact(cursor, user_row, email, phone)
            db.commit()
            cursor.close()

            token = generate_reset_token(user_row.UserID)
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            return _deliver_password_reset(
                user_row,
                reset_url,
                prefer_email=email,
                prefer_phone=phone,
            )
        except Exception as e:
            flash(f"Unable to process request: {str(e)}", "danger")

    return render_template("auth/forgot_password.html", errors=errors.errors, form_data=form_data)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    data = verify_reset_token(token)

    if data is None:
        flash("This password reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("auth.forgot_password"))

    errors = ValidationErrors()

    if request.method == "POST":
        password = clean_password(request.form.get("password"), errors)
        confirm_password = request.form.get("confirm_password") or ""

        if password and confirm_password != password:
            errors.add("confirm_password", "Passwords do not match.")

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template("auth/reset_password.html", errors=errors.errors, token=token)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()
            cursor.execute(
                "UPDATE Users SET Password = ? WHERE UserID = ?",
                (generate_password_hash(password), data["user_id"]),
            )
            db.commit()
            cursor.close()
            flash("Your password has been reset. Please log in.", "success")
            return redirect(url_for("auth.login"))
        except Exception as e:
            flash(f"Unable to reset password: {str(e)}", "danger")

    return render_template("auth/reset_password.html", errors=errors.errors, token=token)
