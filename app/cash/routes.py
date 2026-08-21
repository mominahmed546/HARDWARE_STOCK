from calendar import monthrange
from datetime import date, datetime, timedelta
from io import BytesIO
from math import floor

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.payments import (
    ensure_cash_accounts,
    ensure_invoice_payments_table,
    ensure_purchase_payments_table,
    get_cash_openings,
    save_cash_openings,
)
from app.profit.routes import _year_total_profit
from app.tenancy import owner_sql
from app.validators import ValidationErrors, clean_positive_decimal

cash_bp = Blueprint("cash", __name__, url_prefix="/cash")

LOW_STOCK_THRESHOLD = 10
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ensure_cash_schema(db, cursor):
    ensure_invoice_payments_table(db, cursor)
    ensure_cash_accounts(db, cursor)
    ensure_purchase_payments_table(db, cursor)


def _split_row(row):
    if not row:
        return 0.0, 0.0, 0.0
    cash_amount = float(row.CashAmount or 0)
    bank_amount = float(row.BankAmount or 0)
    total_amount = float(row.TotalAmount or 0)
    if total_amount == 0:
        total_amount = cash_amount + bank_amount
    return cash_amount, bank_amount, total_amount


def _payment_split(cursor, where_sql="", params=()):
    cursor.execute(
        f"""
        SELECT
            ISNULL(SUM(CASE WHEN COALESCE(p.PaymentMethod, 'Cash') = 'Bank' THEN p.Amount ELSE 0 END), 0) AS BankAmount,
            ISNULL(SUM(CASE WHEN COALESCE(p.PaymentMethod, 'Cash') = 'Cash' THEN p.Amount ELSE 0 END), 0) AS CashAmount,
            ISNULL(SUM(p.Amount), 0) AS TotalAmount
        FROM InvoicePayments p
        JOIN Invoices i ON i.InvoiceID = p.InvoiceID
        {where_sql}
        {"AND" if where_sql else "WHERE"} {owner_sql("i")}
        """,
        params,
    )
    return _split_row(cursor.fetchone())


def _purchase_payment_split(cursor, where_sql="", params=()):
    cursor.execute(
        f"""
        SELECT
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Bank' THEN pp.Amount ELSE 0 END), 0) AS BankAmount,
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Cash' THEN pp.Amount ELSE 0 END), 0) AS CashAmount,
            ISNULL(SUM(pp.Amount), 0) AS TotalAmount
        FROM PurchasePayments pp
        JOIN Purchases p ON p.PurchaseID = pp.PurchaseID
        {where_sql}
        {"AND" if where_sql else "WHERE"} {owner_sql("p")}
        """,
        params,
    )
    return _split_row(cursor.fetchone())


def _balances_through(cursor, through_date, cash_opening, bank_opening):
    cash_received, bank_received, _ = _payment_split(
        cursor,
        "WHERE CAST(p.PaymentDate AS DATE) <= ?",
        (through_date,),
    )
    cash_paid, bank_paid, _ = _purchase_payment_split(
        cursor,
        "WHERE CAST(pp.PaymentDate AS DATE) <= ?",
        (through_date,),
    )
    return cash_opening + cash_received - cash_paid, bank_opening + bank_received - bank_paid


def _accrual_profit_for_year(cursor, year):
    """Total profit for the year, matching the Monthly Profit report total."""
    return _year_total_profit(cursor, year)


def _cash_excluding_profit(cursor, through_date, cash_opening, cash_in_hand=None):
    """Split the cash drawer into working capital vs profit.

    Profit is taken straight from the Monthly Profit report's Total Profit
    for the same year, so "Profit sitting in cash" always matches what
    /profit/monthly shows. Capital ("cash for purchases") is whatever's left
    of the drawer once that profit share is set aside.
    """
    total_profit = _accrual_profit_for_year(cursor, through_date.year)

    if cash_in_hand is None:
        return 0.0, total_profit, total_profit

    cash_in_hand_value = max(float(cash_in_hand or 0), 0.0)
    profit_in_cash = min(total_profit, cash_in_hand_value)
    capital = max(cash_in_hand_value - profit_in_cash, 0.0)
    return capital, profit_in_cash, total_profit


def _buy_suggestions(cursor, budget, threshold=LOW_STOCK_THRESHOLD, limit=40):
    """Suggest low-stock items that can be bought with the capital budget."""
    budget = float(budget or 0)
    if budget <= 0:
        return [], 0.0

    cursor.execute(
        f"""
        SELECT
            ItemID,
            ItemName,
            COALESCE(Qty, 0) AS Qty,
            COALESCE(PurchaseRate, 0) AS PurchaseRate,
            COALESCE(SaleRate, 0) AS SaleRate
        FROM Item
        WHERE {owner_sql()}
          AND COALESCE(Qty, 0) < ?
          AND COALESCE(PurchaseRate, 0) > 0
        ORDER BY Qty ASC, ItemName ASC
        """,
        (threshold,),
    )
    suggestions = []
    remaining = budget
    for item in cursor.fetchall():
        unit_cost = float(item.PurchaseRate or 0)
        if unit_cost <= 0:
            continue
        current_qty = int(item.Qty or 0)
        needed = max(threshold - current_qty, 1)
        affordable = int(floor(remaining / unit_cost))
        if affordable <= 0:
            continue
        suggested_qty = min(needed, affordable)
        line_cost = suggested_qty * unit_cost
        remaining -= line_cost
        suggestions.append(
            {
                "item_id": int(item.ItemID),
                "item_name": item.ItemName,
                "current_qty": current_qty,
                "purchase_rate": unit_cost,
                "sale_rate": float(item.SaleRate or 0),
                "suggested_qty": suggested_qty,
                "estimated_cost": line_cost,
                "status": "Out of stock" if current_qty <= 0 else "Low stock",
            }
        )
        if len(suggestions) >= limit:
            break

    spent = budget - remaining
    return suggestions, spent


def _daily_receipts(cursor, selected_date):
    cursor.execute(
        f"""
        SELECT
            p.PaymentID,
            p.Amount,
            p.PaymentDate,
            COALESCE(p.PaymentMethod, 'Cash') AS PaymentMethod,
            p.Notes,
            i.InvoiceID,
            c.CustomerName
        FROM InvoicePayments p
        JOIN Invoices i ON i.InvoiceID = p.InvoiceID
        JOIN Customers c ON c.CustomerID = i.CustomerID
        WHERE CAST(p.PaymentDate AS DATE) = ? AND {owner_sql("i")}
        ORDER BY p.PaymentDate ASC, p.PaymentID ASC
        """,
        (selected_date,),
    )
    return cursor.fetchall()


def _month_day_rows(cursor, year, month, cash_opening, bank_opening):
    period_start = date(year, month, 1)
    day_before = period_start - timedelta(days=1)
    running_cash, running_bank = _balances_through(cursor, day_before, cash_opening, bank_opening)

    cursor.execute(
        f"""
        SELECT
            CAST(PaymentDate AS DATE) AS SaleDate,
            ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Bank' THEN Amount ELSE 0 END), 0) AS BankAmount,
            ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Cash' THEN Amount ELSE 0 END), 0) AS CashAmount,
            ISNULL(SUM(Amount), 0) AS TotalAmount
        FROM InvoicePayments
        WHERE YEAR(PaymentDate) = ? AND MONTH(PaymentDate) = ?
          AND InvoiceID IN (SELECT InvoiceID FROM Invoices WHERE {owner_sql()})
        GROUP BY CAST(PaymentDate AS DATE)
        ORDER BY SaleDate
        """,
        (year, month),
    )
    by_date = {}
    for row in cursor.fetchall():
        sale_date = row.SaleDate
        if isinstance(sale_date, datetime):
            sale_date = sale_date.date()
        by_date[sale_date] = row

    cursor.execute(
        f"""
        SELECT
            CAST(pp.PaymentDate AS DATE) AS PurchaseDate,
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Bank' THEN pp.Amount ELSE 0 END), 0) AS BankAmount,
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Cash' THEN pp.Amount ELSE 0 END), 0) AS CashAmount,
            ISNULL(SUM(pp.Amount), 0) AS TotalAmount
        FROM PurchasePayments pp
        JOIN Purchases p ON p.PurchaseID = pp.PurchaseID
        WHERE YEAR(pp.PaymentDate) = ? AND MONTH(pp.PaymentDate) = ?
          AND {owner_sql("p")}
        GROUP BY CAST(pp.PaymentDate AS DATE)
        ORDER BY PurchaseDate
        """,
        (year, month),
    )
    purchases_by_date = {}
    for row in cursor.fetchall():
        purchase_date = row.PurchaseDate
        if isinstance(purchase_date, datetime):
            purchase_date = purchase_date.date()
        purchases_by_date[purchase_date] = row

    last_day = monthrange(year, month)[1]
    rows = []
    for day in range(1, last_day + 1):
        sale_date = date(year, month, day)
        row = by_date.get(sale_date)
        cash_amount, bank_amount, total_amount = _split_row(row)
        purchase_row = purchases_by_date.get(sale_date)
        purchase_cash_amount, purchase_bank_amount, purchase_total_amount = _split_row(purchase_row)
        running_cash += cash_amount - purchase_cash_amount
        running_bank += bank_amount - purchase_bank_amount
        if total_amount > 0 or purchase_total_amount > 0:
            rows.append(
                {
                    "sale_date": sale_date,
                    "cash_amount": cash_amount,
                    "bank_amount": bank_amount,
                    "total_amount": total_amount,
                    "cash_in_hand": running_cash,
                    "bank_balance": running_bank,
                }
            )
    return rows


def _build_cash_pdf(title, lines, totals):
    commands = []

    def text(x, y, value, size=10, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    text(50, 780, title, 16, "F2")
    y = 752
    for label, value in totals:
        text(50, y, f"{label}: Rs {value:,.2f}", 10, "F1")
        y -= 16

    y -= 8
    text(50, y, "Date", 10, "F2")
    text(150, y, "Cash", 10, "F2")
    text(260, y, "Bank", 10, "F2")
    text(370, y, "Total", 10, "F2")
    line(50, y - 5, 560, y - 5)
    y -= 22

    for row in lines:
        if y < 60:
            break
        text(50, y, row["date"], 9, "F1")
        text(150, y, f"{row['cash']:,.2f}", 9, "F1")
        text(260, y, f"{row['bank']:,.2f}", 9, "F1")
        text(370, y, f"{row['total']:,.2f}", 9, "F1")
        y -= 16

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


def _period_from_request():
    view = (request.args.get("view") or "daily").strip().lower()
    if view not in {"daily", "monthly"}:
        view = "daily"

    today = date.today()
    date_value = request.args.get("date") or today.isoformat()
    try:
        selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        selected_date = today
        date_value = today.isoformat()

    years = list(range(today.year - 5, today.year + 2))
    selected_year = request.args.get("year", default=today.year, type=int)
    selected_month = request.args.get("month", default=today.month, type=int)
    if selected_year not in years:
        selected_year = today.year
    if selected_month < 1 or selected_month > 12:
        selected_month = today.month

    return view, selected_date, date_value, selected_year, selected_month, years


def _report_context(cursor, view, selected_date, selected_year, selected_month):
    cash_opening, bank_opening = get_cash_openings(cursor)

    if view == "monthly":
        last_day = monthrange(selected_year, selected_month)[1]
        through_date = date(selected_year, selected_month, last_day)
        period_cash, period_bank, period_total = _payment_split(
            cursor,
            "WHERE YEAR(p.PaymentDate) = ? AND MONTH(p.PaymentDate) = ?",
            (selected_year, selected_month),
        )
        receipts = []
        day_rows = _month_day_rows(cursor, selected_year, selected_month, cash_opening, bank_opening)
        period_label = f"{MONTHS[selected_month - 1]} {selected_year}"
    else:
        through_date = selected_date
        period_cash, period_bank, period_total = _payment_split(
            cursor,
            "WHERE CAST(p.PaymentDate AS DATE) = ?",
            (selected_date,),
        )
        receipts = _daily_receipts(cursor, selected_date)
        day_rows = []
        period_label = selected_date.strftime("%d/%m/%Y")

    cash_in_hand, bank_balance = _balances_through(cursor, through_date, cash_opening, bank_opening)
    cash_excluding_profit, profit_in_cash, accrual_profit = _cash_excluding_profit(
        cursor, through_date, cash_opening, cash_in_hand
    )
    return {
        "cash_opening": cash_opening,
        "bank_opening": bank_opening,
        "period_cash": period_cash,
        "period_bank": period_bank,
        "period_total": period_total,
        "cash_in_hand": cash_in_hand,
        "bank_balance": bank_balance,
        "cash_excluding_profit": cash_excluding_profit,
        "profit_in_cash": profit_in_cash,
        "accrual_profit": accrual_profit,
        "receipts": receipts,
        "day_rows": day_rows,
        "period_label": period_label,
        "through_date": through_date,
    }


@cash_bp.route("/", methods=["GET", "POST"])
@login_required
def cash_book():
    db = get_db_connection(app)
    cursor = db.cursor()
    view, selected_date, date_value, selected_year, selected_month, years = _period_from_request()

    adjust_errors = ValidationErrors()
    try:
        _ensure_cash_schema(db, cursor)

        if request.method == "POST" and request.form.get("action") == "save_openings":
            errors = ValidationErrors()
            cash_opening = clean_positive_decimal(
                request.form.get("cash_opening"),
                "cash_opening",
                errors,
                min_val=0,
                label="Opening cash in drawer",
            )
            bank_opening = clean_positive_decimal(
                request.form.get("bank_opening"),
                "bank_opening",
                errors,
                min_val=0,
                label="Opening bank balance",
            )
            if not errors.valid:
                flash(errors.first(), "danger")
            else:
                save_cash_openings(cursor, cash_opening, bank_opening)
                db.commit()
                flash("Opening cash and bank balances saved.", "success")
            return redirect(
                url_for(
                    "cash.cash_book",
                    view=view,
                    date=date_value,
                    year=selected_year,
                    month=selected_month,
                )
            )

        if request.method == "POST" and request.form.get("action") == "transfer_to_bank":
            transfer_amount = clean_positive_decimal(
                request.form.get("transfer_amount"),
                "transfer_amount",
                adjust_errors,
                min_val=0.01,
                label="Transfer amount",
            )
            if not adjust_errors.valid:
                flash(adjust_errors.first(), "danger")
            else:
                data = _report_context(cursor, view, selected_date, selected_year, selected_month)
                cash_opening, bank_opening = get_cash_openings(cursor)
                transfer_amount = float(transfer_amount)
                current_cash_in_hand = float(data["cash_in_hand"] or 0)
                if transfer_amount > current_cash_in_hand + 0.005:
                    flash(
                        f"Cannot transfer more than the current cash in hand "
                        f"(Rs {current_cash_in_hand:,.2f}).",
                        "danger",
                    )
                else:
                    # Shifting both openings by the same amount moves the split
                    # between drawer and bank without touching the total
                    # (received - paid) either side has tracked historically,
                    # the same way "Adjust current cash" below only shifts
                    # cash_opening.
                    save_cash_openings(
                        cursor,
                        max(cash_opening - transfer_amount, 0.0),
                        bank_opening + transfer_amount,
                    )
                    db.commit()
                    flash(f"Transferred Rs {transfer_amount:,.2f} from cash in hand to the bank account.", "success")
                    return redirect(
                        url_for(
                            "cash.cash_book",
                            view=view,
                            date=date_value,
                            year=selected_year,
                            month=selected_month,
                        )
                    )

        if request.method == "POST" and request.form.get("action") == "transfer_to_cash":
            transfer_amount = clean_positive_decimal(
                request.form.get("transfer_from_bank_amount"),
                "transfer_from_bank_amount",
                adjust_errors,
                min_val=0.01,
                label="Transfer amount",
            )
            if not adjust_errors.valid:
                flash(adjust_errors.first(), "danger")
            else:
                data = _report_context(cursor, view, selected_date, selected_year, selected_month)
                cash_opening, bank_opening = get_cash_openings(cursor)
                transfer_amount = float(transfer_amount)
                current_bank = float(data["bank_balance"] or 0)
                if transfer_amount > current_bank + 0.005:
                    flash(
                        f"Cannot transfer more than the current bank balance "
                        f"(Rs {current_bank:,.2f}).",
                        "danger",
                    )
                else:
                    # Mirror of cash→bank: move the split the other way by
                    # shifting openings equally so historical receipts/payments
                    # stay intact.
                    save_cash_openings(
                        cursor,
                        cash_opening + transfer_amount,
                        max(bank_opening - transfer_amount, 0.0),
                    )
                    db.commit()
                    flash(
                        f"Transferred Rs {transfer_amount:,.2f} from the bank account to cash in hand.",
                        "success",
                    )
                    return redirect(
                        url_for(
                            "cash.cash_book",
                            view=view,
                            date=date_value,
                            year=selected_year,
                            month=selected_month,
                        )
                    )

        if request.method == "POST" and request.form.get("action") == "set_current_cash":
            target_cash = clean_positive_decimal(
                request.form.get("current_cash_in_drawer"),
                "current_cash_in_drawer",
                adjust_errors,
                min_val=0,
                label="Current cash in drawer",
            )
            if not adjust_errors.valid:
                flash(adjust_errors.first(), "danger")
            else:
                data = _report_context(cursor, view, selected_date, selected_year, selected_month)
                cash_opening, bank_opening = get_cash_openings(cursor)
                # Shift opening by the delta so displayed drawer cash matches target.
                delta = float(target_cash) - float(data["cash_in_hand"] or 0)
                new_cash_opening = max(cash_opening + delta, 0.0)
                save_cash_openings(cursor, new_cash_opening, bank_opening)
                db.commit()
                flash("Current cash in drawer updated.", "success")
                return redirect(
                    url_for(
                        "cash.cash_book",
                        view=view,
                        date=date_value,
                        year=selected_year,
                        month=selected_month,
                    )
                )

        data = _report_context(cursor, view, selected_date, selected_year, selected_month)
        return render_template(
            "cash/index.html",
            view=view,
            selected_date=date_value,
            selected_year=selected_year,
            selected_month=selected_month,
            years=years,
            months=MONTHS,
            adjust_errors=adjust_errors.errors,
            **data,
        )

    except Exception as e:
        flash(f"Error loading cash book: {str(e)}", "danger")
        return render_template(
            "cash/index.html",
            view=view,
            selected_date=date_value,
            selected_year=selected_year,
            selected_month=selected_month,
            years=years,
            months=MONTHS,
            cash_opening=0,
            bank_opening=0,
            period_cash=0,
            period_bank=0,
            period_total=0,
            cash_in_hand=0,
            bank_balance=0,
            cash_excluding_profit=0,
            profit_in_cash=0,
            accrual_profit=0,
            receipts=[],
            day_rows=[],
            period_label="",
        )

    finally:
        cursor.close()


@cash_bp.route("/pdf")
@login_required
def cash_book_pdf():
    db = get_db_connection(app)
    cursor = db.cursor()
    view, selected_date, date_value, selected_year, selected_month, _years = _period_from_request()

    try:
        _ensure_cash_schema(db, cursor)
        data = _report_context(cursor, view, selected_date, selected_year, selected_month)
        title = f"Cash / Bank Report - {data['period_label']}"
        totals = [
            ("Cash received", data["period_cash"]),
            ("Bank received", data["period_bank"]),
            ("Total received", data["period_total"]),
            ("Cash in drawer", data["cash_in_hand"]),
            ("Cash for purchases", data["cash_excluding_profit"]),
            ("Bank for purchases", data["bank_balance"]),
            ("Profit in cash", data["profit_in_cash"]),
            ("Bank account", data["bank_balance"]),
        ]
        if view == "monthly":
            lines = [
                {
                    "date": row["sale_date"].strftime("%d/%m/%Y"),
                    "cash": row["cash_amount"],
                    "bank": row["bank_amount"],
                    "total": row["total_amount"],
                }
                for row in data["day_rows"]
                if row["total_amount"]
            ]
        else:
            lines = [
                {
                    "date": (row.PaymentDate.strftime("%d/%m/%Y") if hasattr(row.PaymentDate, "strftime") else str(row.PaymentDate)[:10]),
                    "cash": float(row.Amount or 0) if str(row.PaymentMethod) == "Cash" else 0.0,
                    "bank": float(row.Amount or 0) if str(row.PaymentMethod) == "Bank" else 0.0,
                    "total": float(row.Amount or 0),
                }
                for row in data["receipts"]
            ]
        pdf = _build_cash_pdf(title, lines, totals)
        name = f"cash_{view}_{data['period_label'].replace(' ', '_').replace('/', '-')}.pdf"
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=name)

    except Exception as e:
        flash(f"Error generating cash PDF: {str(e)}", "danger")
        return redirect(
            url_for(
                "cash.cash_book",
                view=view,
                date=date_value,
                year=selected_year,
                month=selected_month,
            )
        )

    finally:
        cursor.close()


@cash_bp.route("/buy-suggestions")
@login_required
def buy_suggestions():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_cash_schema(db, cursor)
        cash_opening, bank_opening = get_cash_openings(cursor)
        today = date.today()
        cash_in_hand, _bank = _balances_through(cursor, today, cash_opening, bank_opening)
        cash_excluding_profit, profit_in_cash, _accrual_profit = _cash_excluding_profit(
            cursor, today, cash_opening, cash_in_hand
        )
        suggestions, spent = _buy_suggestions(cursor, cash_excluding_profit)
        return render_template(
            "cash/buy_suggestions.html",
            cash_in_hand=cash_in_hand,
            cash_excluding_profit=cash_excluding_profit,
            profit_in_cash=profit_in_cash,
            suggestions=suggestions,
            spent=spent,
            remaining=max(cash_excluding_profit - spent, 0.0),
            threshold=LOW_STOCK_THRESHOLD,
        )
    except Exception as e:
        flash(f"Error loading buy suggestions: {str(e)}", "danger")
        return render_template(
            "cash/buy_suggestions.html",
            cash_in_hand=0,
            cash_excluding_profit=0,
            profit_in_cash=0,
            suggestions=[],
            spent=0,
            remaining=0,
            threshold=LOW_STOCK_THRESHOLD,
        )
    finally:
        cursor.close()
