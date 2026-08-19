from datetime import datetime
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.tenancy import owner_sql
from app.payments import ensure_invoice_payments_table, payments_join_sql

ledger_bp = Blueprint("ledger", __name__, url_prefix="/ledger")


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


def _event_sort_date(value):
    if value is None:
        return datetime.min
    if getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _balance_side(balance):
    return "Dr" if float(balance or 0) >= 0 else "Cr"


def _build_ledger_entries(opening_balance, invoices, details_by_invoice, payments_by_invoice):
    entries = []
    balance = float(opening_balance or 0)
    opening_debit = balance if balance > 0 else 0.0
    opening_credit = abs(balance) if balance < 0 else 0.0

    entries.append(
        {
            "date": None,
            "vch_no": "",
            "vch_type": "",
            "particulars": "Opening Balance",
            "debit": opening_debit,
            "credit": opening_credit,
            "balance": balance,
            "balance_side": _balance_side(balance),
            "invoice_id": None,
            "is_opening": True,
        }
    )

    total_debit = opening_debit
    total_credit = opening_credit
    events = []

    for invoice in invoices:
        events.append(
            {
                "kind": "invoice",
                "date": invoice.InvoiceDate,
                "invoice": invoice,
                "sort": 0,
            }
        )
        for payment in payments_by_invoice.get(invoice.InvoiceID, []):
            events.append(
                {
                    "kind": "payment",
                    "date": payment.PaymentDate,
                    "invoice": invoice,
                    "payment": payment,
                    "sort": 1,
                }
            )

    events.sort(
        key=lambda event: (
            _event_sort_date(event["date"]),
            event["sort"],
            int(event["invoice"].InvoiceID),
            int(getattr(event.get("payment"), "PaymentID", 0) or 0),
        )
    )

    for event in events:
        invoice = event["invoice"]
        invoice_id = invoice.InvoiceID
        if event["kind"] == "invoice":
            amount = float(invoice.TotalAmount or 0)
            item_names = [
                str(row.Particulars or "Item").strip()
                for row in details_by_invoice.get(invoice_id, [])
                if str(row.Particulars or "").strip()
            ]
            if item_names:
                preview = ", ".join(item_names[:3])
                if len(item_names) > 3:
                    preview += f" and {len(item_names) - 3} more"
                sale_particulars = f"To Sales Invoice No. {invoice_id} — {preview}"
            else:
                sale_particulars = f"To Sales Invoice No. {invoice_id}"

            balance += amount
            total_debit += amount
            entries.append(
                {
                    "date": invoice.InvoiceDate,
                    "vch_no": str(invoice_id),
                    "vch_type": "Invoice",
                    "particulars": sale_particulars,
                    "debit": amount,
                    "credit": 0.0,
                    "balance": balance,
                    "balance_side": _balance_side(balance),
                    "invoice_id": invoice_id,
                    "is_opening": False,
                }
            )
            continue

        payment = event["payment"]
        amount = float(payment.Amount or 0)
        notes = str(payment.Notes or "").strip()
        method = str(getattr(payment, "PaymentMethod", "Cash") or "Cash")
        if method == "Bank":
            source = "Bank"
        elif method == "In-Kind":
            source = "In-Kind items"
        else:
            source = "Cash"
        particulars = f"By {source} received against Invoice No. {invoice_id}"
        if notes and notes != "Backfilled from Paid status" and notes != "Marked paid":
            particulars += f" — {notes}"

        balance -= amount
        total_credit += amount
        entries.append(
            {
                "date": payment.PaymentDate,
                "vch_no": str(invoice_id),
                "vch_type": "Receipt",
                "particulars": particulars,
                "debit": 0.0,
                "credit": amount,
                "balance": balance,
                "balance_side": _balance_side(balance),
                "invoice_id": invoice_id,
                "is_opening": False,
            }
        )

    return entries, total_debit, total_credit, balance


def _load_customer_ledger(cursor, customer_id):
    cursor.execute(
        f"""
        SELECT
            CustomerID,
            CustomerName,
            ContactNo,
            COALESCE(PreviousBalance, 0) AS PreviousBalance
        FROM Customers
        WHERE CustomerID = ? AND {owner_sql()}
        """,
        (customer_id,),
    )
    customer = cursor.fetchone()
    if not customer:
        return None

    cursor.execute(
        f"""
        SELECT
            i.InvoiceID,
            i.[Date] AS InvoiceDate,
            i.TotalAmount,
            COALESCE(i.PaymentStatus, 'Unpaid') AS PaymentStatus
        FROM Invoices i
        WHERE i.CustomerID = ? AND {owner_sql("i")}
        ORDER BY i.[Date] ASC, i.InvoiceID ASC
        """,
        (customer_id,),
    )
    invoices = cursor.fetchall()

    details_by_invoice = {}
    if invoices:
        invoice_ids = [int(row.InvoiceID) for row in invoices]
        placeholders = ",".join("?" * len(invoice_ids))
        cursor.execute(
            f"""
            SELECT InvoiceID, Particulars, Qty, Rate
            FROM InvoiceDetails
            WHERE InvoiceID IN ({placeholders})
            ORDER BY InvoiceID, DetailID
            """,
            invoice_ids,
        )
        for row in cursor.fetchall():
            details_by_invoice.setdefault(row.InvoiceID, []).append(row)

    payments_by_invoice = {}
    if invoices:
        invoice_ids = [int(row.InvoiceID) for row in invoices]
        placeholders = ",".join("?" * len(invoice_ids))
        cursor.execute(
            f"""
            SELECT PaymentID, InvoiceID, Amount, PaymentDate, Notes,
                   COALESCE(PaymentMethod, 'Cash') AS PaymentMethod
            FROM InvoicePayments
            WHERE InvoiceID IN ({placeholders})
            ORDER BY PaymentDate, PaymentID
            """,
            invoice_ids,
        )
        for row in cursor.fetchall():
            payments_by_invoice.setdefault(row.InvoiceID, []).append(row)

    opening_balance = float(customer.PreviousBalance or 0)
    entries, total_debit, total_credit, closing_balance = _build_ledger_entries(
        opening_balance, invoices, details_by_invoice, payments_by_invoice
    )
    total_invoiced = sum(float(invoice.TotalAmount or 0) for invoice in invoices)
    total_paid = sum(
        float(payment.Amount or 0)
        for payments in payments_by_invoice.values()
        for payment in payments
    )

    return {
        "customer": customer,
        "invoices": invoices,
        "entries": entries,
        "opening_balance": opening_balance,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing_balance,
        "outstanding": opening_balance + total_invoiced - total_paid,
    }


def _build_ledger_pdf(data):
    customer = data["customer"]
    entries = data["entries"]
    commands = []
    page_width = 612
    page_height = 792
    left = 36
    right = page_width - 36
    top = page_height - 40

    def text(x, y, value, size=9, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def text_right(x, y, value, size=9, font="F1"):
        value = str(value)
        text(max(left, x - len(value) * size * 0.5), y, value, size, font)

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def money(value):
        if not value:
            return ""
        return f"{float(value):,.2f}"

    def format_date(value):
        if not value:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%d/%m/%Y")
        return str(value)[:10]

    y = top
    text(left, y, "EUROGLASS HARDWARE", 14, "F2")
    y -= 14
    text(left, y, "Ph: 0300-5411417", 8)
    y -= 16
    text(left, y, "CUSTOMER ACCOUNT LEDGER", 12, "F2")
    y -= 14
    text(left, y, f"Account of: {customer.CustomerName}", 10, "F2")
    y -= 12
    text(left, y, f"Contact: {customer.ContactNo or 'N/A'}", 9)
    y -= 12
    generated = datetime.now().strftime("%d/%m/%Y %I:%M %p")
    text(left, y, f"Printed: {generated}", 8)
    y -= 10
    line(left, y, right, y)
    y -= 16

    col_date = left
    col_type = left + 68
    col_no = left + 118
    col_part = left + 158
    col_debit = right - 186
    col_credit = right - 118
    col_bal = right - 8

    text(col_date, y, "Date", 8, "F2")
    text(col_type, y, "Type", 8, "F2")
    text(col_no, y, "Vch No", 8, "F2")
    text(col_part, y, "Particulars", 8, "F2")
    text_right(col_debit, y, "Debit", 8, "F2")
    text_right(col_credit, y, "Credit", 8, "F2")
    text_right(col_bal, y, "Balance", 8, "F2")
    y -= 6
    line(left, y, right, y)
    y -= 14

    for entry in entries:
        if y < 70:
            text(left, 48, "Continued...", 8)
            break
        particulars = entry["particulars"]
        if len(particulars) > 42:
            particulars = particulars[:41] + "..."
        text(col_date, y, format_date(entry["date"]), 8)
        text(col_type, y, entry["vch_type"], 8)
        text(col_no, y, entry["vch_no"], 8)
        text(col_part, y, particulars, 8)
        text_right(col_debit, y, money(entry["debit"]), 8)
        text_right(col_credit, y, money(entry["credit"]), 8)
        closing = f"{abs(entry['balance']):,.2f} {entry['balance_side']}"
        text_right(col_bal, y, closing, 8)
        y -= 13

    y -= 4
    line(left, y, right, y)
    y -= 14
    text(col_part, y, "Total", 9, "F2")
    text_right(col_debit, y, f"{data['total_debit']:,.2f}", 9, "F2")
    text_right(col_credit, y, f"{data['total_credit']:,.2f}", 9, "F2")
    y -= 16
    closing = data["closing_balance"]
    text(
        left,
        y,
        f"Closing Balance: {abs(closing):,.2f} {_balance_side(closing)}",
        11,
        "F2",
    )

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


@ledger_bp.route("/list")
@login_required
def list_ledger():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_payment_status_column(db, cursor)
        ensure_invoice_payments_table(db, cursor)

        search = request.args.get("search", "")
        query = f"""
            SELECT
                c.CustomerID,
                c.CustomerName,
                COALESCE(c.PreviousBalance, 0) AS PreviousBalance,
                COALESCE(inv.InvoiceCount, 0) AS InvoiceCount,
                COALESCE(inv.TotalInvoiced, 0) AS TotalInvoiced,
                COALESCE(inv.TotalPaid, 0) AS TotalPaid,
                COALESCE(c.PreviousBalance, 0)
                    + COALESCE(inv.TotalInvoiced, 0)
                    - COALESCE(inv.TotalPaid, 0) AS Outstanding
            FROM Customers c
            LEFT JOIN (
                SELECT
                    i.CustomerID,
                    COUNT(*) AS InvoiceCount,
                    SUM(i.TotalAmount) AS TotalInvoiced,
                    SUM(COALESCE(pay.PaidAmount, 0)) AS TotalPaid
                FROM Invoices i
                {payments_join_sql("i")}
                WHERE {owner_sql("i")}
                GROUP BY i.CustomerID
            ) inv ON inv.CustomerID = c.CustomerID
            WHERE {owner_sql("c")}
        """
        params = []

        if search:
            query += " AND c.CustomerName LIKE ?"
            params.append(f"%{search}%")

        query += " ORDER BY c.CustomerName"

        cursor.execute(query, params or ())
        ledgers = cursor.fetchall()
        total_outstanding = sum(float(row.Outstanding or 0) for row in ledgers)

        return render_template(
            "ledger/list.html",
            ledgers=ledgers,
            search=search,
            total_outstanding=total_outstanding,
        )

    except Exception as e:
        flash(f"Error loading ledger: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))

    finally:
        cursor.close()


@ledger_bp.route("/customer/<int:id>")
@login_required
def customer_ledger(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_payment_status_column(db, cursor)
        ensure_invoice_payments_table(db, cursor)
        data = _load_customer_ledger(cursor, id)

        if not data:
            flash("Customer not found.", "danger")
            return redirect(url_for("ledger.list_ledger"))

        return render_template(
            "ledger/customer.html",
            back_url=url_for("ledger.list_ledger"),
            back_label="Back to Ledger",
            **data,
        )

    except Exception as e:
        flash(f"Error loading customer ledger: {str(e)}", "danger")
        return redirect(url_for("ledger.list_ledger"))

    finally:
        cursor.close()


@ledger_bp.route("/customer/<int:id>/pdf")
@login_required
def customer_ledger_pdf(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_payment_status_column(db, cursor)
        ensure_invoice_payments_table(db, cursor)
        data = _load_customer_ledger(cursor, id)

        if not data:
            flash("Customer not found.", "danger")
            return redirect(url_for("ledger.list_ledger"))

        pdf = _build_ledger_pdf(data)
        name = str(data["customer"].CustomerName or "customer").replace(" ", "_")
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"ledger_{name}.pdf",
        )

    except Exception as e:
        flash(f"Error generating ledger PDF: {str(e)}", "danger")
        return redirect(url_for("ledger.customer_ledger", id=id))

    finally:
        cursor.close()
