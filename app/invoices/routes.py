from datetime import date, datetime
from io import BytesIO

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.cogs import purchase_unit_cost_join, sold_line_cost_sql
from app.db import get_db_connection
from app.wa_api import is_configured as wa_api_configured
from app.wa_api import send_file_bytes as wa_send_file
from app.whatsapp import public_file_url, whatsapp_url
from app.tenancy import next_table_id, owner_sql, request_user_id
from app.payments import (
    add_invoice_payment,
    clear_invoice_payments,
    delete_invoice_payment,
    ensure_invoice_payments_table,
    invoice_paid_total,
    list_invoice_payments,
    paid_ratio_sql,
    pay_invoice_remaining,
    payments_join_sql,
    normalize_payment_method,
    refresh_invoice_settlement,
    remaining_due,
)
from app.validators import (
    ValidationErrors,
    clean_date,
    clean_optional_string,
    clean_positive_decimal,
    clean_positive_int,
    clean_select_id,
    clean_phone,
)

invoices_bp = Blueprint("invoices", __name__, url_prefix="/invoices")


def _ensure_previous_balance_column(db, cursor):
    cursor.execute(
        """
        ALTER TABLE Customers
        ADD COLUMN IF NOT EXISTS PreviousBalance NUMERIC(12, 2) DEFAULT 0
        """
    )
    db.commit()


def _ensure_invoice_payment_status_column(db, cursor):
    cursor.execute(
        """
        ALTER TABLE Invoices
        ADD COLUMN IF NOT EXISTS PaymentStatus VARCHAR(20) DEFAULT 'Unpaid'
        """
    )
    cursor.execute(
        """
        UPDATE Invoices
        SET PaymentStatus = 'Unpaid'
        WHERE PaymentStatus IS NULL OR BTRIM(PaymentStatus) = ''
        """
    )
    db.commit()


def _ensure_invoice_previous_balance_column(db, cursor):
    cursor.execute(
        """
        ALTER TABLE Invoices
        ADD COLUMN IF NOT EXISTS PreviousBalance NUMERIC(12, 2) DEFAULT 0
        """
    )
    db.commit()


def _ensure_invoice_settlement_columns(db, cursor):
    cursor.execute(
        """
        ALTER TABLE Invoices
        ADD COLUMN IF NOT EXISTS CashReceived NUMERIC(12, 2) DEFAULT 0
        """
    )
    cursor.execute(
        """
        ALTER TABLE Invoices
        ADD COLUMN IF NOT EXISTS NetBalance NUMERIC(12, 2) DEFAULT 0
        """
    )
    db.commit()


def _ensure_invoice_schema(db, cursor):
    _ensure_previous_balance_column(db, cursor)
    _ensure_invoice_payment_status_column(db, cursor)
    _ensure_invoice_previous_balance_column(db, cursor)
    _ensure_invoice_settlement_columns(db, cursor)
    _ensure_invoice_date_is_timestamp(db, cursor)
    ensure_invoice_payments_table(db, cursor)


def _invoice_settlement(previous_balance, total_amount, paid_amount=0):
    """Cash received is money paid on this invoice; net includes previous balance."""
    previous_balance = float(previous_balance or 0)
    total_amount = float(total_amount or 0)
    paid_amount = float(paid_amount or 0)
    cash_received = paid_amount
    net_balance = previous_balance + total_amount - cash_received
    if net_balance < 0:
        net_balance = 0.0
    return cash_received, net_balance


def _load_invoice_record(cursor, invoice_id):
    cursor.execute(
        f"""
        SELECT
            i.InvoiceID,
            i.CustomerID,
            i.[Date] AS InvoiceDate,
            COALESCE(i.TotalAmount, 0) AS TotalAmount,
            COALESCE(i.PreviousBalance, 0) AS PreviousBalance,
            COALESCE(i.PaymentStatus, 'Unpaid') AS PaymentStatus,
            COALESCE(pay.PaidAmount, 0) AS PaidAmount,
            COALESCE(pay.PaidAmount, 0) AS CashReceived,
            GREATEST(
                COALESCE(i.PreviousBalance, 0)
                    + COALESCE(i.TotalAmount, 0)
                    - COALESCE(pay.PaidAmount, 0),
                0
            ) AS NetBalance,
            GREATEST(COALESCE(i.TotalAmount, 0) - COALESCE(pay.PaidAmount, 0), 0) AS RemainingAmount,
            c.CustomerName,
            c.ContactNo
        FROM Invoices i
        JOIN Customers c ON c.CustomerID = i.CustomerID
        {payments_join_sql("i")}
        WHERE i.InvoiceID = ? AND {owner_sql("i")}
        """,
        (invoice_id,),
    )
    return cursor.fetchone()


def _ensure_invoice_date_is_timestamp(db, cursor):
    """Migrate the date column from DATE to TIMESTAMP WITH TIME ZONE if needed."""
    cursor.execute(
        """
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'invoices' AND column_name = 'date'
        """
    )
    row = cursor.fetchone()
    if row and str(row[0]).lower() in ("date",):
        cursor.execute(
            "ALTER TABLE invoices ALTER COLUMN date TYPE TIMESTAMP USING date::TIMESTAMP"
        )
        db.commit()


def _validate_invoice_header(form, errors):
    prev_bal_raw = form.get("previous_balance", "0") or "0"
    try:
        prev_bal = float(prev_bal_raw)
        if prev_bal < 0:
            prev_bal = 0.0
    except (ValueError, TypeError):
        prev_bal = 0.0

    return {
        "invoice_date": clean_date(form.get("invoice_date"), "invoice_date", errors, label="Invoice date"),
        "customer_id": clean_select_id(form.get("customer_id"), "customer_id", errors, label="Customer"),
        "previous_balance": prev_bal,
    }


def _invoice_lines_from_form(form):
    item_ids = form.getlist("item_id[]")
    quantities = form.getlist("quantity[]")
    rates = form.getlist("rate[]")

    line_count = max(len(item_ids), len(quantities), len(rates), 1)
    lines = []

    for index in range(line_count):
        lines.append(
            {
                "item_id": item_ids[index] if index < len(item_ids) else "",
                "quantity": quantities[index] if index < len(quantities) else "",
                "rate": rates[index] if index < len(rates) else "",
            }
        )

    return lines


def _default_invoice_lines():
    return [{"item_id": "", "quantity": "1", "rate": "0"}]


def _validate_invoice_lines(form, cursor, errors, extra_stock_by_item=None):
    lines = _invoice_lines_from_form(form)
    valid_lines = []
    requested_qty_by_item = {}
    extra_stock_by_item = extra_stock_by_item or {}

    if not any(line["item_id"] or line["quantity"] or line["rate"] for line in lines):
        errors.add("item_id[]", "At least one item is required.")
        return lines, valid_lines

    for line in lines:
        item_id = line["item_id"]
        quantity = line["quantity"]
        rate = line["rate"]

        if not item_id and not quantity and not rate:
            continue

        if not item_id or not quantity or not rate:
            errors.add("item_id[]", "Each item row must include an item, quantity, and sale rate.")
            break

        item_value = clean_select_id(item_id, "item_id[]", errors, label="Item")
        quantity_value = clean_positive_int(quantity, "quantity[]", errors, min_val=1, label="Quantity")
        rate_value = clean_positive_decimal(rate, "rate[]", errors, label="Sale rate")

        if not errors.valid:
            break

        cursor.execute(
            f"SELECT ItemID, ItemName, Qty FROM Item WHERE ItemID = ? AND {owner_sql()}",
            (item_value,),
        )
        item = cursor.fetchone()

        if not item:
            errors.add("item_id[]", "Selected item was not found.")
            break

        available_qty = int(item.Qty or 0) + int(extra_stock_by_item.get(item_value, 0) or 0)
        requested_qty_by_item[item_value] = requested_qty_by_item.get(item_value, 0) + quantity_value
        if requested_qty_by_item[item_value] > available_qty:
            errors.add("quantity[]", f"Only {available_qty} item(s) are available for {item.ItemName}.")
            break

        valid_lines.append(
            {
                "item_id": item_value,
                "item_name": item.ItemName,
                "quantity": quantity_value,
                "rate": rate_value,
                "total": quantity_value * rate_value,
            }
        )

    if errors.valid and not valid_lines:
        errors.add("item_id[]", "At least one valid item line is required.")

    return lines, valid_lines


def _load_invoice_form_data(cursor, extra_item_ids=None, exclude_invoice_id=None):
    unpaid_filter = ""
    unpaid_params = []
    if exclude_invoice_id is not None:
        unpaid_filter = "WHERE i.InvoiceID <> ?"
        unpaid_params.append(exclude_invoice_id)

    cursor.execute(
        f"""
        SELECT
            c.CustomerID,
            c.CustomerName,
            COALESCE(c.PreviousBalance, 0)
                + COALESCE(unpaid.UnpaidTotal, 0) AS PreviousBalance
        FROM Customers c
        LEFT JOIN (
            SELECT
                i.CustomerID,
                SUM(
                    GREATEST(
                        COALESCE(i.TotalAmount, 0) - COALESCE(pay.PaidAmount, 0),
                        0
                    )
                ) AS UnpaidTotal
            FROM Invoices i
            {payments_join_sql("i")}
            {unpaid_filter}
            {"AND" if unpaid_filter else "WHERE"} {owner_sql("i")}
            GROUP BY i.CustomerID
        ) unpaid ON unpaid.CustomerID = c.CustomerID
        WHERE {owner_sql("c")}
        ORDER BY c.CustomerName
        """,
        unpaid_params,
    )
    customers = cursor.fetchall()

    extra_item_ids = [int(item_id) for item_id in (extra_item_ids or []) if item_id]
    if extra_item_ids:
        placeholders = ",".join("?" * len(extra_item_ids))
        cursor.execute(
            f"""
            SELECT ItemID, ItemName, SaleRate, Qty
            FROM Item
            WHERE ({owner_sql()}) AND (Qty > 0 OR ItemID IN ({placeholders}))
            ORDER BY ItemName
            """,
            extra_item_ids,
        )
    else:
        cursor.execute(
            f"""
            SELECT ItemID, ItemName, SaleRate, Qty
            FROM Item
            WHERE Qty > 0 AND {owner_sql()}
            ORDER BY ItemName
            """
        )
    items = cursor.fetchall()

    return customers, items


def _invoice_date_iso(value):
    if not value:
        return date.today().isoformat()
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")

    value = str(value).strip()
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date.today().isoformat()


def _invoice_time(value):
    if hasattr(value, "time"):
        return value.time()
    return datetime.now().time()


def _existing_invoice_stock(cursor, invoice_id):
    cursor.execute(
        """
        SELECT ItemID, Qty
        FROM InvoiceDetails
        WHERE InvoiceID = ?
        """,
        (invoice_id,),
    )
    extra_stock_by_item = {}
    extra_item_ids = []
    for row in cursor.fetchall():
        item_id = row.ItemID
        extra_item_ids.append(item_id)
        extra_stock_by_item[item_id] = extra_stock_by_item.get(item_id, 0) + int(row.Qty or 0)
    return extra_stock_by_item, extra_item_ids


def _invoice_lines_from_details(cursor, invoice_id):
    cursor.execute(
        """
        SELECT ItemID, Qty, Rate
        FROM InvoiceDetails
        WHERE InvoiceID = ?
        ORDER BY DetailID
        """,
        (invoice_id,),
    )
    lines = [
        {
            "item_id": str(row.ItemID or ""),
            "quantity": str(row.Qty or 1),
            "rate": str(row.Rate or 0),
        }
        for row in cursor.fetchall()
    ]
    return lines or _default_invoice_lines()


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _format_date_dmy(value):
    if not value:
        return ""

    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")

    value = str(value).strip()
    for date_format in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:19], date_format).strftime("%d/%m/%Y")
        except ValueError:
            continue

    return value


def _load_invoice_pdf_details(cursor, invoice_id):
    cursor.execute(
        """
        SELECT Particulars, Qty, Rate, (Qty * Rate) AS TotalAmount
        FROM InvoiceDetails
        WHERE InvoiceID = ?
        """,
        (invoice_id,),
    )
    return cursor.fetchall()


def _send_invoice_pdf(invoice, details, invoice_id, as_attachment=False):
    pdf = _build_invoice_pdf(invoice, details)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=as_attachment,
        download_name=f"invoice_{invoice_id}.pdf",
    )


def _format_datetime_for_invoice(value):
    if not value:
        return ""

    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y %I:%M:%S %p")

    value = str(value).strip()
    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value[:19], date_format)
            return parsed.strftime("%d/%m/%Y %I:%M:%S %p")
        except ValueError:
            continue

    return value


def _wrap_text(text_str, max_chars):
    """Split text_str into lines of at most max_chars characters, breaking on spaces."""
    words = text_str.split()
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


def _build_invoice_pdf(invoice, details):
    commands = []
    details = list(details)

    # A4 portrait: 595 × 842 pt
    receipt_width = 595
    receipt_height = 842
    line_h = 10
    max_name_chars = 42

    def text(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y, width, height):
        commands.append(f"0.6 w {x} {y} {width} {height} re S")

    def filled_rect(x, y, width, height, r, g, b):
        commands.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg "
            f"{x} {y} {width} {height} re f "
            f"0 0 0 rg"
        )
        commands.append(f"0.6 w {x} {y} {width} {height} re S")

    def money(value):
        return f"{float(value or 0):,.2f}"

    def text_right(x, y, value, size=9, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x - approx_width), y, value, size, font)

    def text_center(x_center, y, value, size=9, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x_center - (approx_width / 2)), y, value, size, font)

    x_left = 14
    x_right = receipt_width - 14
    y = receipt_height - 26

    customer_name = str(invoice.CustomerName or "N/A")
    contact_no = str(getattr(invoice, "ContactNo", "") or "")
    previous_balance = float(getattr(invoice, "PreviousBalance", 0) or 0)

    text_center(receipt_width / 2, y, "EUROGLASS HARDWARE", 14, "F2")
    y -= 12
    text_center(receipt_width / 2, y, "Ph: 0300-5411417", 8, "F1")
    y -= 10
    line(x_left, y, x_right, y)
    y -= 12

    text(x_left, y, "Invoice", 10, "F2")
    text(x_left + 60, y, str(invoice.InvoiceID), 10, "F2")
    text(x_left + 120, y, "DATED", 10, "F2")
    text(x_left + 168, y, _format_datetime_for_invoice(invoice.InvoiceDate), 8, "F1")
    y -= 11
    text(x_left, y, f"Customer: {customer_name}", 10, "F2")
    y -= 10
    if contact_no:
        text(x_left, y, contact_no, 8, "F1")
        y -= 10
    line(x_left, y, x_right, y)
    y -= 12

    table_x = x_left
    table_w = x_right - x_left

    # Column layout: # | PRODUCT NAME | QTY | RATE | TOTAL  (A4 widths)
    col_sr_w = 24
    col_sr_right = table_x + col_sr_w
    col_product_right = col_sr_right + 280
    col_qty_right = col_product_right + 44
    col_rate_right = col_qty_right + 90
    col_total_right = table_x + table_w

    header_y = y
    row_h = 18
    # Light grey header — black text, prints clearly
    filled_rect(table_x, header_y - row_h + 4, table_w, row_h, 0.88, 0.88, 0.88)
    line(col_sr_right,      header_y - row_h + 4, col_sr_right,      header_y + 4)
    line(col_product_right, header_y - row_h + 4, col_product_right, header_y + 4)
    line(col_qty_right,     header_y - row_h + 4, col_qty_right,     header_y + 4)
    line(col_rate_right,    header_y - row_h + 4, col_rate_right,    header_y + 4)
    text_center(table_x + col_sr_w / 2,               header_y - 8, "#",            8, "F2")
    text(col_sr_right + 4,                            header_y - 8, "PRODUCT NAME", 9, "F2")
    text(col_product_right + 4,                       header_y - 8, "QTY",          9, "F2")
    text(col_qty_right + 4,                           header_y - 8, "RATE",         9, "F2")
    text(col_rate_right + 4,                          header_y - 8, "AMOUNT",       9, "F2")
    y = header_y - row_h - 2

    if not details:
        rect(table_x, y - row_h + 4, table_w, row_h)
        text(col_sr_right + 4, y - 8, "No items", 8, "F1")
        y -= row_h
    else:
        for index, detail in enumerate(details, start=1):
            item_name = str(detail.Particulars or "Item")
            name_lines = _wrap_text(item_name, max_name_chars)
            dyn_row_h = max(18, 6 + len(name_lines) * line_h)

            rect(table_x, y - dyn_row_h + 4, table_w, dyn_row_h)
            line(col_sr_right,      y - dyn_row_h + 4, col_sr_right,      y + 4)
            line(col_product_right, y - dyn_row_h + 4, col_product_right, y + 4)
            line(col_qty_right,     y - dyn_row_h + 4, col_qty_right,     y + 4)
            line(col_rate_right,    y - dyn_row_h + 4, col_rate_right,    y + 4)

            mid_y = y - (dyn_row_h / 2) + 2
            text_center(table_x + col_sr_w / 2, mid_y, str(index), 8, "F1")

            text_y = y - 8
            for name_line in name_lines:
                text(col_sr_right + 4, text_y, name_line, 8, "F1")
                text_y -= line_h

            text_right(col_qty_right - 4,  mid_y, str(detail.Qty),          8, "F1")
            text_right(col_rate_right - 4, mid_y, money(detail.Rate),       8, "F1")
            text_right(col_total_right - 4, mid_y, money(detail.TotalAmount), 8, "F1")
            y -= dyn_row_h

    y -= 12
    items_count = len(details)
    total_amount = float(invoice.TotalAmount or 0)
    if hasattr(invoice, "CashReceived") and hasattr(invoice, "NetBalance"):
        cash_received = float(invoice.CashReceived or 0)
        net_balance = float(invoice.NetBalance or 0)
    else:
        paid_amount = float(getattr(invoice, "PaidAmount", 0) or 0)
        cash_received, net_balance = _invoice_settlement(
            previous_balance,
            total_amount,
            paid_amount,
        )

    invoice_due = max(total_amount - cash_received, 0)

    # ── Footer table ─────────────────────────────────────────────────────────
    # Box spans only from col_qty_right to col_total_right (no empty left cells).
    # One divider at col_rate_right separates label from amount.
    # TOTAL and Net Balance: dark-navy fill + white bold text.
    row_h_footer = 18
    footer_w = col_total_right - col_qty_right

    def footer_row(label, amount_str, highlight=False):
        nonlocal y
        bx = col_qty_right           # box starts at RATE column left edge
        if highlight:
            filled_rect(bx, y - row_h_footer + 4, footer_w, row_h_footer,
                        0.88, 0.88, 0.88)
        else:
            rect(bx, y - row_h_footer + 4, footer_w, row_h_footer)
        # Divider between RATE label cell and TOTAL amount cell
        line(col_rate_right, y - row_h_footer + 4, col_rate_right, y + 4)
        mid_y = y - (row_h_footer / 2) + 2
        font = "F2" if highlight else "F1"
        size = 11 if highlight else 10
        text(bx + 4, mid_y - 3, label, size, font)
        text_right(col_total_right - 4, mid_y - 3, amount_str, size, font)
        y -= row_h_footer

    footer_row("TOTAL",            money(total_amount),    highlight=True)
    footer_row("Previous Balance", money(previous_balance))
    footer_row("Cash Received",    money(cash_received))
    footer_row("Invoice Due",      money(invoice_due))
    footer_row("Net Balance",      money(net_balance),     highlight=True)

    # ── Tear-off dotted line ─────────────────────────────────────────────────
    y -= 20
    dash_x = x_left
    seg = 5
    gap = 4
    while dash_x < x_right - seg:
        commands.append(f"0.4 w {dash_x:.1f} {y:.1f} m {dash_x + seg:.1f} {y:.1f} l S")
        dash_x += seg + gap
    pass  # tear-off dotted line only, no text label

    content = "\n".join(commands).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 "
        + str(receipt_width).encode("ascii")
        + b" "
        + str(receipt_height).encode("ascii")
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


@invoices_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_invoice():
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {"invoice_date": date.today().isoformat()}
    invoice_lines = _default_invoice_lines()

    try:
        _ensure_invoice_schema(db, cursor)
        customers, items = _load_invoice_form_data(cursor)

        if request.method == "POST":
            form_data = request.form.to_dict()
            invoice_lines = _invoice_lines_from_form(request.form)
            action = request.form.get("action", "create_invoice")

            if action == "save_previous_balance":
                customer_id = clean_select_id(request.form.get("customer_id"), "customer_id", errors, label="Customer")
                previous_balance = clean_positive_decimal(
                    request.form.get("previous_balance"),
                    "previous_balance",
                    errors,
                    min_val=0,
                    label="Previous balance",
                )

                if not errors.valid:
                    flash(errors.first(), "danger")
                    return render_template(
                        "invoices/form.html",
                        customers=customers,
                        items=items,
                        errors=errors.errors,
                        form_data=form_data,
                        invoice_lines=invoice_lines,
                    )

                cursor.execute(
                    f"UPDATE Customers SET PreviousBalance = ? WHERE CustomerID = ? AND {owner_sql()}",
                    (previous_balance, customer_id),
                )
                db.commit()
                flash("Previous balance updated successfully.", "success")

                customers, items = _load_invoice_form_data(cursor)
                return render_template(
                    "invoices/form.html",
                    customers=customers,
                    items=items,
                    errors=errors.errors,
                    form_data=form_data,
                    invoice_lines=invoice_lines,
                )

            data = _validate_invoice_header(request.form, errors)
            invoice_lines, valid_lines = _validate_invoice_lines(request.form, cursor, errors)

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "invoices/form.html",
                    customers=customers,
                    items=items,
                    errors=errors.errors,
                    form_data=form_data,
                    invoice_lines=invoice_lines,
                )

            total = sum(line["total"] for line in valid_lines)

            # Use the date selected on the form, keeping the current time of day
            invoice_datetime = datetime.combine(
                datetime.strptime(data["invoice_date"], "%Y-%m-%d").date(),
                datetime.now().time(),
            )

            # Persist manually entered previous balance so future invoices
            # start from the same user-confirmed opening balance.
            cursor.execute(
                f"UPDATE Customers SET PreviousBalance = ? WHERE CustomerID = ? AND {owner_sql()}",
                (data["previous_balance"], data["customer_id"]),
            )

            # Assign invoice number as MAX(invoice_id)+1 so deleted numbers are reused
            next_id = next_table_id(cursor, "Invoices", "InvoiceID")

            cash_received, net_balance = _invoice_settlement(
                data["previous_balance"], total, 0
            )

            cursor.execute(
                """
                INSERT INTO Invoices (
                    InvoiceID, CustomerID, [Date], TotalAmount, PaymentStatus,
                    PreviousBalance, CashReceived, NetBalance, UserID
                )
                OUTPUT INSERTED.InvoiceID
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_id,
                    data["customer_id"],
                    invoice_datetime,
                    total,
                    "Unpaid",
                    data["previous_balance"],
                    cash_received,
                    net_balance,
                    request_user_id(),
                ),
            )
            invoice_id = int(cursor.fetchone()[0])

            for line in valid_lines:
                cursor.execute(
                    """
                    INSERT INTO InvoiceDetails (InvoiceID, ItemID, Rate, Qty, Particulars)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (invoice_id, line["item_id"], line["rate"], line["quantity"], line["item_name"]),
                )

                cursor.execute(
                    f"""
                    UPDATE Item
                    SET Qty = Qty - ?
                    WHERE ItemID = ? AND {owner_sql()}
                    """,
                    (line["quantity"], line["item_id"]),
                )

            db.commit()
            flash("Invoice created successfully.", "success")
            return redirect(url_for("invoices.invoice_pdf", id=invoice_id))

        return render_template(
            "invoices/form.html",
            customers=customers,
            items=items,
            errors=errors.errors,
            form_data=form_data,
            invoice_lines=invoice_lines,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error creating invoice: {str(e)}", "danger")
        return render_template(
            "invoices/form.html",
            customers=[],
            items=[],
            errors=errors.errors,
            form_data=form_data,
            invoice_lines=invoice_lines,
        )

    finally:
        cursor.close()


@invoices_bp.route("/list")
@login_required
def list_invoices():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_invoice_schema(db, cursor)
        search = request.args.get("search", "")
        line_cost = sold_line_cost_sql("d")
        paid_ratio = paid_ratio_sql("i", "pay")

        query = f"""
            SELECT
                i.InvoiceID,
                i.[Date] AS InvoiceDate,
                i.TotalAmount,
                COALESCE(i.PaymentStatus, 'Unpaid') AS PaymentStatus,
                COALESCE(pay.PaidAmount, 0) AS PaidAmount,
                GREATEST(COALESCE(i.TotalAmount, 0) - COALESCE(pay.PaidAmount, 0), 0) AS RemainingAmount,
                c.CustomerName,
                ISNULL(
                    SUM(
                        (COALESCE(d.Qty, 0) * COALESCE(d.Rate, 0) - ({line_cost}))
                        * ({paid_ratio})
                    ),
                    0
                ) AS Profit
            FROM Invoices i
            JOIN Customers c ON i.CustomerID = c.CustomerID
            LEFT JOIN InvoiceDetails d ON i.InvoiceID = d.InvoiceID
            LEFT JOIN Item it ON d.ItemID = it.ItemID
            {purchase_unit_cost_join("d")}
            {payments_join_sql("i")}
            WHERE {owner_sql("i")}
        """
        params = []

        if search:
            query += " AND (CAST(i.InvoiceID AS VARCHAR(20)) LIKE ? OR c.CustomerName LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        query += """
            GROUP BY
                i.InvoiceID, i.[Date], i.TotalAmount, i.PaymentStatus,
                pay.PaidAmount, c.CustomerName
            ORDER BY i.InvoiceID DESC
        """

        cursor.execute(query, params or ())
        invoices = cursor.fetchall()

        return render_template("invoices/list.html", invoices=invoices, search=search)

    except Exception as e:
        flash(f"Error loading invoices: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))

    finally:
        cursor.close()


@invoices_bp.route("/<int:id>/pdf")
@login_required
def invoice_pdf(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_invoice_schema(db, cursor)
        invoice = _load_invoice_record(cursor, id)

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        details = _load_invoice_pdf_details(cursor, id)
        return _send_invoice_pdf(
            invoice,
            details,
            id,
            as_attachment=request.args.get("download") == "1",
        )

    except Exception as e:
        flash(f"Error generating invoice PDF: {str(e)}", "danger")
        return redirect(url_for("invoices.list_invoices"))

    finally:
        cursor.close()


@invoices_bp.route("/<int:id>/pdf/share")
def invoice_pdf_share(id):
    """Login-free PDF download — served to anyone with the direct URL."""
    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        _ensure_invoice_schema(db, cursor)
        invoice = _load_invoice_record(cursor, id)
        if not invoice:
            abort(404)
        details = _load_invoice_pdf_details(cursor, id)
        return _send_invoice_pdf(invoice, details, id, as_attachment=True)
    finally:
        cursor.close()


@invoices_bp.route("/<int:id>/whatsapp", methods=["GET", "POST"])
@login_required
def invoice_whatsapp(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()

    try:
        _ensure_invoice_schema(db, cursor)
        invoice = _load_invoice_record(cursor, id)
        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        entered_number = (
            request.form.get("whatsapp_number") if request.method == "POST" else (invoice.ContactNo or "")
        )
        share_path = url_for("invoices.invoice_pdf_share", id=id)

        if request.method == "POST":
            phone = clean_phone(entered_number, "whatsapp_number", errors, required=True, max_len=20)
            if errors.valid and not whatsapp_url(phone):
                errors.add("whatsapp_number", "Enter a valid WhatsApp mobile number.")
            if errors.valid:
                if wa_api_configured():
                    # Send actual PDF file via WhatsApp Business API
                    details_for_pdf = _load_invoice_pdf_details(cursor, id)
                    pdf_buf = _build_invoice_pdf(invoice, details_for_pdf)
                    pdf_bytes = pdf_buf.read()
                    caption = (
                        f"Invoice #{invoice.InvoiceID} — {invoice.CustomerName}\n"
                        f"Amount: Rs {float(invoice.TotalAmount or 0):,.2f}"
                    )
                    _result, err = wa_send_file(
                        phone, pdf_bytes, "application/pdf",
                        f"invoice_{invoice.InvoiceID}.pdf", caption,
                    )
                    if err:
                        flash(f"WhatsApp send failed: {err}", "danger")
                    else:
                        flash("Invoice PDF sent to WhatsApp.", "success")
                        return redirect(url_for("invoices.list_invoices"))
                else:
                    # Fallback: open wa.me with a download link in the text
                    pdf_link = public_file_url(share_path)
                    message = (
                        f"Invoice #{invoice.InvoiceID} — {invoice.CustomerName}\n"
                        f"Amount: Rs {float(invoice.TotalAmount or 0):,.2f}\n"
                        f"Download PDF: {pdf_link}"
                    )
                    return redirect(whatsapp_url(phone, message))
            else:
                flash(errors.first(), "danger")

        return render_template(
            "invoices/whatsapp.html",
            invoice=invoice,
            share_path=share_path,
            wa_api=wa_api_configured(),
            form_data={"whatsapp_number": entered_number or ""},
            errors=errors.errors,
        )

    except Exception as e:
        flash(f"Error opening WhatsApp invoice: {str(e)}", "danger")
        return redirect(url_for("invoices.list_invoices"))

    finally:
        cursor.close()


@invoices_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {}
    invoice_lines = _default_invoice_lines()

    try:
        _ensure_invoice_schema(db, cursor)

        cursor.execute(
            f"""
            SELECT
                InvoiceID,
                CustomerID,
                [Date] AS InvoiceDate,
                TotalAmount,
                COALESCE(PaymentStatus, 'Unpaid') AS PaymentStatus,
                COALESCE(PreviousBalance, 0) AS PreviousBalance
            FROM Invoices
            WHERE InvoiceID = ? AND {owner_sql()}
            """,
            (id,),
        )
        invoice = cursor.fetchone()

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        extra_stock_by_item, extra_item_ids = _existing_invoice_stock(cursor, id)
        customers, items = _load_invoice_form_data(
            cursor,
            extra_item_ids=extra_item_ids,
            exclude_invoice_id=id,
        )

        if request.method == "POST":
            form_data = request.form.to_dict()
            invoice_lines = _invoice_lines_from_form(request.form)
            action = request.form.get("action", "update_invoice")

            if action == "save_previous_balance":
                customer_id = clean_select_id(request.form.get("customer_id"), "customer_id", errors, label="Customer")
                previous_balance = clean_positive_decimal(
                    request.form.get("previous_balance"),
                    "previous_balance",
                    errors,
                    min_val=0,
                    label="Previous balance",
                )

                if not errors.valid:
                    flash(errors.first(), "danger")
                    return render_template(
                        "invoices/form.html",
                        customers=customers,
                        items=items,
                        errors=errors.errors,
                        form_data=form_data,
                        invoice_lines=invoice_lines,
                        is_edit=True,
                        invoice_id=id,
                    )

                cursor.execute(
                    f"UPDATE Customers SET PreviousBalance = ? WHERE CustomerID = ? AND {owner_sql()}",
                    (previous_balance, customer_id),
                )
                db.commit()
                flash("Previous balance updated successfully.", "success")

                customers, items = _load_invoice_form_data(
                    cursor,
                    extra_item_ids=extra_item_ids,
                    exclude_invoice_id=id,
                )
                return render_template(
                    "invoices/form.html",
                    customers=customers,
                    items=items,
                    errors=errors.errors,
                    form_data=form_data,
                    invoice_lines=invoice_lines,
                    is_edit=True,
                    invoice_id=id,
                )

            data = _validate_invoice_header(request.form, errors)
            invoice_lines, valid_lines = _validate_invoice_lines(
                request.form,
                cursor,
                errors,
                extra_stock_by_item=extra_stock_by_item,
            )

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "invoices/form.html",
                    customers=customers,
                    items=items,
                    errors=errors.errors,
                    form_data=form_data,
                    invoice_lines=invoice_lines,
                    is_edit=True,
                    invoice_id=id,
                )

            total = sum(line["total"] for line in valid_lines)
            invoice_datetime = datetime.combine(
                datetime.strptime(data["invoice_date"], "%Y-%m-%d").date(),
                _invoice_time(invoice.InvoiceDate),
            )

            cursor.execute(
                f"UPDATE Customers SET PreviousBalance = ? WHERE CustomerID = ? AND {owner_sql()}",
                (data["previous_balance"], data["customer_id"]),
            )

            for item_id, qty in extra_stock_by_item.items():
                cursor.execute(
                    f"""
                    UPDATE Item
                    SET Qty = Qty + ?
                    WHERE ItemID = ? AND {owner_sql()}
                    """,
                    (qty, item_id),
                )

            cursor.execute("DELETE FROM InvoiceDetails WHERE InvoiceID = ?", (id,))
            cursor.execute(
                f"""
                UPDATE Invoices
                SET CustomerID = ?, [Date] = ?, TotalAmount = ?, PreviousBalance = ?
                WHERE InvoiceID = ? AND {owner_sql()}
                """,
                (
                    data["customer_id"],
                    invoice_datetime,
                    total,
                    data["previous_balance"],
                    id,
                ),
            )

            for line in valid_lines:
                cursor.execute(
                    """
                    INSERT INTO InvoiceDetails (InvoiceID, ItemID, Rate, Qty, Particulars)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (id, line["item_id"], line["rate"], line["quantity"], line["item_name"]),
                )
                cursor.execute(
                    f"""
                    UPDATE Item
                    SET Qty = Qty - ?
                    WHERE ItemID = ? AND {owner_sql()}
                    """,
                    (line["quantity"], line["item_id"]),
                )

            refresh_invoice_settlement(cursor, id)
            db.commit()
            flash("Invoice updated successfully.", "success")
            return redirect(url_for("invoices.invoice_pdf", id=id))

        form_data = {
            "invoice_date": _invoice_date_iso(invoice.InvoiceDate),
            "customer_id": str(invoice.CustomerID),
            "previous_balance": f"{float(invoice.PreviousBalance or 0):.2f}",
        }
        invoice_lines = _invoice_lines_from_details(cursor, id)

        return render_template(
            "invoices/form.html",
            customers=customers,
            items=items,
            errors=errors.errors,
            form_data=form_data,
            invoice_lines=invoice_lines,
            is_edit=True,
            invoice_id=id,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error updating invoice: {str(e)}", "danger")
        return redirect(url_for("invoices.list_invoices"))

    finally:
        cursor.close()


@invoices_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_invoice_schema(db, cursor)
        cursor.execute(
            """
            SELECT ItemID, Qty
            FROM InvoiceDetails
            WHERE InvoiceID = ?
            """,
            (id,),
        )
        details = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT
                i.InvoiceID,
                i.CustomerID,
                COALESCE(i.TotalAmount, 0) AS TotalAmount
            FROM Invoices i
            WHERE i.InvoiceID = ? AND {owner_sql("i")}
            """,
            (id,),
        )
        invoice = cursor.fetchone()

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        paid_amount = invoice_paid_total(cursor, id)
        if paid_amount > 0:
            cursor.execute(
                f"""
                UPDATE Customers
                SET PreviousBalance = COALESCE(PreviousBalance, 0) + ?
                WHERE CustomerID = ? AND {owner_sql()}
                """,
                (paid_amount, int(invoice.CustomerID)),
            )

        for detail in details:
            cursor.execute(
                f"""
                UPDATE Item
                SET Qty = Qty + ?
                WHERE ItemID = ? AND {owner_sql()}
                """,
                (detail.Qty, detail.ItemID),
            )

        cursor.execute("DELETE FROM StockHistory WHERE InvoiceID = ?", (id,))
        cursor.execute("DELETE FROM InvoiceDetails WHERE InvoiceID = ?", (id,))
        cursor.execute(f"DELETE FROM Invoices WHERE InvoiceID = ? AND {owner_sql()}", (id,))

        db.commit()
        flash("Invoice deleted successfully. Stock quantities were restored.", "success")

    except Exception as e:
        db.rollback()
        flash(f"Error deleting invoice: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(url_for("invoices.list_invoices"))


@invoices_bp.route("/<int:id>/status", methods=["POST"])
@login_required
def update_invoice_status(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    redirect_to = request.form.get("next") or url_for("invoices.list_invoices")

    try:
        _ensure_invoice_schema(db, cursor)
        target_status = (request.form.get("status") or "").strip()

        if target_status not in {"Paid", "Unpaid"}:
            flash("Invalid payment status.", "danger")
            return redirect(redirect_to)

        invoice = _load_invoice_record(cursor, id)
        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        current_status = (invoice.PaymentStatus or "Unpaid").strip()
        if current_status == target_status:
            flash(f"Invoice #{id} is already marked as {target_status}.", "info")
            return redirect(redirect_to)

        if target_status == "Paid":
            pay_invoice_remaining(
                cursor,
                invoice,
                payment_method=request.form.get("payment_method") or "Cash",
            )
            flash(f"Invoice #{id} marked as Paid.", "success")
        else:
            clear_invoice_payments(cursor, invoice)
            flash(f"Invoice #{id} marked as Unpaid. Payments were cleared.", "success")

        db.commit()

    except ValueError as e:
        db.rollback()
        flash(str(e), "danger")
    except Exception as e:
        db.rollback()
        flash(f"Error updating invoice status: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(redirect_to)


@invoices_bp.route("/<int:id>/payments", methods=["GET", "POST"])
@login_required
def invoice_payments(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {
        "payment_date": date.today().isoformat(),
        "amount": "",
        "notes": "",
        "payment_method": "Cash",
    }

    try:
        _ensure_invoice_schema(db, cursor)
        invoice = _load_invoice_record(cursor, id)
        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        paid_amount = invoice_paid_total(cursor, id)
        remaining = remaining_due(invoice.TotalAmount, paid_amount)

        if request.method == "POST":
            form_data = request.form.to_dict()
            payment_date_value = clean_date(
                request.form.get("payment_date") or date.today().isoformat(),
                "payment_date",
                errors,
                label="Payment date",
            )
            amount = clean_positive_decimal(
                request.form.get("amount"),
                "amount",
                errors,
                min_val=0.01,
                label="Amount received",
            )
            notes = clean_optional_string(
                request.form.get("notes"),
                "notes",
                errors,
                max_len=255,
                label="Notes",
            )
            payment_method = request.form.get("payment_method") or "Cash"

            if not errors.valid:
                flash(errors.first(), "danger")
            else:
                payment_datetime = datetime.combine(
                    datetime.strptime(payment_date_value, "%Y-%m-%d").date(),
                    datetime.now().time(),
                )
                result = add_invoice_payment(
                    cursor, invoice, amount, payment_datetime, notes, payment_method
                )
                db.commit()
                flash(
                    f"Received Rs {amount:,.2f} in {normalize_payment_method(payment_method)}. Invoice #{id} is now {result['status']}.",
                    "success",
                )
                return redirect(url_for("invoices.invoice_payments", id=id))

        payments = list_invoice_payments(cursor, id)
        return render_template(
            "invoices/payments.html",
            invoice=invoice,
            payments=payments,
            paid_amount=paid_amount,
            remaining=remaining,
            errors=errors.errors,
            form_data=form_data,
        )

    except ValueError as e:
        db.rollback()
        flash(str(e), "danger")
        return redirect(url_for("invoices.invoice_payments", id=id))
    except Exception as e:
        db.rollback()
        flash(f"Error loading payments: {str(e)}", "danger")
        return redirect(url_for("invoices.list_invoices"))

    finally:
        cursor.close()


@invoices_bp.route("/<int:id>/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
def remove_invoice_payment(id, payment_id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_invoice_schema(db, cursor)
        invoice = _load_invoice_record(cursor, id)
        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        delete_invoice_payment(cursor, invoice, payment_id)
        db.commit()
        flash("Payment removed.", "success")

    except ValueError as e:
        db.rollback()
        flash(str(e), "danger")
    except Exception as e:
        db.rollback()
        flash(f"Error removing payment: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(url_for("invoices.invoice_payments", id=id))
