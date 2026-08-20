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


def generate_reset_token(user_id, email):
    return _get_serializer().dumps(
        {'user_id': user_id, 'email': email}, salt=RESET_TOKEN_SALT
    )


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

        email = clean_email(request.form.get('email'), errors)

        phone = clean_phone(request.form.get('phone'), 'phone', errors, required=True)

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
        email = clean_email(request.form.get('email'), errors)
        phone = clean_phone(request.form.get('phone'), 'phone', errors, required=True)

        if not errors.valid:
            flash(errors.first(), 'danger')
            return render_template('auth/forgot_password.html', errors=errors.errors, form_data=form_data)

        try:
            db = get_db_connection(app)
            cursor = db.cursor()

            cursor.execute(
                "SELECT UserID, Username, Email FROM Users WHERE Username = ? AND Email = ? AND Phone = ?",
                (username, email, phone)
            )
            result = cursor.fetchone()
            cursor.close()

            if result:
                token = generate_reset_token(result[0], result[2])
                reset_url = url_for('auth.reset_password', token=token, _external=True)

                if is_email_configured(app):
                    send_password_reset_email(app, result[2], result[1], reset_url)
                else:
                    # No SMTP configured (e.g. local development) - surface the
                    # link directly instead of silently failing.
                    flash(
                        f'Email is not configured on this server. Use this link to reset your password: {reset_url}',
                        'warning'
                    )
                    return redirect(url_for('auth.login'))

            # Same message whether or not a match was found, to avoid leaking
            # which usernames/emails/phones are registered.
            flash(
                'If the details you entered match our records, a password reset '
                'link has been sent to the registered email address.',
                'success'
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
                "UPDATE Users SET Password = ? WHERE UserID = ? AND Email = ?",
                (generate_password_hash(password), data['user_id'], data['email'])
            )
            db.commit()
            cursor.close()

            flash('Your password has been reset. Please log in.', 'success')
            return redirect(url_for('auth.login'))

        except Exception as e:
            flash(f'Unable to reset password: {str(e)}', 'danger')

    return render_template('auth/reset_password.html', errors=errors.errors, token=token)

