"""Build a WhatsApp invoice message that includes the generated PDF."""

import hmac
import hashlib
from datetime import datetime

from app import app

MAX_MESSAGE_CHARS = 1800


def invoice_pdf_token(invoice_id):
    secret = str(app.config.get("SECRET_KEY") or "hardware-stock")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"invoice-pdf:{int(invoice_id)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def invoice_pdf_token_valid(invoice_id, token):
    expected = invoice_pdf_token(invoice_id)
    provided = str(token or "")
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def _format_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    value = str(value).strip()
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:19], date_format).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return value


def _qty(value):
    number = float(value or 0)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _money(value):
    return f"{float(value or 0):,.2f}"


def build_invoice_message(invoice, details, pdf_url):
    invoice_id = getattr(invoice, "InvoiceID", "") or ""
    total_amount = float(getattr(invoice, "TotalAmount", 0) or 0)
    previous_balance = float(getattr(invoice, "PreviousBalance", 0) or 0)
    cash_received = float(
        getattr(invoice, "CashReceived", None)
        if getattr(invoice, "CashReceived", None) is not None
        else getattr(invoice, "PaidAmount", 0) or 0
    )
    net_balance = float(getattr(invoice, "NetBalance", 0) or 0)
    invoice_due = max(total_amount - cash_received, 0)
    used_details = list(details or [])

    parts = [
        "*EUROGLASS HARDWARE*",
        f"*INVOICE {invoice_id}*",
        f"Date: {_format_date(getattr(invoice, 'InvoiceDate', None))}",
        f"Customer: {getattr(invoice, 'CustomerName', None) or 'N/A'}",
    ]
    contact_no = str(getattr(invoice, "ContactNo", "") or "").strip()
    if contact_no:
        parts.append(f"Contact: {contact_no}")

    parts.append("")
    parts.append("*Items*")
    if not used_details:
        parts.append("No items")
    else:
        for index, detail in enumerate(used_details, start=1):
            name = str(getattr(detail, "Particulars", None) or "Item")
            qty = getattr(detail, "Qty", 0)
            rate = getattr(detail, "Rate", 0)
            amount = getattr(detail, "TotalAmount", None)
            if amount is None:
                amount = float(qty or 0) * float(rate or 0)
            parts.append(f"{index}. {name}")
            parts.append(f"   Qty {_qty(qty)}  Rate {_money(rate)}  Amt {_money(amount)}")

    parts.extend(
        [
            "",
            f"*TOTAL:* Rs {_money(total_amount)}",
            f"Previous Balance: Rs {_money(previous_balance)}",
            f"Cash Received: Rs {_money(cash_received)}",
            f"Invoice Due: Rs {_money(invoice_due)}",
            f"*Net Balance:* Rs {_money(net_balance)}",
        ]
    )
    if pdf_url:
        parts.extend(["", "Invoice PDF:", pdf_url])

    message = "\n".join(parts)
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    if len(used_details) > 8:
        return build_invoice_message(invoice, used_details[:8], pdf_url)
    return message[:MAX_MESSAGE_CHARS]
