from datetime import date, datetime
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.validators import ValidationErrors, clean_date, clean_positive_decimal, clean_positive_int, clean_select_id

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


def _validate_invoice_lines(form, cursor, errors):
    lines = _invoice_lines_from_form(form)
    valid_lines = []
    requested_qty_by_item = {}

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
            "SELECT ItemID, ItemName, Qty FROM Item WHERE ItemID = ?",
            (item_value,),
        )
        item = cursor.fetchone()

        if not item:
            errors.add("item_id[]", "Selected item was not found.")
            break

        requested_qty_by_item[item_value] = requested_qty_by_item.get(item_value, 0) + quantity_value
        if requested_qty_by_item[item_value] > item.Qty:
            errors.add("quantity[]", f"Only {item.Qty} item(s) are available for {item.ItemName}.")
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


def _load_invoice_form_data(cursor):
    cursor.execute(
        """
        SELECT
            c.CustomerID,
            c.CustomerName,
            COALESCE(c.PreviousBalance, 0)
                + COALESCE(unpaid.UnpaidTotal, 0) AS PreviousBalance
        FROM Customers c
        LEFT JOIN (
            SELECT CustomerID, SUM(TotalAmount) AS UnpaidTotal
            FROM Invoices
            WHERE COALESCE(PaymentStatus, 'Unpaid') = 'Unpaid'
            GROUP BY CustomerID
        ) unpaid ON unpaid.CustomerID = c.CustomerID
        ORDER BY c.CustomerName
        """
    )
    customers = cursor.fetchall()

    cursor.execute(
        """
        SELECT ItemID, ItemName, SaleRate, Qty
        FROM Item
        WHERE Qty > 0
        ORDER BY ItemName
        """
    )
    items = cursor.fetchall()

    return customers, items


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

    receipt_width = 310
    line_h = 10
    max_name_chars = 20

    def _row_height(item_name):
        wrapped = _wrap_text(item_name, max_name_chars)
        return max(18, 6 + len(wrapped) * line_h)

    extra_h = sum(_row_height(str(d.Particulars or "Item")) for d in details)
    receipt_height = max(520, 260 + extra_h)

    def text(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y, width, height):
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
    text(x_left + 58, y, str(invoice.InvoiceID), 10, "F2")
    text(x_left + 112, y, "DATED", 10, "F2")
    text(x_left + 158, y, _format_datetime_for_invoice(invoice.InvoiceDate), 8, "F1")
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
    col_product_right = table_x + 130
    col_qty_right = col_product_right + 30
    col_rate_right = col_qty_right + 62
    col_total_right = table_x + table_w

    header_y = y
    row_h = 18
    rect(table_x, header_y - row_h + 4, table_w, row_h)
    line(col_product_right, header_y - row_h + 4, col_product_right, header_y + 4)
    line(col_qty_right, header_y - row_h + 4, col_qty_right, header_y + 4)
    line(col_rate_right, header_y - row_h + 4, col_rate_right, header_y + 4)
    text(table_x + 4, header_y - 8, "PRODUCT NAME", 9, "F2")
    text(col_product_right + 6, header_y - 8, "QTY", 9, "F2")
    text(col_qty_right + 6, header_y - 8, "RATE", 9, "F2")
    text(col_rate_right + 6, header_y - 8, "TOTAL", 9, "F2")
    y = header_y - row_h - 2

    if not details:
        rect(table_x, y - row_h + 4, table_w, row_h)
        text(table_x + 4, y - 8, "No items", 8, "F1")
        y -= row_h
    else:
        for detail in details:
            item_name = str(detail.Particulars or "Item")
            name_lines = _wrap_text(item_name, max_name_chars)
            dyn_row_h = max(18, 6 + len(name_lines) * line_h)

            rect(table_x, y - dyn_row_h + 4, table_w, dyn_row_h)
            line(col_product_right, y - dyn_row_h + 4, col_product_right, y + 4)
            line(col_qty_right, y - dyn_row_h + 4, col_qty_right, y + 4)
            line(col_rate_right, y - dyn_row_h + 4, col_rate_right, y + 4)

            text_y = y - 8
            for name_line in name_lines:
                text(table_x + 4, text_y, name_line, 8, "F1")
                text_y -= line_h

            mid_y = y - (dyn_row_h / 2) + 2
            text_right(col_qty_right - 4, mid_y, str(detail.Qty), 8, "F1")
            text_right(col_rate_right - 4, mid_y, money(detail.Rate), 8, "F1")
            text_right(col_total_right - 4, mid_y, money(detail.TotalAmount), 8, "F1")
            y -= dyn_row_h

    y -= 12
    items_count = len(details)
    total_amount = float(invoice.TotalAmount or 0)
    cash_received = float(getattr(invoice, "CashReceived", 0) or 0)
    net_balance = previous_balance + total_amount - cash_received

    text(x_left, y, f"Items    {items_count}", 11, "F2")
    text_right(x_right, y, f"TOTAL: {money(total_amount)}", 12, "F2")
    y -= 20
    text_right(x_right, y, f"Previous Balance: {money(previous_balance)}", 11, "F2")
    y -= 16
    text_right(x_right, y, f"Cash Received: {money(cash_received)}", 11, "F2")
    y -= 16
    text_right(x_right, y, f"Net Balance: {money(net_balance)}", 12, "F2")

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
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_payment_status_column(db, cursor)
        _ensure_invoice_previous_balance_column(db, cursor)
        _ensure_invoice_date_is_timestamp(db, cursor)
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
                    "UPDATE Customers SET PreviousBalance = ? WHERE CustomerID = ?",
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

            cursor.execute(
                """
                INSERT INTO Invoices (CustomerID, [Date], TotalAmount, PaymentStatus, PreviousBalance)
                OUTPUT INSERTED.InvoiceID
                VALUES (?, NOW(), ?, ?, ?)
                """,
                (data["customer_id"], total, "Unpaid", data["previous_balance"]),
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
                    """
                    UPDATE Item
                    SET Qty = Qty - ?
                    WHERE ItemID = ?
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
        _ensure_invoice_payment_status_column(db, cursor)
        search = request.args.get("search", "")

        query = """
            SELECT
                i.InvoiceID,
                i.[Date] AS InvoiceDate,
                i.TotalAmount,
                COALESCE(i.PaymentStatus, 'Unpaid') AS PaymentStatus,
                c.CustomerName,
                ISNULL(SUM((d.Rate - ISNULL(it.PurchaseRate, 0)) * d.Qty), 0) AS Profit
            FROM Invoices i
            JOIN Customers c ON i.CustomerID = c.CustomerID
            LEFT JOIN InvoiceDetails d ON i.InvoiceID = d.InvoiceID
            LEFT JOIN Item it ON d.ItemID = it.ItemID
            WHERE 1=1
        """
        params = []

        if search:
            query += " AND (CAST(i.InvoiceID AS VARCHAR(20)) LIKE ? OR c.CustomerName LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])

        query += """
            GROUP BY i.InvoiceID, i.[Date], i.TotalAmount, i.PaymentStatus, c.CustomerName
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
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_previous_balance_column(db, cursor)
        _ensure_invoice_date_is_timestamp(db, cursor)
        cursor.execute(
            """
            SELECT
                i.InvoiceID,
                i.[Date] AS InvoiceDate,
                i.TotalAmount,
                c.CustomerName,
                c.ContactNo,
                COALESCE(i.PreviousBalance, 0) AS PreviousBalance,
                COALESCE((
                    SELECT SUM(i2.TotalAmount)
                    FROM Invoices i2
                    WHERE i2.CustomerID = i.CustomerID
                      AND COALESCE(i2.PaymentStatus, 'Unpaid') = 'Paid'
                ), 0) AS CashReceived
            FROM Invoices i
            JOIN Customers c ON i.CustomerID = c.CustomerID
            WHERE i.InvoiceID = ?
            """,
            (id,),
        )
        invoice = cursor.fetchone()

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        cursor.execute(
            """
            SELECT Particulars, Qty, Rate, (Qty * Rate) AS TotalAmount
            FROM InvoiceDetails
            WHERE InvoiceID = ?
            """,
            (id,),
        )
        details = cursor.fetchall()

        pdf = _build_invoice_pdf(invoice, details)
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"invoice_{id}.pdf",
        )

    except Exception as e:
        flash(f"Error generating invoice PDF: {str(e)}", "danger")
        return redirect(url_for("invoices.list_invoices"))

    finally:
        cursor.close()


@invoices_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_invoice(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            SELECT ItemID, Qty
            FROM InvoiceDetails
            WHERE InvoiceID = ?
            """,
            (id,),
        )
        details = cursor.fetchall()

        cursor.execute("SELECT InvoiceID FROM Invoices WHERE InvoiceID = ?", (id,))
        invoice = cursor.fetchone()

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        for detail in details:
            cursor.execute(
                """
                UPDATE Item
                SET Qty = Qty + ?
                WHERE ItemID = ?
                """,
                (detail.Qty, detail.ItemID),
            )

        cursor.execute("DELETE FROM StockHistory WHERE InvoiceID = ?", (id,))
        cursor.execute("DELETE FROM InvoiceDetails WHERE InvoiceID = ?", (id,))
        cursor.execute("DELETE FROM Invoices WHERE InvoiceID = ?", (id,))

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

    try:
        _ensure_invoice_payment_status_column(db, cursor)
        target_status = (request.form.get("status") or "").strip()

        if target_status not in {"Paid", "Unpaid"}:
            flash("Invalid payment status.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        cursor.execute(
            "SELECT InvoiceID FROM Invoices WHERE InvoiceID = ?",
            (id,),
        )
        invoice = cursor.fetchone()

        if not invoice:
            flash("Invoice not found.", "danger")
            return redirect(url_for("invoices.list_invoices"))

        cursor.execute(
            "UPDATE Invoices SET PaymentStatus = ? WHERE InvoiceID = ?",
            (target_status, id),
        )
        db.commit()
        flash(f"Invoice #{id} marked as {target_status}.", "success")

    except Exception as e:
        db.rollback()
        flash(f"Error updating invoice status: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(url_for("invoices.list_invoices"))
