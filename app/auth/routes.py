from hmac import compare_digest

from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_user, logout_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired



from app import app

from app.db import get_db_connection
from app.email_utils import send_password_reset_email, is_email_configured

from app.validators import (

    ValidationErrors,

    clean_username,

    clean_password,

    clean_email,

    clean_phone,

)


RESET_TOKEN_SALT = 'password-reset-salt'
RESET_TOKEN_MAX_AGE = 3600  # 1 hour


def _get_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'])


def generate_reset_token(user_id):
    return _get_serializer().dumps({'user_id': user_id}, salt=RESET_TOKEN_SALT)


def verify_reset_token(token, max_age=RESET_TOKEN_MAX_AGE):
    try:
        return _get_serializer().loads(token, salt=RESET_TOKEN_SALT, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None





auth_bp = Blueprint('auth', __name__, url_prefix='/auth')


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


def _user_matches_recovery(user_row, email, phone):
    provided_email = _normalize_email(email)
    provided_phone = _normalize_phone(phone)

    # Username-only recovery when email and phone are left blank.
    if not provided_email and not provided_phone:
        return True

    stored_email = _normalize_email(getattr(user_row, "Email", None))
    stored_phone = _normalize_phone(getattr(user_row, "Phone", None))

    if stored_email and provided_email != stored_email:
        return False
    if stored_phone and provided_phone != stored_phone:
        return False
    return True


def _recovery_field_errors(user_row, email, phone, errors):
    provided_email = _normalize_email(email)
    provided_phone = _normalize_phone(phone)
    if not provided_email and not provided_phone:
        return

    stored_email = _normalize_email(getattr(user_row, "Email", None))
    stored_phone = _normalize_phone(getattr(user_row, "Phone", None))
    if stored_email and not provided_email:
        errors.add("email", "Email is required for this account.")
    if stored_phone and not provided_phone:
        errors.add("phone", "Phone number is required for this account.")


def _deliver_password_reset(user_row, reset_url):
    stored_email = _normalize_email(getattr(user_row, "Email", None))
    username = getattr(user_row, "Username", None) or getattr(user_row, "UserName", "")

    email_sent = False
    if stored_email and is_email_configured(app):
        try:
            send_password_reset_email(app, stored_email, username, reset_url)
            email_sent = True
        except Exception:
            app.logger.exception("Failed to send password reset email")

    if email_sent:
        flash(
            f"A reset link was also emailed to {stored_email}. "
            f"Use this link now (valid for 1 hour): {reset_url}",
            "success",
        )
    else:
        flash(
            f"Use this link to reset your password (valid for 1 hour): {reset_url}",
            "warning",
        )
    return redirect(url_for("auth.login"))


@auth_bp.route('/login', methods=['GET', 'POST'])

def login():

    errors = ValidationErrors()

    form_data = {}



    if request.method == 'POST':

        form_data = request.form.to_dict()

        username = clean_username(request.form.get('username'), errors)

        password = clean_password(request.form.get('password'), errors)



        if not errors.valid:

            flash(errors.first(), 'danger')

            return render_template('auth/login.html', errors=errors.errors, form_data=form_data)



        try:

            db = get_db_connection(app)

            cursor = db.cursor()



            cursor.execute(

                "SELECT UserID, Username, Password FROM Users WHERE Username = ?",

                (username,)

            )



            result = cursor.fetchone()

            if result:

                password_valid, needs_rehash = _verify_password(result[2], password)

                if password_valid:

                    if needs_rehash:

                        cursor.execute(

                            "UPDATE Users SET Password = ? WHERE UserID = ?",

                            (generate_password_hash(password), result[0])

                        )

                        db.commit()

                    cursor.close()

                    user = app.User(result[0], result[1])

                    login_user(user)

                    flash('Logged in successfully', 'success')

                    return redirect(url_for('dashboard.dashboard'))

            cursor.close()



            flash('Invalid username or password', 'danger')



        except Exception as e:

            flash(f'Login error: {str(e)}', 'danger')



    return render_template('auth/login.html', errors=errors.errors, form_data=form_data)





@auth_bp.route('/register', methods=['GET', 'POST'])

def register():

    errors = ValidationErrors()

    form_data = {}



    if request.method == 'POST':

        form_data = request.form.to_dict()

        username = clean_username(request.form.get('username'), errors)

        email = clean_email(request.form.get('email'), 'email', errors, required=False)

        phone = clean_phone(request.form.get('phone'), 'phone', errors, required=False)

        password = clean_password(request.form.get('password'), errors)



        if not errors.valid:

            flash(errors.first(), 'danger')

            return render_template('auth/register.html', errors=errors.errors, form_data=form_data)



        try:

            db = get_db_connection(app)

            cursor = db.cursor()



            cursor.execute("SELECT UserID FROM Users WHERE Username = ?", (username,))

            if cursor.fetchone():

                cursor.close()

                errors.add('username', 'Username is already taken.')

                flash(errors.first(), 'danger')

                return render_template('auth/register.html', errors=errors.errors, form_data=form_data)



            if email:
                cursor.execute("SELECT UserID FROM Users WHERE Email = ?", (email,))

                if cursor.fetchone():

                    cursor.close()

                    errors.add('email', 'Email is already registered.')

                    flash(errors.first(), 'danger')

                    return render_template('auth/register.html', errors=errors.errors, form_data=form_data)



            cursor.execute(

                "INSERT INTO Users (UserName, Email, Phone, Password) VALUES (?, ?, ?, ?)",

                (username, email, phone, generate_password_hash(password))

            )



            db.commit()

            cursor.close()



            flash('Registration successful. Please log in.', 'success')

            return redirect(url_for('auth.login'))



        except Exception as e:

            flash(f'Registration error: {str(e)}', 'danger')



    return render_template('auth/register.html', errors=errors.errors, form_data=form_data)





@auth_bp.route('/logout', methods=['POST'])

@login_required

def logout():

    logout_user()

    flash('Logged out successfully', 'success')

    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():

    errors = ValidationErrors()
    form_data = {}

    if request.method == 'POST':

        form_data = request.form.to_dict()

        username = clean_username(request.form.get('username'), errors)
        email = clean_email(request.form.get('email'), 'email', errors, required=False)
        phone = clean_phone(request.form.get('phone'), 'phone', errors, required=False)

        if errors.valid:
            db = get_db_connection(app)
            cursor = db.cursor()
            cursor.execute(
                "SELECT UserID, Username, Email, Phone FROM Users WHERE Username = ?",
                (username,),
            )
            user_row = cursor.fetchone()
            cursor.close()
            if user_row:
                _recovery_field_errors(user_row, email, phone, errors)

        if not errors.valid:
            flash(errors.first(), 'danger')
            return render_template('auth/forgot_password.html', errors=errors.errors, form_data=form_data)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()

            cursor.execute(
                "SELECT UserID, Username, Email, Phone FROM Users WHERE Username = ?",
                (username,),
            )
            result = cursor.fetchone()
            cursor.close()

            if result and _user_matches_recovery(result, email, phone):
                token = generate_reset_token(result.UserID)
                reset_url = url_for('auth.reset_password', token=token, _external=True)
                return _deliver_password_reset(result, reset_url)

            # Same message whether or not a match was found, to avoid leaking
            # which usernames/emails/phones are registered.
            flash(
                'No matching account found. Check your username, or leave email and phone '
                'blank if your account has no contact details on file.',
                'danger',
            )
            return redirect(url_for('auth.login'))

        except Exception as e:
            flash(f'Unable to process request: {str(e)}', 'danger')

    return render_template('auth/forgot_password.html', errors=errors.errors, form_data=form_data)


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):

    data = verify_reset_token(token)

    if data is None:
        flash('This password reset link is invalid or has expired. Please request a new one.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    errors = ValidationErrors()

    if request.method == 'POST':

        password = clean_password(request.form.get('password'), errors)
        confirm_password = request.form.get('confirm_password') or ''

        if password and confirm_password != password:
            errors.add('confirm_password', 'Passwords do not match.')

        if not errors.valid:
            flash(errors.first(), 'danger')
            return render_template('auth/reset_password.html', errors=errors.errors, token=token)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()

            cursor.execute(
                "UPDATE Users SET Password = ? WHERE UserID = ?",
                (generate_password_hash(password), data['user_id'])
            )
            db.commit()
            cursor.close()

            flash('Your password has been reset. Please log in.', 'success')
            return redirect(url_for('auth.login'))

        except Exception as e:
            flash(f'Unable to reset password: {str(e)}', 'danger')

    return render_template('auth/reset_password.html', errors=errors.errors, token=token)

