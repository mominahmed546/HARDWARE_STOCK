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


def _validate_invoice_header(form, errors):
    return {
        "invoice_date": clean_date(form.get("invoice_date"), "invoice_date", errors, label="Invoice date"),
        "customer_id": clean_select_id(form.get("customer_id"), "customer_id", errors, label="Customer"),
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
        SELECT CustomerID, CustomerName, COALESCE(PreviousBalance, 0) AS PreviousBalance
        FROM Customers
        ORDER BY CustomerName
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


def _build_invoice_pdf(invoice, details):
    commands = []

    def set_color(values, operator):
        return f"{values[0]} {values[1]} {values[2]} {operator}"

    def text(x, y, value, size=10, font="F1", text_color=(0, 0, 0)):
        commands.append(set_color(text_color, "rg"))
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2, width=1, stroke_color=(0, 0, 0)):
        commands.append(set_color(stroke_color, "RG"))
        commands.append(f"{width} w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y, width, height, fill_color=None, stroke_color=(0, 0, 0)):
        if fill_color:
            commands.append(set_color(fill_color, "rg"))
            commands.append(f"{x} {y} {width} {height} re f")
        commands.append(set_color(stroke_color, "RG"))
        commands.append(f"{x} {y} {width} {height} re S")

    def money(value):
        return f"Rs {float(value or 0):.2f}"

    navy = (0.06, 0.09, 0.16)
    blue = (0.15, 0.39, 0.92)
    light = (0.95, 0.97, 1)
    border = (0.78, 0.84, 0.9)
    muted = (0.39, 0.45, 0.55)

    rect(40, 720, 532, 42, fill_color=navy, stroke_color=navy)
    text(55, 746, "EUROGLASS", 22, "F2", (1, 1, 1))
    text(55, 728, "Hardware Stock Management", 9, "F1", (0.85, 0.9, 1))
    text(442, 742, "SALES INVOICE", 16, "F2", (1, 1, 1))

    rect(40, 640, 250, 55, fill_color=light, stroke_color=border)
    text(55, 675, "Bill To", 10, "F2", blue)
    text(55, 655, invoice.CustomerName, 12, "F2")

    rect(322, 640, 250, 55, fill_color=light, stroke_color=border)
    text(337, 675, f"Invoice No: {invoice.InvoiceID}", 10, "F2")
    text(337, 655, f"Date: {_format_date_dmy(invoice.InvoiceDate)}", 10, "F1")

    table_x = 40
    table_y = 585
    table_w = 532
    row_h = 28
    columns = [
        ("#", 40),
        ("Item", 215),
        ("Qty", 60),
        ("Rate", 100),
        ("Total", 117),
    ]

    rect(table_x, table_y, table_w, row_h, fill_color=blue, stroke_color=blue)
    current_x = table_x
    for header, width in columns:
        text(current_x + 8, table_y + 10, header, 10, "F2", (1, 1, 1))
        current_x += width

    y = table_y - row_h
    shown_details = list(details)[:12]

    for index, detail in enumerate(shown_details, start=1):
        fill = (1, 1, 1) if index % 2 else (0.98, 0.99, 1)
        rect(table_x, y, table_w, row_h, fill_color=fill, stroke_color=border)

        values = [
            str(index),
            detail.Particulars,
            str(detail.Qty),
            money(detail.Rate),
            money(detail.TotalAmount),
        ]

        current_x = table_x
        for value, (_, width) in zip(values, columns):
            text(current_x + 8, y + 10, value, 9)
            current_x += width

        current_x = table_x
        for _, width in columns[:-1]:
            current_x += width
            line(current_x, y, current_x, y + row_h, 0.5, border)

        y -= row_h

    if len(details) > len(shown_details):
        text(table_x, y + 10, f"{len(details) - len(shown_details)} more item(s) not shown.", 9, "F1", muted)
        y -= row_h

    total_y = max(y - 35, 90)
    rect(360, total_y, 212, 38, fill_color=light, stroke_color=border)
    text(375, total_y + 15, "Grand Total", 12, "F2")
    text(485, total_y + 15, money(invoice.TotalAmount), 12, "F2", blue)

    line(40, 65, 572, 65, 0.75, border)
    text(40, 45, "Thank you for your business.", 9, "F1", muted)
    text(395, 45, "Generated by Euroglass", 9, "F1", muted)

    content = "\n".join(commands).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
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
                INSERT INTO Invoices (CustomerID, [Date], TotalAmount)
                OUTPUT INSERTED.InvoiceID
                VALUES (?, ?, ?)
                """,
                (data["customer_id"], data["invoice_date"], total),
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
        search = request.args.get("search", "")

        query = """
            SELECT
                i.InvoiceID,
                i.[Date] AS InvoiceDate,
                i.TotalAmount,
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
            GROUP BY i.InvoiceID, i.[Date], i.TotalAmount, c.CustomerName
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
        cursor.execute(
            """
            SELECT i.InvoiceID, i.[Date] AS InvoiceDate, i.TotalAmount, c.CustomerName
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
