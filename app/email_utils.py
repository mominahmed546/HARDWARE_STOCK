"""
Minimal SMTP email helper used for password-reset notifications.

Configuration is read from the Flask app config (populated from environment
variables in config.py): MAIL_SERVER, MAIL_PORT, MAIL_USE_TLS, MAIL_USERNAME,
MAIL_PASSWORD, MAIL_DEFAULT_SENDER.
"""

import smtplib
from email.mime.text import MIMEText


def _smtp_settings(app):
    username = app.config.get("MAIL_USERNAME")
    return {
        "host": app.config.get("MAIL_SERVER"),
        "port": app.config.get("MAIL_PORT", 587),
        "use_tls": app.config.get("MAIL_USE_TLS", True),
        "username": username,
        "password": app.config.get("MAIL_PASSWORD"),
        "sender": app.config.get("MAIL_DEFAULT_SENDER") or username,
    }


def is_email_configured(app):
    settings = _smtp_settings(app)
    return bool(settings["host"] and settings["username"] and settings["password"])


def send_email(app, to_address, subject, body):
    settings = _smtp_settings(app)

    if not is_email_configured(app):
        raise RuntimeError(
            "Email is not configured. Set MAIL_SERVER, MAIL_USERNAME and "
            "MAIL_PASSWORD environment variables."
        )

    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = to_address

    with smtplib.SMTP(settings["host"], settings["port"], timeout=10) as server:
        if settings["use_tls"]:
            server.starttls()
        server.login(settings["username"], settings["password"])
        server.sendmail(settings["sender"], [to_address], message.as_string())


def send_password_reset_email(app, to_address, username, reset_url):
    subject = "Reset your Hardware Stock Management password"
    body = (
        f"Hello {username},\n\n"
        "We received a request to reset the password for your Hardware Stock "
        "Management account.\n\n"
        f"Click the link below to choose a new password (valid for 1 hour):\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can safely ignore this email.\n"
    )
    send_email(app, to_address, subject, body)


def send_password_reset_whatsapp(to_phone, username, reset_url):
    """Send password reset link via WhatsApp. Returns (ok, error_message)."""
    from app.wa_api import is_configured, send_text

    if not is_configured():
        return False, "WhatsApp is not configured on this server."

    body = (
        f"Hello {username},\n\n"
        "Password reset for your Euroglass Hardware Stock account.\n\n"
        f"Open this link within 1 hour to set a new password:\n{reset_url}\n\n"
        "If you did not request this, ignore this message."
    )
    _result, err = send_text(to_phone, body)
    if err:
        return False, err
    return True, None
