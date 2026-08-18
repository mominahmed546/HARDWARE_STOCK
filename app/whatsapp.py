"""Shared WhatsApp click-to-chat and file-share helpers."""

import hashlib
import hmac
import re
from urllib.parse import quote

from flask import request

from app import app

INVOICE_PDF_KIND = "invoice-pdf"
QUOTATION_EXCEL_KIND = "quotation-excel"


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


def share_token(kind, record_id):
    secret = str(app.config.get("SECRET_KEY") or "hardware-stock")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{kind}:{int(record_id)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def share_token_valid(kind, record_id, token):
    expected = share_token(kind, record_id)
    provided = str(token or "")
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def public_absolute_url(path):
    root = request.url_root.rstrip("/")
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if forwarded == "https" and root.startswith("http://"):
        root = "https://" + root[len("http://") :]
    if not path.startswith("/"):
        path = "/" + path
    return root + path
