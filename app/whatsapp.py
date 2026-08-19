"""Shared WhatsApp helpers: number normalisation and signed share links."""

import hashlib
import hmac
import re
from urllib.parse import quote

from flask import request

from app import app


def whatsapp_digits(raw):
    """Return international digits for wa.me, or None if the number is unusable."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) >= 10:
        digits = "92" + digits[1:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    if 10 <= len(digits) <= 15:
        return digits
    return None


def whatsapp_url(raw_number, message=""):
    digits = whatsapp_digits(raw_number)
    if not digits:
        return None
    if message:
        return f"https://wa.me/{digits}?text={quote(str(message))}"
    return f"https://wa.me/{digits}"


# ---- signed share tokens (login-free download links) ----

def _secret():
    return str(app.config.get("SECRET_KEY") or "hardware-stock")


def share_token(kind, record_id):
    digest = hmac.new(
        _secret().encode(),
        f"{kind}:{int(record_id)}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def share_token_valid(kind, record_id, token):
    expected = share_token(kind, record_id)
    provided = str(token or "")
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def _absolute_root():
    root = request.url_root.rstrip("/")
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if forwarded == "https" and root.startswith("http://"):
        root = "https://" + root[len("http://"):]
    return root


def public_file_url(path):
    return _absolute_root() + ("/" if not path.startswith("/") else "") + path


INVOICE_PDF_KIND = "invoice-pdf"
QUOTATION_EXCEL_KIND = "quotation-excel"
