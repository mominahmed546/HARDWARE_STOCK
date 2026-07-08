from hmac import compare_digest

from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_user, logout_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash



from app import app

from app.db import get_db_connection

from app.validators import (

    ValidationErrors,

    clean_username,

    clean_password,

)





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



            cursor.execute(

                "INSERT INTO Users (UserName, Password) VALUES (?, ?)",

                (username, generate_password_hash(password))

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

