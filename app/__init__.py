"""
App initialization
"""

import os
from datetime import datetime

from flask import Flask, redirect, url_for
from flask_login import LoginManager
from config import config_map
from app.db import get_db_connection, close_db_connection

_base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
app = Flask(
    __name__,
    template_folder=os.path.join(_base_dir, "templates"),
    static_folder=os.path.join(_base_dir, "static"),
)
app.config.from_object(config_map["development"])

app.teardown_appcontext(close_db_connection)


@app.template_filter("date_dmy")
def date_dmy(value):
    if not value:
        return ""

    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")

    value = str(value).strip()
    for date_format in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:19], date_format).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return value

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."


class User:
    def __init__(self, id, username, role="user"):
        self.id = id
        self.username = username
        self.role = role
        self.is_authenticated = True
        self.is_active = True
        self.is_anonymous = False

    def get_id(self):
        return str(self.id)


app.User = User


@login_manager.user_loader
def load_user(user_id):
    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        cursor.execute(
            "SELECT UserID, Username FROM Users WHERE UserID = ?",
            (int(user_id),),
        )
        row = cursor.fetchone()
        cursor.close()

        if row:
            return User(row[0], row[1])

    except Exception as e:
        print("User load error:", e)

    return None


from app.auth.routes import auth_bp
from app.dashboard.routes import dashboard_bp
from app.categories.routes import categories_bp
from app.suppliers.routes import suppliers_bp
from app.items.routes import items_bp
from app.customers.routes import customers_bp
from app.purchases.routes import purchases_bp
from app.invoices.routes import invoices_bp
from app.reports.routes import reports_bp
from app.ledger.routes import ledger_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(categories_bp)
app.register_blueprint(suppliers_bp)
app.register_blueprint(items_bp)
app.register_blueprint(customers_bp)
app.register_blueprint(purchases_bp)
app.register_blueprint(invoices_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(ledger_bp)


@app.route("/")
def index():
    return redirect(url_for("auth.login"))
