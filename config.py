# config.py
import os
from datetime import timedelta


def _desktop_mode_enabled() -> bool:
    flag = (os.environ.get("DESKTOP_MODE") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    url = (os.environ.get("DATABASE_URL") or "").strip().lower()
    return url.startswith("sqlite:")


def _default_database_url() -> str:
    if _desktop_mode_enabled():
        try:
            from desktop.paths import default_sqlite_url

            return default_sqlite_url()
        except Exception:
            return "sqlite:///euroglass_stock.db"
    return "postgresql://postgres:postgres@localhost:5432/project2_db"


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

    DESKTOP_MODE = _desktop_mode_enabled()

    # PostgreSQL (online) or sqlite:///... (offline Windows desktop)
    DATABASE_URL = os.environ.get("DATABASE_URL") or _default_database_url()

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


class DesktopConfig(DevelopmentConfig):
    """Offline Windows app: local SQLite, loose cookies, no HTTPS requirement."""

    DESKTOP_MODE = True
    SESSION_COOKIE_SECURE = False


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "desktop": DesktopConfig,
}