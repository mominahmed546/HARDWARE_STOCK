"""Shared Euroglass A4 tabular list PDF builder (items-list style)."""

from datetime import datetime
from io import BytesIO

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
X_LEFT = 14
X_RIGHT = PAGE_WIDTH - 14
TABLE_W = X_RIGHT - X_LEFT
LINE_H = 10
BOTTOM_MARGIN = 52
SR_COL_W = 22


def pdf_safe_text(value):
    text = str(value or "")
    for src, dst in (
        ("\u2014", "-"),
        ("\u2013", "-"),
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
    ):
        text = text.replace(src, dst)
    return text


def pdf_escape(value):
    return pdf_safe_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap_pdf_text(text_str, max_chars):
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


def format_money(value):
    return f"{float(value or 0):,.2f}"


def assemble_multipage_pdf(page_streams, page_width=PAGE_WIDTH, page_height=PAGE_HEIGHT):
    n = len(page_streams)
    font1_idx = 3 + 2 * n
    font2_idx = 4 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii"),
    ]

    for i, stream in enumerate(page_streams):
        content = stream.encode("latin-1", errors="replace")
        page_idx = 3 + 2 * i
        content_idx = 4 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 {font1_idx} 0 R /F2 {font2_idx} 0 R >> >> "
                f"/Contents {content_idx} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

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


def build_tabular_list_pdf(
    title,
    columns,
    rows,
    subtitle_lines=None,
    summary_rows=None,
    summary_value_from_index=-2,
):
    """
    Build a multi-page Euroglass tabular list PDF.

    columns: list of dicts with keys:
      - label (str)
      - width (int)
      - get (callable(row, index) -> str)
      - align ('left' | 'center' | 'right'), default 'center'
      - wrap (int | None): word-wrap threshold for left-aligned text
    summary_rows: optional list of (label, value) footer rows; value aligns to last column.
    summary_value_from_index: col_bounds index (supports negatives) for the left edge of
        the summary value area; defaults to the second-to-last column.
    """
    table_x = X_LEFT
    col_bounds = [table_x + SR_COL_W]
    for col in columns:
        col_bounds.append(col_bounds[-1] + col["width"])
    col_bounds[-1] = table_x + TABLE_W
    divider_xs = col_bounds[:-1]

    page_streams = []
    commands = []
    y = PAGE_HEIGHT - 26
    page_num = 1

    def text(x, y_pos, value, size=8, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y_pos} Td ({pdf_escape(value)}) Tj ET")

    def text_right(x, y_pos, value, size=8, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x - approx_width), y_pos, value, size, font)

    def text_center(x_center, y_pos, value, size=8, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x_center - (approx_width / 2)), y_pos, value, size, font)

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y_pos, width, height):
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def filled_rect(x, y_pos, width, height, r, g, b):
        commands.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg "
            f"{x} {y_pos} {width} {height} re f "
            f"0 0 0 rg"
        )
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def draw_table_header():
        nonlocal y
        header_y = y
        row_h = 18
        filled_rect(table_x, header_y - row_h + 4, TABLE_W, row_h, 0.88, 0.88, 0.88)
        for x in divider_xs:
            line(x, header_y - row_h + 4, x, header_y + 4)
        text_center(table_x + SR_COL_W / 2, header_y - 8, "#", 8, "F2")
        for idx, col in enumerate(columns):
            left = col_bounds[idx]
            right = col_bounds[idx + 1]
            text_center(left + (right - left) / 2, header_y - 8, col["label"], 8, "F2")
        y = header_y - row_h - 2

    def start_page():
        nonlocal y, commands
        commands = []
        y = PAGE_HEIGHT - 26
        text_center(PAGE_WIDTH / 2, y, "EUROGLASS HARDWARE", 14, "F2")
        y -= 12
        text_center(PAGE_WIDTH / 2, y, "Ph: 0300-5411417", 8, "F1")
        y -= 10
        line(X_LEFT, y, X_RIGHT, y)
        y -= 12
        text(X_LEFT, y, title, 10, "F2")
        text(X_LEFT + max(len(title) * 6, 88), y, datetime.now().strftime("%d/%m/%Y %I:%M:%S %p"), 8, "F1")
        y -= 11
        if subtitle_lines:
            for subtitle in subtitle_lines:
                text(X_LEFT, y, subtitle, 8, "F1")
                y -= 10
        text(X_LEFT, y, f"Page {page_num}", 8, "F1")
        y -= 10
        line(X_LEFT, y, X_RIGHT, y)
        y -= 12
        draw_table_header()

    def new_page():
        nonlocal page_num
        page_streams.append("\n".join(commands))
        page_num += 1
        start_page()

    start_page()

    for index, row in enumerate(rows, start=1):
        values = [col["get"](row, index) for col in columns]
        cell_lines = []
        for col_idx, col in enumerate(columns):
            wrap = col.get("wrap")
            if wrap:
                cell_lines.append(wrap_pdf_text(values[col_idx], wrap))
            else:
                cell_lines.append([values[col_idx]])

        row_lines = max((len(lines) for lines in cell_lines), default=1)
        dyn_row_h = max(18, 6 + row_lines * LINE_H)

        if y - dyn_row_h < BOTTOM_MARGIN:
            new_page()

        rect(table_x, y - dyn_row_h + 4, TABLE_W, dyn_row_h)
        for x in divider_xs:
            line(x, y - dyn_row_h + 4, x, y + 4)

        mid_y = y - (dyn_row_h / 2) + 2
        text_center(table_x + SR_COL_W / 2, mid_y, str(index), 8, "F1")

        text_y = y - 8
        for col_idx, col in enumerate(columns):
            align = col.get("align", "center")
            wrap = col.get("wrap")
            left = col_bounds[col_idx]
            right = col_bounds[col_idx + 1]
            if wrap:
                ty = text_y
                for wrapped_line in cell_lines[col_idx]:
                    text(left + 3, ty, wrapped_line, 8, "F1")
                    ty -= LINE_H
            elif align == "right":
                text_right(right - 3, mid_y, values[col_idx], 8, "F1")
            elif align == "left":
                text(left + 3, mid_y, values[col_idx], 8, "F1")
            else:
                text_center(left + (right - left) / 2, mid_y, values[col_idx], 8, "F1")

        y -= dyn_row_h

    if summary_rows:
        row_h_footer = 18
        if y - row_h_footer * len(summary_rows) < BOTTOM_MARGIN:
            new_page()
        y -= 8
        split_idx = summary_value_from_index
        if split_idx < 0:
            split_idx = len(col_bounds) + split_idx
        split_idx = max(1, min(split_idx, len(col_bounds) - 1))
        value_split_x = col_bounds[split_idx]
        for label, value in summary_rows:
            filled_rect(table_x, y - row_h_footer + 4, TABLE_W, row_h_footer, 0.88, 0.88, 0.88)
            line(value_split_x, y - row_h_footer + 4, value_split_x, y + 4)
            mid_y = y - (row_h_footer / 2) + 2
            text(table_x + 4, mid_y - 3, label, 10, "F2")
            text_right(col_bounds[-1] - 4, mid_y - 3, value, 10, "F2")
            y -= row_h_footer

    page_streams.append("\n".join(commands))
    return assemble_multipage_pdf(page_streams)


def build_invoice_style_report_pdf(
    title,
    info_lines=None,
    columns=None,
    rows=None,
    summary_rows=None,
    footer_from_index=-3,
):
    """A4 report that uses the same header, table, and footer layout as an invoice.

    columns: list of dicts with keys:
      - label (str)
      - width (int)
      - get (callable(row, index) -> str)
      - align ('left' | 'center' | 'right'), default 'right' for numbers
      - wrap (int | None): word-wrap threshold for left-aligned text
    summary_rows: list of dicts with keys:
      - label, value
      - highlight (bool)
      - color (optional (r, g, b) 0-1)
    footer_from_index: col_bounds index for the left edge of the boxed totals.
    """
    columns = columns or []
    rows = list(rows or [])
    info_lines = [line for line in (info_lines or []) if line]
    summary_rows = summary_rows or []

    sr_col_w = 24
    table_x = X_LEFT
    col_bounds = [table_x + sr_col_w]
    for col in columns:
        col_bounds.append(col_bounds[-1] + col["width"])
    col_bounds[-1] = table_x + TABLE_W
    divider_xs = col_bounds[:-1]

    page_streams = []
    commands = []
    y = PAGE_HEIGHT - 26
    page_num = 1
    row_h = 18

    def text(x, y_pos, value, size=9, font="F1", color=None):
        if color:
            r, g, b = color
            commands.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        commands.append(f"BT /{font} {size} Tf {x} {y_pos} Td ({pdf_escape(value)}) Tj ET")
        if color:
            commands.append("0 0 0 rg")

    def text_right(x, y_pos, value, size=9, font="F1", color=None):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x - approx_width), y_pos, value, size, font, color=color)

    def text_center(x_center, y_pos, value, size=9, font="F1", color=None):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x_center - (approx_width / 2)), y_pos, value, size, font, color=color)

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y_pos, width, height):
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def filled_rect(x, y_pos, width, height, r, g, b):
        commands.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg "
            f"{x} {y_pos} {width} {height} re f "
            f"0 0 0 rg"
        )
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def draw_table_header():
        nonlocal y
        header_y = y
        filled_rect(table_x, header_y - row_h + 4, TABLE_W, row_h, 0.88, 0.88, 0.88)
        for x in divider_xs:
            line(x, header_y - row_h + 4, x, header_y + 4)
        text_center(table_x + sr_col_w / 2, header_y - 8, "#", 8, "F2")
        for idx, col in enumerate(columns):
            left = col_bounds[idx]
            right = col_bounds[idx + 1]
            text_center(left + (right - left) / 2, header_y - 8, col["label"], 8, "F2")
        y = header_y - row_h - 2

    def start_page():
        nonlocal y, commands
        commands = []
        y = PAGE_HEIGHT - 26
        text_center(PAGE_WIDTH / 2, y, "EUROGLASS HARDWARE", 14, "F2")
        y -= 12
        text_center(PAGE_WIDTH / 2, y, "Ph: 0300-5411417", 8, "F1")
        y -= 10
        line(X_LEFT, y, X_RIGHT, y)
        y -= 12
        text(X_LEFT, y, title, 10, "F2")
        dated_x = X_LEFT + 248
        text(dated_x, y, "DATED", 10, "F2")
        text(dated_x + 48, y, datetime.now().strftime("%d/%m/%Y %I:%M:%S %p"), 8, "F1")
        y -= 11
        for info in info_lines:
            bold = info.startswith("Year:") or info.startswith("Account of:")
            text(X_LEFT, y, info, 9, "F2" if bold else "F1")
            y -= 10
        if page_num > 1:
            text(X_LEFT, y, f"Page {page_num}", 8, "F1")
            y -= 10
        line(X_LEFT, y, X_RIGHT, y)
        y -= 12
        draw_table_header()

    def new_page():
        nonlocal page_num
        page_streams.append("\n".join(commands))
        page_num += 1
        start_page()

    start_page()

    if not rows:
        rect(table_x, y - row_h + 4, TABLE_W, row_h)
        text(col_bounds[0] + 4, y - 8, "No items", 8, "F1")
        y -= row_h
    else:
        for index, row in enumerate(rows, start=1):
            values = [col["get"](row, index) for col in columns]
            cell_lines = []
            for col_idx, col in enumerate(columns):
                wrap = col.get("wrap")
                if wrap:
                    cell_lines.append(wrap_pdf_text(values[col_idx], wrap))
                else:
                    cell_lines.append([values[col_idx]])
            row_lines = max((len(lines) for lines in cell_lines), default=1)
            dyn_row_h = max(row_h, 6 + row_lines * LINE_H)
            if y - dyn_row_h < BOTTOM_MARGIN:
                new_page()

            rect(table_x, y - dyn_row_h + 4, TABLE_W, dyn_row_h)
            for x in divider_xs:
                line(x, y - dyn_row_h + 4, x, y + 4)

            mid_y = y - (dyn_row_h / 2) + 2
            text_center(table_x + sr_col_w / 2, mid_y, str(index), 8, "F1")
            text_y = y - 8
            for col_idx, col in enumerate(columns):
                align = col.get("align", "right")
                wrap = col.get("wrap")
                left = col_bounds[col_idx]
                right = col_bounds[col_idx + 1]
                if wrap:
                    ty = text_y
                    for wrapped_line in cell_lines[col_idx]:
                        text(left + 4, ty, wrapped_line, 8, "F1")
                        ty -= LINE_H
                elif align == "left":
                    text(left + 4, mid_y, values[col_idx], 8, "F1")
                elif align == "center":
                    text_center(left + (right - left) / 2, mid_y, values[col_idx], 8, "F1")
                else:
                    text_right(right - 4, mid_y, values[col_idx], 8, "F1")
            y -= dyn_row_h

    if summary_rows:
        footer_h = 18
        needed = 12 + footer_h * len(summary_rows) + 24
        if y - needed < BOTTOM_MARGIN:
            new_page()
        y -= 12
        # Invoice-style footer: boxed label | amount on the right side
        split_x = col_bounds[-2] if len(col_bounds) > 1 else (table_x + TABLE_W / 2)
        footer_idx = footer_from_index
        if footer_idx < 0:
            footer_idx = len(col_bounds) + footer_idx
        footer_idx = max(0, min(footer_idx, len(col_bounds) - 2))
        footer_x = col_bounds[footer_idx]
        footer_w = (table_x + TABLE_W) - footer_x

        def footer_row(label, amount_str, highlight=False, color=None, bold=False):
            nonlocal y
            if highlight:
                filled_rect(footer_x, y - footer_h + 4, footer_w, footer_h, 0.88, 0.88, 0.88)
            else:
                rect(footer_x, y - footer_h + 4, footer_w, footer_h)
            line(split_x, y - footer_h + 4, split_x, y + 4)
            mid_y = y - (footer_h / 2) + 2
            font = "F2" if highlight or bold else "F1"
            size = 11 if highlight or bold else 10
            text(footer_x + 4, mid_y - 3, label, size, font, color=color)
            text_right(table_x + TABLE_W - 4, mid_y - 3, amount_str, size, font, color=color)
            y -= footer_h

        for item in summary_rows:
            if isinstance(item, dict):
                footer_row(
                    item["label"],
                    item["value"],
                    highlight=bool(item.get("highlight")),
                    color=item.get("color"),
                    bold=bool(item.get("bold")),
                )
            else:
                footer_row(item[0], item[1], highlight=len(item) > 2 and item[2])

        y -= 20
        dash_x = X_LEFT
        seg = 5
        gap = 4
        while dash_x < X_RIGHT - seg:
            commands.append(f"0.4 w {dash_x:.1f} {y:.1f} m {dash_x + seg:.1f} {y:.1f} l S")
            dash_x += seg + gap

    page_streams.append("\n".join(commands))
    return assemble_multipage_pdf(page_streams)
