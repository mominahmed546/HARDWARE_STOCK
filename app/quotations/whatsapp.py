"""Build a WhatsApp click-to-chat link for a saved quotation."""

from datetime import date, datetime

from app.quotations.excel import line_amount, sqft_for_line
from app.whatsapp import whatsapp_digits, whatsapp_url

MAX_MESSAGE_CHARS = 1800

__all__ = ["build_quotation_message", "whatsapp_digits", "whatsapp_url"]


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


def build_quotation_message(header, lines):
    quotation_no = header.get("quotation_no") or ""
    parts = [
        "*EUROGLASS HARDWARE*",
        f"*QUOTATION No. {quotation_no}*",
        f"Date: {_format_date(header.get('quotation_date') or date.today())}",
        "",
        f"Name: {header.get('customer_name') or '-'}",
    ]
    if header.get("address"):
        parts.append(f"Address: {header['address']}")
    if header.get("project"):
        parts.append(f"Project: {header['project']}")
    if header.get("work_type"):
        parts.append(f"Work Type: {header['work_type']}")
    if header.get("engineer"):
        parts.append(f"Engineer: {header['engineer']}")
    if header.get("contact_no"):
        parts.append(f"Contact: {header['contact_no']}")

    used_lines = list(lines or [])
    parts.append("")
    parts.append("*Items*")
    if not used_lines:
        parts.append("No items")
    else:
        for index, line in enumerate(used_lines, start=1):
            width = line.get("width") or 0
            height = line.get("height") or 0
            qty = line.get("quantity") or line.get("qty") or 0
            rate = line.get("rate") or 0
            sqft = sqft_for_line(width, height, qty, line.get("sqft"))
            amount = line_amount(width, height, qty, rate, sqft)
            description = line.get("description") or line.get("item_name") or "Item"
            detail = [f"{index}. {description}"]
            measures = []
            if float(width or 0):
                measures.append(f"W {_qty(width)}\"")
            if float(height or 0):
                measures.append(f"H {_qty(height)}\"")
            measures.append(f"Qty {_qty(qty)}")
            if float(sqft or 0):
                measures.append(f"SQ/FT {float(sqft):.2f}")
            if float(rate or 0):
                measures.append(f"Rate {_money(rate)}")
            measures.append(f"Amt {_money(amount)}")
            detail.append("   " + "  ".join(measures))
            parts.extend(detail)

    stored_total = header.get("total_amount")
    if stored_total not in (None, ""):
        gross = float(stored_total or 0)
    else:
        gross = 0.0
        for line in used_lines:
            width = line.get("width") or 0
            height = line.get("height") or 0
            qty = line.get("quantity") or line.get("qty") or 0
            rate = line.get("rate") or 0
            gross += float(line_amount(width, height, qty, rate, line.get("sqft")) or 0)

    advance = float(header.get("advance") or 0)
    parts.extend(
        [
            "",
            f"*GROSS AMOUNT:* Rs {_money(gross)}",
            f"*ADVANCE:* Rs {_money(advance)}",
            f"*BALANCE DUE:* Rs {_money(gross - advance)}",
        ]
    )
    notes = str(header.get("notes") or "").strip()
    if notes:
        parts.extend(["", f"Note: {notes}"])

    message = "\n".join(parts)
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    if len(used_lines) > 8:
        extra = len(used_lines) - 8
        header_copy = dict(header)
        note = str(header_copy.get("notes") or "").strip()
        suffix = f"+ {extra} more item(s)."
        header_copy["notes"] = f"{note} {suffix}".strip() if note else suffix
        return build_quotation_message(header_copy, used_lines[:8])
    return message[:MAX_MESSAGE_CHARS]
