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
    details = list(details)

    receipt_width = 226  # ~80mm thermal paper
    receipt_height = max(430, 230 + (len(details) * 22))

    def text(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def money(value):
        return f"{float(value or 0):,.2f}"

    x_left = 12
    x_right = receipt_width - 12
    y = receipt_height - 24

    text(x_left, y, "EUROGLASS", 12, "F2")
    y -= 14
    text(x_left, y, "Hardware Stock Management", 8, "F1")
    y -= 12
    line(x_left, y, x_right, y)
    y -= 12

    text(x_left, y, f"Invoice #: {invoice.InvoiceID}", 8, "F1")
    y -= 11
    text(x_left, y, f"Date: {_format_date_dmy(invoice.InvoiceDate)}", 8, "F1")
    y -= 11
    text(x_left, y, f"Customer: {invoice.CustomerName}", 8, "F1")
    y -= 10
    line(x_left, y, x_right, y)
    y -= 12

    text(x_left, y, "ITEM", 8, "F2")
    text(x_right - 26, y, "TOTAL", 8, "F2")
    y -= 8
    line(x_left, y, x_right, y)
    y -= 11

    if not details:
        text(x_left, y, "No items", 8, "F1")
        y -= 12
    else:
        for detail in details:
            item_name = str(detail.Particulars or "Item")
            if len(item_name) > 30:
                item_name = item_name[:27] + "..."
            text(x_left, y, item_name, 8, "F1")
            y -= 10

            qty_rate_text = f"{detail.Qty} x Rs {money(detail.Rate)}"
            line_total_text = f"Rs {money(detail.TotalAmount)}"
            text(x_left + 4, y, qty_rate_text, 8, "F1")
            text(x_right - (6 * len(line_total_text)), y, line_total_text, 8, "F1")
            y -= 13

    line(x_left, y, x_right, y)
    y -= 14
    text(x_left, y, "Grand Total", 10, "F2")
    total_text = f"Rs {money(invoice.TotalAmount)}"
    text(x_right - (6.2 * len(total_text)), y, total_text, 10, "F2")
    y -= 16

    line(x_left, y, x_right, y)
    y -= 12
    text(x_left, y, "Payment Status: __________________", 8, "F1")
    y -= 12
    text(x_left, y, "Thank you for your business.", 8, "F1")
    y -= 10
    text(x_left, y, "Generated by Euroglass", 7, "F1")

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
                INSERT INTO Invoices (CustomerID, [Date], TotalAmount, PaymentStatus)
                OUTPUT INSERTED.InvoiceID
                VALUES (?, ?, ?, ?)
                """,
                (data["customer_id"], data["invoice_date"], total, "Unpaid"),
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
