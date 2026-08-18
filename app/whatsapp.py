"""Shared WhatsApp click-to-chat helpers."""

import re
from urllib.parse import quote


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
