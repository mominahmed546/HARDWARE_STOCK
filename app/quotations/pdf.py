"""Build a quotation PDF that follows the Excel QUOTATION.xlsx form."""

from datetime import date, datetime
from io import BytesIO

from app.quotations.excel import MAX_LINE_ROWS, line_amount, sqft_for_line

# A4 portrait — the Excel sheet prints portrait, scaled to one page.
PAGE_W = 595.0
PAGE_H = 842.0
MARGIN_L = 16.0
MARGIN_R = 16.0
MARGIN_T = 14.0
MARGIN_B = 14.0

# Excel column widths A–N.
COL_UNITS = (
    11.714,
    14.524,
    9.143,
    12.0,
    9.143,
    13.0,
    9.429,
    34.0,
    17.857,
    20.0,
    10.286,
    20.714,
    11.571,
    27.429,
)

# Excel row heights 1–46.
ROW_UNITS = (
    40.5,
    39.95,
    39.95,
    44.1,
    44.1,
    44.1,
    44.1,
    61.0,
    60.0,
    42.0,
    41.0,
    5.25,
    5.25,
    54.75,
    44.1,
    *([44.1] * 22),
    45.75,
    27.75,
    3.0,
    42.0,
    42.0,
    42.0,
    45.0,
    130.5,
    48.0,
)

COLOR_SR = (0.902, 0.725, 0.722)
COLOR_DESC = (0.725, 0.804, 0.898)
COLOR_WIDTH = (0.847, 0.831, 0.792)
COLOR_HEIGHT = (0.800, 0.753, 0.855)
COLOR_QTY = (1.0, 1.0, 0.0)
COLOR_SQFT = (0.980, 0.753, 0.565)
COLOR_RATE = (0.851, 0.588, 0.580)
COLOR_AMOUNT = (0.933, 0.925, 0.882)
COLOR_VALUE = (0.949, 0.949, 0.949)
COLOR_NOTE_LABEL = (0.941, 0.851, 0.847)
COLOR_NOTE = (0.851, 0.890, 0.941)
COLOR_ADVANCE = (0.769, 0.843, 0.608)
COLOR_BALANCE = (1.0, 0.486, 0.502)
COLOR_YELLOW = (1.0, 1.0, 0.0)


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


def _fit_text(text_str, max_chars):
    text_str = str(text_str or "")
    if len(text_str) <= max_chars:
        return text_str
    if max_chars <= 1:
        return text_str[:max_chars]
    return text_str[: max_chars - 1] + "."


def _money(value):
    return f"{float(value or 0):,.2f}"


def _qty(value):
    number = float(value or 0)
    if number == 0:
        return ""
    if number == int(number):
        return str(int(number))
    return f"{number:g}"


def _text_width(value, size):
    return len(str(value)) * size * 0.5


def _layout():
    usable_h = PAGE_H - MARGIN_T - MARGIN_B
    usable_w = PAGE_W - MARGIN_L - MARGIN_R
    v_scale = usable_h / sum(ROW_UNITS)
    heights = [unit * v_scale for unit in ROW_UNITS]
    tops = []
    y_top = PAGE_H - MARGIN_T
    for height in heights:
        tops.append(y_top)
        y_top -= height

    h_scale = usable_w / sum(COL_UNITS)
    widths = [unit * h_scale for unit in COL_UNITS]
    xs = []
    x = MARGIN_L
    for width in widths:
        xs.append(x)
        x += width
    return heights, tops, widths, xs, v_scale


def build_quotation_pdf(header, lines):
    commands = []
    heights, tops, widths, xs, v_scale = _layout()

    def rect(r1, c1, r2=None, c2=None):
        """Excel 1-based row/col box in PDF coordinates."""
        r2 = r2 or r1
        c2 = c2 or c1
        x = xs[c1 - 1]
        w = (xs[c2 - 1] + widths[c2 - 1]) - x
        y = tops[r2 - 1] - heights[r2 - 1]
        h = tops[r1 - 1] - y
        return x, y, w, h

    def fill_rect(x, y, w, h, color):
        r, g, b = color
        commands.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
        commands.append("0 0 0 rg")

    def stroke_rect(x, y, w, h, weight=0.55):
        commands.append(f"0 0 0 RG {weight:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def box(r1, c1, r2=None, c2=None, color=None, weight=0.55):
        x, y, w, h = rect(r1, c1, r2, c2)
        if color:
            fill_rect(x, y, w, h, color)
        stroke_rect(x, y, w, h, weight)
        return x, y, w, h

    def text_at(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size:.2f} Tf {x:.2f} {y:.2f} Td ({_pdf_escape(value)}) Tj ET")

    def text_in(box_rect, value, size=9, font="F1", align="left", pad=3):
        value = str(value or "")
        if not value:
            return
        x, y, w, h = box_rect
        size = min(size, max(6.5, h * 0.72))
        max_chars = max(1, int((w - pad * 2) / max(size * 0.48, 1)))
        value = _fit_text(value, max_chars)
        tw = _text_width(value, size)
        if align == "center":
            tx = x + max(pad, (w - tw) / 2)
        elif align == "right":
            tx = x + w - pad - tw
        else:
            tx = x + pad
        ty = y + (h / 2) - (size * 0.32)
        text_at(max(x + 0.8, tx), ty, value, size, font)

    def font_pt(excel_pt):
        return max(6.5, min(22, excel_pt * v_scale * 0.9))

    # Title QUOTATION (A1:H1) and empty A2:H3, matching the sheet merges.
    title = box(1, 1, 1, 8, weight=0.8)
    text_in(title, "QUOTATION", font_pt(48), "F2", align="left", pad=6)
    box(2, 1, 3, 8, weight=0.4)
    box(1, 9, 9, 14, weight=0.8)

    fields = (
        (4, "INVOICE NO.", header.get("quotation_no") or "", font_pt(36), "F2"),
        (5, "DATE:", _format_date(header.get("quotation_date") or date.today()), font_pt(26), "F1"),
        (6, "NAME:", header.get("customer_name") or "", font_pt(26), "F1"),
        (7, "ADDRESS:", header.get("address") or "", font_pt(26), "F1"),
        (8, "PROJECT:", header.get("project") or "", font_pt(26), "F1"),
        (9, "WORK TYPE:", header.get("work_type") or "", font_pt(26), "F1"),
        (10, "ENGINEER:", header.get("engineer") or "", font_pt(26), "F1"),
        (11, "CONTACT:", header.get("contact_no") or "", font_pt(26), "F1"),
    )
    label_size = font_pt(24)
    for row, label, value, value_size, value_font in fields:
        label_box = box(row, 1, row, 2, weight=0.7)
        value_box = box(row, 3, row, 8, COLOR_VALUE, weight=0.7)
        text_in(label_box, label, label_size, "F2", align="left", pad=3)
        text_in(value_box, value, value_size, value_font, align="left", pad=4)

    headers = (
        (1, 1, "SR.NO", COLOR_SR, font_pt(20)),
        (2, 8, "DESCRIPTION", COLOR_DESC, font_pt(28)),
        (9, 9, "WIDTH''", COLOR_WIDTH, font_pt(24)),
        (10, 10, "HEIGHT''", COLOR_HEIGHT, font_pt(24)),
        (11, 11, "QTY", COLOR_QTY, font_pt(24)),
        (12, 12, "SQ/FT", COLOR_SQFT, font_pt(24)),
        (13, 13, "RATE", COLOR_RATE, font_pt(20)),
        (14, 14, "AMOUNT", COLOR_AMOUNT, font_pt(24)),
    )
    for c1, c2, title, color, size in headers:
        header_box = box(15, c1, 15, c2, color, weight=0.65)
        text_in(header_box, title, size, "F2", align="center")

    used_lines = list(lines or [])[:MAX_LINE_ROWS]
    body_size = font_pt(20)
    gross = 0.0
    table_cols = (
        (1, 1, "center"),
        (2, 8, "left"),
        (9, 9, "center"),
        (10, 10, "center"),
        (11, 11, "center"),
        (12, 12, "center"),
        (13, 13, "center"),
        (14, 14, "center"),
    )
    for offset in range(MAX_LINE_ROWS):
        row = 16 + offset
        values = ["", "", "", "", "", "", "", ""]
        if offset < len(used_lines):
            line = used_lines[offset]
            width = line.get("width") or 0
            height = line.get("height") or 0
            qty = line.get("quantity") or line.get("qty") or 0
            rate = line.get("rate") or 0
            sqft = sqft_for_line(width, height, qty, line.get("sqft"))
            amount = line_amount(width, height, qty, rate, sqft)
            gross += float(amount or 0)
            values = [
                str(offset + 1),
                line.get("description") or line.get("item_name") or "",
                _qty(width),
                _qty(height),
                _qty(qty),
                f"{float(sqft or 0):,.2f}" if float(sqft or 0) else "",
                _money(rate) if float(rate or 0) else "",
                _money(amount) if float(amount or 0) else "",
            ]
        for (c1, c2, align), value in zip(table_cols, values):
            data_box = box(row, c1, row, c2, weight=0.4)
            if c1 == 2:
                x, y, w, h = data_box
                max_chars = max(8, int((w - 6) / max(body_size * 0.48, 1)))
                wrapped = _wrap_text(value, max_chars)[:2]
                if len(wrapped) == 1:
                    text_in(data_box, wrapped[0], body_size, "F1", align="left", pad=3)
                else:
                    line_h = h / 2
                    for index, part in enumerate(wrapped):
                        part_box = (x, y + h - (index + 1) * line_h, w, line_h)
                        text_in(part_box, part, max(6.5, body_size - 0.6), "F1", align="left", pad=3)
            else:
                text_in(data_box, value, body_size, "F1", align=align)

    stored_total = header.get("total_amount")
    if stored_total not in (None, ""):
        gross = float(stored_total or 0)
    advance = float(header.get("advance") or 0)
    balance = gross - advance

    note_parts = [part.strip() for part in str(header.get("notes") or "").splitlines() if part.strip()]
    if not note_parts:
        notes_text = str(header.get("notes") or "").strip()
        note_parts = _wrap_text(notes_text, 48)[:3] if notes_text else []
    while len(note_parts) < 3:
        note_parts.append("")

    # A41 NOTE, B41:I41 / B42:I42 / B43:I43 notes, matching the sheet.
    box(41, 1, 41, 1, COLOR_NOTE_LABEL, weight=0.7)
    text_in(rect(41, 1), "NOTE", font_pt(20), "F2", align="left", pad=2)
    box(42, 1, 43, 1, COLOR_NOTE, weight=0.5)
    for row in (41, 42, 43):
        note_box = box(row, 2, row, 9, COLOR_NOTE, weight=0.5)
        text_in(note_box, note_parts[row - 41], font_pt(22), "F1", align="left", pad=4)

    totals = (
        (41, "GROSS  AMOUNT", _money(gross), COLOR_YELLOW, "F1"),
        (42, "ADVANCE", _money(advance), COLOR_ADVANCE, "F1"),
        (43, "BALANCE DUE", _money(balance), COLOR_BALANCE, "F2"),
    )
    for row, label, value, color, value_font in totals:
        label_box = box(row, 10, row, 12, color, weight=0.8)
        text_in(label_box, label, font_pt(28), "F2", align="center")
        if row == 42:
            box(row, 13, row, 13, weight=0.6)
            value_box = box(row, 14, row, 14, weight=0.8)
        else:
            value_box = box(row, 13, row, 14, weight=0.8)
        text_in(value_box, value, font_pt(28), value_font, align="center")

    disclaimer = box(44, 1, 44, 8, weight=0.7)
    text_in(
        disclaimer,
        "Any error/mistake in calculation is liable to be adjusted:",
        font_pt(22),
        "F1",
        align="center",
        pad=2,
    )
    box(44, 9, 44, 14, weight=0.4)

    sign = box(46, 11, 46, 14, COLOR_YELLOW, weight=0.8)
    text_in(sign, "for EUROGLASS", font_pt(28), "F2", align="left", pad=5)

    content = "\n".join(commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
        + str(int(PAGE_W)).encode("ascii")
        + b" "
        + str(int(PAGE_H)).encode("ascii")
        + b"] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
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
