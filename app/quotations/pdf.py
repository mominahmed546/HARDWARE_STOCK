"""Build an A4 quotation PDF in the same style as the Excel quotation."""

import os
from datetime import date, datetime
from io import BytesIO

from app.quotations.excel import line_amount, sqft_for_line

LOGO_PATH = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    "static",
    "images",
    "euroglass-logo.png",
)


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


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


def _wrap_text(text_str, max_chars):
    words = str(text_str or "").split()
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [""]


def _money(value):
    return f"{float(value or 0):,.2f}"


def _qty(value):
    number = float(value or 0)
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _logo_jpeg():
    if not os.path.exists(LOGO_PATH):
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    image = Image.open(LOGO_PATH).convert("RGB")
    image.thumbnail((220, 220))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return image.size[0], image.size[1], buffer.getvalue()


def build_quotation_pdf(header, lines):
    commands = []
    page_width = 612
    left = 36
    right = page_width - 36
    line_h = 10
    row_h = 16
    used_lines = list(lines or [])
    extra_h = 0
    for line in used_lines:
        wrapped = _wrap_text(line.get("description") or "", 28)
        extra_h += max(row_h, 6 + len(wrapped) * line_h)
    page_height = max(792, 340 + extra_h)

    def text(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def text_right(x, y, value, size=9, font="F1"):
        value = str(value)
        text(max(left, x - len(value) * size * 0.5), y, value, size, font)

    def text_center(x_center, y, value, size=9, font="F1"):
        value = str(value)
        text(max(left, x_center - len(value) * size * 0.25), y, value, size, font)

    def draw_line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    logo = _logo_jpeg()
    logo_pt = 86
    if logo:
        commands.append(
            f"q {logo_pt} 0 0 {logo_pt} {right - logo_pt} {page_height - 18 - logo_pt} cm /Im1 Do Q"
        )

    y = page_height - 40
    text_center(page_width / 2, y, "EUROGLASS HARDWARE", 16, "F2")
    y -= 14
    text_center(page_width / 2, y, "Ph: 0300-5411417", 9)
    y -= 16
    text_center(page_width / 2, y, "QUOTATION", 13, "F2")
    y -= 10
    draw_line(left, y, right, y)
    y -= 16

    quotation_no = header.get("quotation_no") or ""
    quotation_date = _format_date(header.get("quotation_date") or date.today())
    text(left, y, f"No. {quotation_no}", 10, "F2")
    text_right(right, y, f"Date: {quotation_date}", 10, "F2")
    y -= 14
    text(left, y, f"Name: {header.get('customer_name') or ''}", 10, "F2")
    y -= 12
    if header.get("address"):
        text(left, y, f"Address: {header.get('address')}", 9)
        y -= 12
    text(left, y, f"Project: {header.get('project') or '-'}", 9)
    text(left + 280, y, f"Work Type: {header.get('work_type') or '-'}", 9)
    y -= 12
    if header.get("engineer") or header.get("contact_no"):
        text(left, y, f"Engineer: {header.get('engineer') or '-'}", 9)
        text(left + 280, y, f"Contact: {header.get('contact_no') or '-'}", 9)
        y -= 12
    draw_line(left, y, right, y)
    y -= 14

    col_sr = left
    col_desc = left + 28
    col_w = left + 250
    col_h = left + 292
    col_qty = left + 334
    col_sqft = left + 376
    col_rate = left + 430
    col_amt = right

    text(col_sr, y, "Sr", 8, "F2")
    text(col_desc, y, "Description", 8, "F2")
    text_right(col_w + 30, y, "W", 8, "F2")
    text_right(col_h + 30, y, "H", 8, "F2")
    text_right(col_qty + 30, y, "Qty", 8, "F2")
    text_right(col_sqft + 42, y, "SQ/FT", 8, "F2")
    text_right(col_rate + 42, y, "Rate", 8, "F2")
    text_right(col_amt, y, "Amount", 8, "F2")
    y -= 5
    draw_line(left, y, right, y)
    y -= 12

    gross = 0.0
    if not used_lines:
        text(col_desc, y, "No items", 8)
        y -= row_h
    else:
        for index, line in enumerate(used_lines, start=1):
            width = line.get("width") or 0
            height = line.get("height") or 0
            qty = line.get("quantity") or line.get("qty") or 0
            rate = line.get("rate") or 0
            sqft = sqft_for_line(width, height, qty, line.get("sqft"))
            amount = line_amount(width, height, qty, rate, sqft)
            gross += float(amount or 0)
            name_lines = _wrap_text(line.get("description") or "", 28)
            dyn_h = max(row_h, 4 + len(name_lines) * line_h)
            if y - dyn_h < 90:
                text(left, y, "Continued on printed copy...", 8)
                break
            text(col_sr, y, str(index), 8)
            text_y = y
            for name_line in name_lines:
                text(col_desc, text_y, name_line, 8)
                text_y -= line_h
            text_right(col_w + 30, y, _qty(width) if float(width or 0) else "", 8)
            text_right(col_h + 30, y, _qty(height) if float(height or 0) else "", 8)
            text_right(col_qty + 30, y, _qty(qty), 8)
            text_right(col_sqft + 42, y, f"{float(sqft or 0):.2f}", 8)
            text_right(col_rate + 42, y, _money(rate), 8)
            text_right(col_amt, y, _money(amount), 8)
            y -= dyn_h

    y -= 4
    draw_line(left, y, right, y)
    y -= 16
    stored_total = header.get("total_amount")
    if stored_total not in (None, ""):
        gross = float(stored_total or 0)
    advance = float(header.get("advance") or 0)
    text_right(right, y, f"Gross: Rs {_money(gross)}", 11, "F2")
    y -= 14
    text_right(right, y, f"Advance: Rs {_money(advance)}", 10, "F2")
    y -= 14
    text_right(right, y, f"Balance: Rs {_money(max(gross - advance, 0))}", 11, "F2")

    notes = str(header.get("notes") or "").strip()
    if notes:
        y -= 20
        text(left, y, "Note:", 9, "F2")
        y -= 12
        for note_line in _wrap_text(notes, 78)[:6]:
            text(left, y, note_line, 8)
            y -= 11

    content = "\n".join(commands).encode("latin-1", errors="replace")
    page_resources = b"/Font << /F1 4 0 R /F2 5 0 R >>"
    extra_objects = []
    if logo:
        page_resources += b" /XObject << /Im1 7 0 R >>"
        extra_objects.append(
            b"<< /Type /XObject /Subtype /Image /Width "
            + str(logo[0]).encode("ascii")
            + b" /Height "
            + str(logo[1]).encode("ascii")
            + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(logo[2])).encode("ascii")
            + b" >>\nstream\n"
            + logo[2]
            + b"\nendstream"
        )

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
        + str(page_width).encode("ascii")
        + b" "
        + str(page_height).encode("ascii")
        + b"] /Resources << "
        + page_resources
        + b" >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
        *extra_objects,
    ]

    pdf = BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{index} 0 obj\n".encode("ascii"))
        pdf.write(obj)
        pdf.write(b"\nendobj\n")

    xref_offset = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets:
        pdf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )
    pdf.seek(0)
    return pdf
