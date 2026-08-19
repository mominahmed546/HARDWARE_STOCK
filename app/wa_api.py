"""
WhatsApp Business Cloud API sender.

Requires three env vars / Render secrets:
  WA_TOKEN       — permanent system-user access token from Meta Business
  WA_PHONE_ID    — Phone Number ID (not the phone number itself) from Meta
  WA_VERIFY_TOKEN — any string you choose, used to verify the webhook

To get these:
  1. https://developers.facebook.com/apps  →  Create App  →  Business
  2. Add "WhatsApp" product.  Under WhatsApp > API Setup you'll find
     the Phone Number ID and a temporary token (make a permanent one
     via System User in Business Settings).
  3. Set WA_TOKEN and WA_PHONE_ID as Render secrets.
"""

import os
import urllib.request
import urllib.error
import json


def _token():
    return os.environ.get("WA_TOKEN", "")


def _phone_id():
    return os.environ.get("WA_PHONE_ID", "")


def _normalise(raw):
    """Return E.164 digits (no +) suitable for the API."""
    import re
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) >= 10:
        digits = "92" + digits[1:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    return digits if 10 <= len(digits) <= 15 else None


def _api(method, path, body=None, content_type="application/json"):
    url = f"https://graph.facebook.com/v20.0{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    if data:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        return None, f"HTTP {e.code}: {msg}"
    except Exception as exc:
        return None, str(exc)


def is_configured():
    return bool(_token() and _phone_id())


def upload_media(file_bytes, mime_type, file_name):
    """Upload bytes to the WhatsApp media endpoint. Returns (media_id, error)."""
    boundary = "----WAboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    url = f"https://graph.facebook.com/v20.0/{_phone_id()}/media"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result.get("id"), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode(errors='replace')}"
    except Exception as exc:
        return None, str(exc)


def send_document(to_raw, media_id, file_name, caption=""):
    """Send an already-uploaded document to a WhatsApp number."""
    to = _normalise(to_raw)
    if not to:
        return None, "Invalid phone number."
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": file_name,
            "caption": caption,
        },
    }
    result, err = _api("POST", f"/{_phone_id()}/messages", body)
    if err:
        return None, err
    return result, None


def send_file_bytes(to_raw, file_bytes, mime_type, file_name, caption=""):
    """Upload file_bytes then send as a document. Returns (result, error)."""
    if not is_configured():
        return None, "WA_TOKEN and WA_PHONE_ID are not set. Add them in Render → Environment."
    media_id, err = upload_media(file_bytes, mime_type, file_name)
    if err:
        return None, f"Upload failed: {err}"
    return send_document(to_raw, media_id, file_name, caption)
