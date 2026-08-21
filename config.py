# config.py
import os
from datetime import timedelta


class Config:
    """Base configuration"""

    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-key-change-this"

    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Lets browsers reuse CSS/JS/images across page navigations instead of
    # re-requesting them on every single page load (short enough that a
    # deploy's updated assets show up within an hour, not stuck forever).
    SEND_FILE_MAX_AGE_DEFAULT = timedelta(hours=1)

    # PostgreSQL
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/project2_db"
    )

    # Outgoing email (used for password-reset links). Leave unset to disable
    # sending and instead show the reset link directly (useful for local dev).
    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER") or MAIL_USERNAME


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


class TestingConfig(Config):
    DEBUG = True
    TESTING = True


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig
}