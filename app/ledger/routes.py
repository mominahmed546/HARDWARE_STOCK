from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.list_pdf import build_invoice_style_report_pdf, format_money
from app.tenancy import owner_sql, request_user_id
from app.payments import ensure_invoice_payments_table
from app.perf import pagination_meta, parse_page, parse_page_size
from app.validators import (
    ValidationErrors,
    clean_date,
    clean_optional_string,
    clean_positive_decimal,
    clean_select_id,
    clean_string,
)

ledger_bp = Blueprint("ledger", __name__, url_prefix="/ledger")

_LEDGER_ENTRIES_READY = False
_LEDGER_SCHEMA_READY = False

ENTRY_TYPES = ("Debit", "Credit")
VCH_TYPES = ("Journal", "Adjustment", "Receipt", "Payment", "Opening", "Other")


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


def ensure_ledger_entries_table(db, cursor):
    global _LEDGER_ENTRIES_READY
    if _LEDGER_ENTRIES_READY:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS LedgerEntries (
            EntryID SERIAL PRIMARY KEY,
            CustomerID INTEGER NOT NULL REFERENCES Customers(CustomerID) ON DELETE CASCADE,
            EntryDate TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            EntryType VARCHAR(10) NOT NULL,
            Amount NUMERIC(12, 2) NOT NULL,
            Particulars VARCHAR(255) NOT NULL,
            VchType VARCHAR(40) DEFAULT 'Journal',
            Notes VARCHAR(255),
            UserID INTEGER
        )
        """
    )
    cursor.execute(
        """
        ALTER TABLE LedgerEntries
        ADD COLUMN IF NOT EXISTS UserID INTEGER
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ledger_entries_customer_id
        ON LedgerEntries (CustomerID)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ledger_entries_user_id
        ON LedgerEntries (UserID)
        """
    )
    db.commit()
    _LEDGER_ENTRIES_READY = True


def _prepare_ledger_schema(db, cursor):
    global _LEDGER_SCHEMA_READY
    if _LEDGER_SCHEMA_READY:
        ensure_invoice_payments_table(db, cursor)
        ensure_ledger_entries_table(db, cursor)
        return
    _ensure_previous_balance_column(db, cursor)
    _ensure_invoice_payment_status_column(db, cursor)
    ensure_invoice_payments_table(db, cursor)
    ensure_ledger_entries_table(db, cursor)
    _LEDGER_SCHEMA_READY = True


def _event_sort_date(value):
    if value is None:
        return datetime.min
    if getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


def _balance_side(balance):
    return "Dr" if float(balance or 0) >= 0 else "Cr"


def _build_ledger_entries(opening_balance, invoices, details_by_invoice, payments_by_invoice, manual_entries=None):
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
            "entry_id": None,
            "is_opening": True,
            "is_manual": False,
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

    for manual in manual_entries or []:
        entry_type = str(manual.EntryType or "").strip()
        events.append(
            {
                "kind": "manual_debit" if entry_type == "Debit" else "manual_credit",
                "date": manual.EntryDate,
                "manual": manual,
                "sort": 0 if entry_type == "Debit" else 1,
            }
        )

    events.sort(
        key=lambda event: (
            _event_sort_date(event["date"]),
            event["sort"],
            int(getattr(event.get("invoice"), "InvoiceID", 0) or 0),
            int(getattr(event.get("manual"), "EntryID", 0) or 0),
            int(getattr(event.get("payment"), "PaymentID", 0) or 0),
        )
    )

    for event in events:
        if event["kind"] == "invoice":
            invoice = event["invoice"]
            invoice_id = invoice.InvoiceID
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
                    "entry_id": None,
                    "is_opening": False,
                    "is_manual": False,
                }
            )
            continue

        if event["kind"] == "payment":
            invoice = event["invoice"]
            invoice_id = invoice.InvoiceID
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
                    "entry_id": None,
                    "is_opening": False,
                    "is_manual": False,
                }
            )
            continue

        manual = event["manual"]
        amount = float(manual.Amount or 0)
        particulars = str(manual.Particulars or "").strip() or "Ledger entry"
        notes = str(manual.Notes or "").strip()
        if notes:
            particulars = f"{particulars} — {notes}"
        vch_type = str(manual.VchType or "Journal").strip() or "Journal"
        entry_id = int(manual.EntryID)

        if event["kind"] == "manual_debit":
            balance += amount
            total_debit += amount
            entries.append(
                {
                    "date": manual.EntryDate,
                    "vch_no": f"L{entry_id}",
                    "vch_type": vch_type,
                    "particulars": particulars,
                    "debit": amount,
                    "credit": 0.0,
                    "balance": balance,
                    "balance_side": _balance_side(balance),
                    "invoice_id": None,
                    "entry_id": entry_id,
                    "is_opening": False,
                    "is_manual": True,
                }
            )
        else:
            balance -= amount
            total_credit += amount
            entries.append(
                {
                    "date": manual.EntryDate,
                    "vch_no": f"L{entry_id}",
                    "vch_type": vch_type,
                    "particulars": particulars,
                    "debit": 0.0,
                    "credit": amount,
                    "balance": balance,
                    "balance_side": _balance_side(balance),
                    "invoice_id": None,
                    "entry_id": entry_id,
                    "is_opening": False,
                    "is_manual": True,
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

    cursor.execute(
        f"""
        SELECT
            EntryID,
            CustomerID,
            EntryDate,
            EntryType,
            Amount,
            Particulars,
            COALESCE(VchType, 'Journal') AS VchType,
            Notes
        FROM LedgerEntries
        WHERE CustomerID = ? AND {owner_sql()}
        ORDER BY EntryDate ASC, EntryID ASC
        """,
        (customer_id,),
    )
    manual_entries = cursor.fetchall()

    opening_balance = float(customer.PreviousBalance or 0)
    entries, total_debit, total_credit, closing_balance = _build_ledger_entries(
        opening_balance,
        invoices,
        details_by_invoice,
        payments_by_invoice,
        manual_entries,
    )
    total_invoiced = sum(float(invoice.TotalAmount or 0) for invoice in invoices)
    total_paid = sum(
        float(payment.Amount or 0)
        for payments in payments_by_invoice.values()
        for payment in payments
    )
    manual_debit = sum(
        float(row.Amount or 0) for row in manual_entries if str(row.EntryType or "") == "Debit"
    )
    manual_credit = sum(
        float(row.Amount or 0) for row in manual_entries if str(row.EntryType or "") == "Credit"
    )

    return {
        "customer": customer,
        "invoices": invoices,
        "entries": entries,
        "opening_balance": opening_balance,
        "total_invoiced": total_invoiced,
        "total_paid": total_paid,
        "manual_debit": manual_debit,
        "manual_credit": manual_credit,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": closing_balance,
        "outstanding": opening_balance + total_invoiced + manual_debit - total_paid - manual_credit,
    }


def _format_ledger_date(value):
    if not value:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y")
    return str(value)[:10]


def _ledger_money(value):
    if not value:
        return ""
    return format_money(value)


def _build_ledger_pdf(data):
    customer = data["customer"]
    closing = float(data["closing_balance"] or 0)
    closing_side = _balance_side(closing)
    closing_color = (0.86, 0.08, 0.24) if closing > 0 else None
    columns = [
        {
            "label": "DATE",
            "width": 56,
            "get": lambda row, _i: _format_ledger_date(row.get("date")),
            "align": "left",
        },
        {
            "label": "TYPE",
            "width": 56,
            "get": lambda row, _i: row.get("vch_type") or "-",
            "align": "left",
        },
        {
            "label": "VCH NO",
            "width": 40,
            "get": lambda row, _i: str(row.get("vch_no") or "-"),
            "align": "center",
        },
        {
            "label": "PARTICULARS",
            "width": 191,
            "get": lambda row, _i: row.get("particulars") or "-",
            "align": "left",
            "wrap": 32,
        },
        {
            "label": "DEBIT",
            "width": 66,
            "get": lambda row, _i: _ledger_money(row.get("debit")),
            "align": "right",
        },
        {
            "label": "CREDIT",
            "width": 66,
            "get": lambda row, _i: _ledger_money(row.get("credit")),
            "align": "right",
        },
        {
            "label": "BALANCE",
            "width": 68,
            "get": lambda row, _i: f"{abs(float(row.get('balance') or 0)):,.2f} {row.get('balance_side') or _balance_side(row.get('balance'))}",
            "align": "right",
        },
    ]
    return build_invoice_style_report_pdf(
        "Account Ledger",
        info_lines=[
            f"Account of: {customer.CustomerName}",
            f"Contact: {customer.ContactNo or 'N/A'}",
            f"Opening Balance: {format_money(data['opening_balance'])}",
        ],
        columns=columns,
        rows=data["entries"],
        summary_rows=[
            {"label": "TOTAL DEBIT", "value": format_money(data["total_debit"])},
            {"label": "TOTAL CREDIT", "value": format_money(data["total_credit"])},
            {
                "label": "CLOSING BALANCE",
                "value": f"{abs(closing):,.2f} {closing_side}",
                "highlight": True,
                "color": closing_color,
            },
        ],
        footer_from_index=-4,
    )


def _load_customers_for_select(cursor):
    cursor.execute(
        f"""
        SELECT CustomerID, CustomerName
        FROM Customers
        WHERE {owner_sql()}
        ORDER BY CustomerName
        """
    )
    return cursor.fetchall()


def _parse_entry_form(form, fixed_customer_id=None):
    errors = ValidationErrors()
    if fixed_customer_id is not None:
        customer_id = int(fixed_customer_id)
    else:
        customer_id = clean_select_id(form.get("customer_id"), "customer_id", errors, label="Customer")

    entry_date = clean_date(form.get("entry_date"), "entry_date", errors, label="Entry date")
    entry_type = clean_string(
        form.get("entry_type"),
        "entry_type",
        errors,
        required=True,
        min_len=1,
        max_len=10,
        label="Entry type",
    )
    if entry_type and entry_type not in ENTRY_TYPES:
        errors.add("entry_type", "Entry type must be Debit or Credit.")

    amount = clean_positive_decimal(
        form.get("amount"), "amount", errors, required=True, min_val=0.01, label="Amount"
    )
    particulars = clean_string(
        form.get("particulars"),
        "particulars",
        errors,
        required=True,
        min_len=1,
        max_len=255,
        label="Particulars",
    )
    vch_type = clean_string(
        form.get("vch_type"),
        "vch_type",
        errors,
        required=True,
        min_len=1,
        max_len=40,
        label="Voucher type",
    )
    if vch_type and vch_type not in VCH_TYPES:
        errors.add("vch_type", "Choose a valid voucher type.")
    notes = clean_optional_string(form.get("notes"), "notes", errors, max_len=255, label="Notes")

    form_data = {
        "customer_id": str(customer_id or form.get("customer_id") or ""),
        "entry_date": form.get("entry_date") or "",
        "entry_type": entry_type or form.get("entry_type") or "Debit",
        "amount": form.get("amount") or "",
        "particulars": particulars or form.get("particulars") or "",
        "vch_type": vch_type or form.get("vch_type") or "Journal",
        "notes": notes or form.get("notes") or "",
    }

    if not errors.valid:
        return None, form_data, errors.errors

    return (
        {
            "customer_id": customer_id,
            "entry_date": entry_date,
            "entry_type": entry_type,
            "amount": amount,
            "particulars": particulars,
            "vch_type": vch_type,
            "notes": notes or None,
        },
        form_data,
        {},
    )


@ledger_bp.route("/list")
@login_required
def list_ledger():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _prepare_ledger_schema(db, cursor)

        search = request.args.get("search", "")
        page = parse_page(request.args.get("page"))
        page_size = parse_page_size(request.args.get("page_size"))

        # CashReceived on invoices replaces the global InvoicePayments join.
        base_from = f"""
            FROM Customers c
            LEFT JOIN (
                SELECT
                    i.CustomerID,
                    COUNT(*) AS InvoiceCount,
                    SUM(i.TotalAmount) AS TotalInvoiced,
                    SUM(COALESCE(i.CashReceived, 0)) AS TotalPaid
                FROM Invoices i
                WHERE {owner_sql("i")}
                GROUP BY i.CustomerID
            ) inv ON inv.CustomerID = c.CustomerID
            LEFT JOIN (
                SELECT
                    CustomerID,
                    SUM(CASE WHEN EntryType = 'Debit' THEN Amount ELSE 0 END) AS ManualDebit,
                    SUM(CASE WHEN EntryType = 'Credit' THEN Amount ELSE 0 END) AS ManualCredit
                FROM LedgerEntries
                WHERE {owner_sql()}
                GROUP BY CustomerID
            ) le ON le.CustomerID = c.CustomerID
            WHERE {owner_sql("c")}
        """
        params = []
        if search:
            base_from += " AND c.CustomerName LIKE ?"
            params.append(f"%{search}%")

        outstanding_expr = """
            COALESCE(c.PreviousBalance, 0)
                + COALESCE(inv.TotalInvoiced, 0)
                + COALESCE(le.ManualDebit, 0)
                - COALESCE(inv.TotalPaid, 0)
                - COALESCE(le.ManualCredit, 0)
        """

        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS TotalCount,
                COALESCE(SUM({outstanding_expr}), 0) AS TotalOutstanding
            {base_from}
            """,
            params or (),
        )
        totals = cursor.fetchone()
        total = int(totals.TotalCount or 0)
        total_outstanding = float(totals.TotalOutstanding or 0)
        pagination = pagination_meta(total, page, page_size)

        cursor.execute(
            f"""
            SELECT
                c.CustomerID,
                c.CustomerName,
                COALESCE(c.PreviousBalance, 0) AS PreviousBalance,
                COALESCE(inv.InvoiceCount, 0) AS InvoiceCount,
                COALESCE(inv.TotalInvoiced, 0) AS TotalInvoiced,
                COALESCE(inv.TotalPaid, 0) AS TotalPaid,
                COALESCE(le.ManualDebit, 0) AS ManualDebit,
                COALESCE(le.ManualCredit, 0) AS ManualCredit,
                {outstanding_expr} AS Outstanding
            {base_from}
            ORDER BY c.CustomerName
            LIMIT ? OFFSET ?
            """,
            list(params) + [pagination["page_size"], pagination["offset"]],
        )
        ledgers = cursor.fetchall()

        return render_template(
            "ledger/list.html",
            ledgers=ledgers,
            search=search,
            total_outstanding=total_outstanding,
            pagination=pagination,
        )

    except Exception as e:
        flash(f"Error loading ledger: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))

    finally:
        cursor.close()


@ledger_bp.route("/entry/new", methods=["GET", "POST"])
@login_required
def add_ledger_entry():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _prepare_ledger_schema(db, cursor)
        customers = _load_customers_for_select(cursor)
        preset_customer_id = request.args.get("customer_id", type=int)

        if request.method == "POST":
            parsed, form_data, errors = _parse_entry_form(request.form)
            if errors:
                return render_template(
                    "ledger/entry_form.html",
                    customers=customers,
                    form_data=form_data,
                    errors=errors,
                    fixed_customer=None,
                    entry_types=ENTRY_TYPES,
                    vch_types=VCH_TYPES,
                )

            cursor.execute(
                f"SELECT CustomerID FROM Customers WHERE CustomerID = ? AND {owner_sql()}",
                (parsed["customer_id"],),
            )
            if not cursor.fetchone():
                flash("Customer not found.", "danger")
                return redirect(url_for("ledger.list_ledger"))

            cursor.execute(
                """
                INSERT INTO LedgerEntries (
                    CustomerID, EntryDate, EntryType, Amount, Particulars, VchType, Notes, UserID
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed["customer_id"],
                    parsed["entry_date"],
                    parsed["entry_type"],
                    parsed["amount"],
                    parsed["particulars"],
                    parsed["vch_type"],
                    parsed["notes"],
                    request_user_id(),
                ),
            )
            db.commit()
            flash("Ledger entry added.", "success")
            return redirect(url_for("ledger.customer_ledger", id=parsed["customer_id"]))

        form_data = {
            "customer_id": str(preset_customer_id or ""),
            "entry_date": datetime.now().strftime("%d/%m/%Y"),
            "entry_type": "Debit",
            "amount": "",
            "particulars": "",
            "vch_type": "Journal",
            "notes": "",
        }
        return render_template(
            "ledger/entry_form.html",
            customers=customers,
            form_data=form_data,
            errors={},
            fixed_customer=None,
            entry_types=ENTRY_TYPES,
            vch_types=VCH_TYPES,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error saving ledger entry: {str(e)}", "danger")
        return redirect(url_for("ledger.list_ledger"))

    finally:
        cursor.close()


@ledger_bp.route("/customer/<int:id>/entry", methods=["GET", "POST"])
@login_required
def add_customer_ledger_entry(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _prepare_ledger_schema(db, cursor)
        cursor.execute(
            f"""
            SELECT CustomerID, CustomerName
            FROM Customers
            WHERE CustomerID = ? AND {owner_sql()}
            """,
            (id,),
        )
        customer = cursor.fetchone()
        if not customer:
            flash("Customer not found.", "danger")
            return redirect(url_for("ledger.list_ledger"))

        if request.method == "POST":
            parsed, form_data, errors = _parse_entry_form(request.form, fixed_customer_id=id)
            if errors:
                return render_template(
                    "ledger/entry_form.html",
                    customers=[],
                    form_data=form_data,
                    errors=errors,
                    fixed_customer=customer,
                    entry_types=ENTRY_TYPES,
                    vch_types=VCH_TYPES,
                )

            cursor.execute(
                """
                INSERT INTO LedgerEntries (
                    CustomerID, EntryDate, EntryType, Amount, Particulars, VchType, Notes, UserID
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    id,
                    parsed["entry_date"],
                    parsed["entry_type"],
                    parsed["amount"],
                    parsed["particulars"],
                    parsed["vch_type"],
                    parsed["notes"],
                    request_user_id(),
                ),
            )
            db.commit()
            flash("Ledger entry added.", "success")
            return redirect(url_for("ledger.customer_ledger", id=id))

        form_data = {
            "customer_id": str(id),
            "entry_date": datetime.now().strftime("%d/%m/%Y"),
            "entry_type": "Debit",
            "amount": "",
            "particulars": "",
            "vch_type": "Journal",
            "notes": "",
        }
        return render_template(
            "ledger/entry_form.html",
            customers=[],
            form_data=form_data,
            errors={},
            fixed_customer=customer,
            entry_types=ENTRY_TYPES,
            vch_types=VCH_TYPES,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error saving ledger entry: {str(e)}", "danger")
        return redirect(url_for("ledger.customer_ledger", id=id))

    finally:
        cursor.close()


@ledger_bp.route("/entry/<int:entry_id>/delete", methods=["POST"])
@login_required
def delete_ledger_entry(entry_id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _prepare_ledger_schema(db, cursor)
        cursor.execute(
            f"""
            SELECT EntryID, CustomerID
            FROM LedgerEntries
            WHERE EntryID = ? AND {owner_sql()}
            """,
            (entry_id,),
        )
        row = cursor.fetchone()
        if not row:
            flash("Ledger entry not found.", "danger")
            return redirect(url_for("ledger.list_ledger"))

        customer_id = int(row.CustomerID)
        cursor.execute(
            f"DELETE FROM LedgerEntries WHERE EntryID = ? AND {owner_sql()}",
            (entry_id,),
        )
        db.commit()
        flash("Ledger entry deleted.", "success")
        return redirect(url_for("ledger.customer_ledger", id=customer_id))

    except Exception as e:
        db.rollback()
        flash(f"Error deleting ledger entry: {str(e)}", "danger")
        return redirect(url_for("ledger.list_ledger"))

    finally:
        cursor.close()


@ledger_bp.route("/customer/<int:id>")
@login_required
def customer_ledger(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _prepare_ledger_schema(db, cursor)
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
        _prepare_ledger_schema(db, cursor)
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
