import time
from datetime import date
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.cogs import purchase_unit_cost_join, sold_line_cost_sql
from app.db import get_db_connection
from app.tenancy import owner_sql, request_user_id
from app.payments import ensure_invoice_payments_table, paid_ratio_sql
from app.validators import (
    ValidationErrors,
    clean_date,
    clean_optional_string,
    clean_positive_decimal,
)

profit_bp = Blueprint("profit", __name__, url_prefix="/profit")

_YEAR_PROFIT_CACHE = {}
YEAR_PROFIT_CACHE_TTL = 120.0
_ADJUSTMENTS_READY = False

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def ensure_profit_adjustments_table(db, cursor):
    """Manual increases/decreases applied on top of calculated profit."""
    global _ADJUSTMENTS_READY
    if _ADJUSTMENTS_READY:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ProfitAdjustments (
            AdjustmentID SERIAL PRIMARY KEY,
            AdjustmentDate TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            Amount NUMERIC(12, 2) NOT NULL,
            Reason VARCHAR(255),
            UserID INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_profit_adjustments_user_date
        ON ProfitAdjustments (UserID, AdjustmentDate)
        """
    )
    db.commit()
    _ADJUSTMENTS_READY = True


def clear_year_profit_cache(user_id=None):
    """Drop cached year profit so an adjustment shows up immediately."""
    if user_id is None:
        user_id = request_user_id()
    for key in [key for key in _YEAR_PROFIT_CACHE if key[0] == user_id]:
        _YEAR_PROFIT_CACHE.pop(key, None)


def _year_bounds(selected_year):
    year = int(selected_year)
    return date(year, 1, 1), date(year + 1, 1, 1)


def profit_adjustment_total(cursor, selected_year):
    """Net of the manual adjustments recorded for the year (may be negative)."""
    cursor.execute(
        f"""
        SELECT COALESCE(SUM(Amount), 0) AS AdjustmentTotal
        FROM ProfitAdjustments
        WHERE {owner_sql()} AND AdjustmentDate >= ? AND AdjustmentDate < ?
        """,
        _year_bounds(selected_year),
    )
    row = cursor.fetchone()
    return float(row.AdjustmentTotal or 0) if row else 0.0


def _monthly_adjustment_totals(cursor, selected_year):
    cursor.execute(
        f"""
        SELECT
            MONTH(AdjustmentDate) AS SalesMonth,
            COALESCE(SUM(Amount), 0) AS AdjustmentTotal
        FROM ProfitAdjustments
        WHERE {owner_sql()} AND AdjustmentDate >= ? AND AdjustmentDate < ?
        GROUP BY MONTH(AdjustmentDate)
        """,
        _year_bounds(selected_year),
    )
    return {int(row.SalesMonth): float(row.AdjustmentTotal or 0) for row in cursor.fetchall()}


def list_profit_adjustments(cursor, selected_year):
    cursor.execute(
        f"""
        SELECT AdjustmentID, AdjustmentDate, Amount, Reason
        FROM ProfitAdjustments
        WHERE {owner_sql()} AND AdjustmentDate >= ? AND AdjustmentDate < ?
        ORDER BY AdjustmentDate DESC, AdjustmentID DESC
        """,
        _year_bounds(selected_year),
    )
    return cursor.fetchall()


def get_year_total_profit(cursor, selected_year, *, use_cache=True):
    """Year profit with a short-lived per-account cache for dashboard/cash pages.

    Calculated the same way as the Monthly Profit report, then shifted by any
    manual adjustments for that year, so every page that shows or reserves
    year profit agrees on one number.
    """
    from app.cogs import purchase_unit_cost_join, sold_line_cost_sql

    user_id = request_user_id()
    year = int(selected_year)
    cache_key = (user_id, year)
    if use_cache and user_id:
        now = time.monotonic()
        cached = _YEAR_PROFIT_CACHE.get(cache_key)
        if cached and (now - cached[0]) < YEAR_PROFIT_CACHE_TTL:
            return cached[1]

    year_start = date(year, 1, 1)
    year_end = date(year + 1, 1, 1)
    line_cost = sold_line_cost_sql("id")
    paid_ratio = paid_ratio_sql("i", None)
    cursor.execute(
        f"""
        SELECT
            ISNULL(SUM((id.Qty * id.Rate) * ({paid_ratio})), 0)
                - ISNULL(SUM(({line_cost}) * ({paid_ratio})), 0) AS Profit
        FROM Invoices i
        JOIN InvoiceDetails id ON id.InvoiceID = i.InvoiceID
        LEFT JOIN Item it ON it.ItemID = id.ItemID
        {purchase_unit_cost_join("id")}
        WHERE i.[Date] >= ? AND i.[Date] < ? AND {owner_sql("i")}
        """,
        (year_start, year_end),
    )
    row = cursor.fetchone()
    calculated = max(float(row.Profit or 0) if row else 0.0, 0.0)
    profit = max(calculated + profit_adjustment_total(cursor, year), 0.0)
    if use_cache and user_id:
        _YEAR_PROFIT_CACHE[cache_key] = (time.monotonic(), profit)
    return profit


def _monthly_profit_data(cursor, selected_year):
    cursor.execute(
        f"""
        SELECT DISTINCT YEAR([Date]) AS SalesYear
        FROM Invoices
        WHERE {owner_sql()}
        ORDER BY SalesYear DESC
        """
    )
    years = [row.SalesYear for row in cursor.fetchall()]
    if selected_year not in years and years:
        selected_year = years[0]

    # Revenue/cost/profit scale with cash received on each invoice.
    # Unpaid = 0, partial = paid/total, fully paid = 100%.
    # Use CashReceived so we skip the global InvoicePayments aggregate join.
    line_cost = sold_line_cost_sql("id")
    paid_ratio = paid_ratio_sql("i", None)
    year_start = date(int(selected_year), 1, 1)
    year_end = date(int(selected_year) + 1, 1, 1)
    cursor.execute(
        f"""
        SELECT
            MONTH(i.[Date]) AS SalesMonth,
            ISNULL(SUM((id.Qty * id.Rate) * ({paid_ratio})), 0) AS Revenue,
            ISNULL(SUM(({line_cost}) * ({paid_ratio})), 0) AS Cost,
            ISNULL(SUM((id.Qty * id.Rate) * ({paid_ratio})), 0)
                - ISNULL(SUM(({line_cost}) * ({paid_ratio})), 0) AS Profit
        FROM Invoices i
        JOIN InvoiceDetails id ON id.InvoiceID = i.InvoiceID
        LEFT JOIN Item it ON it.ItemID = id.ItemID
        {purchase_unit_cost_join("id")}
        WHERE i.[Date] >= ? AND i.[Date] < ? AND {owner_sql("i")}
        GROUP BY MONTH(i.[Date])
        ORDER BY SalesMonth
        """,
        (year_start, year_end),
    )
    rows_by_month = {row.SalesMonth: row for row in cursor.fetchall()}
    adjustments_by_month = _monthly_adjustment_totals(cursor, selected_year)

    monthly_rows = []
    total_revenue = 0.0
    total_cost = 0.0
    total_calculated = 0.0
    total_adjustment = 0.0

    for month_number, month_name in enumerate(MONTHS, start=1):
        row = rows_by_month.get(month_number)
        revenue = float(row.Revenue) if row else 0.0
        cost = float(row.Cost) if row else 0.0
        calculated = float(row.Profit) if row else 0.0
        adjustment = adjustments_by_month.get(month_number, 0.0)
        total_revenue += revenue
        total_cost += cost
        total_calculated += calculated
        total_adjustment += adjustment
        monthly_rows.append({
            "month_number": month_number,
            "month_name": month_name,
            "revenue": revenue,
            "cost": cost,
            "calculated_profit": calculated,
            "adjustment": adjustment,
            "profit": calculated + adjustment,
        })

    best_month = max(monthly_rows, key=lambda r: r["profit"], default=None)
    return {
        "years": years,
        "selected_year": selected_year,
        "monthly_rows": monthly_rows,
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "calculated_profit": total_calculated,
        "total_adjustment": total_adjustment,
        "total_profit": total_calculated + total_adjustment,
        "best_month": best_month,
    }


def _build_profit_pdf(report):
    commands = []
    selected_year = report["selected_year"]
    monthly_rows = report["monthly_rows"]
    total_revenue = report["total_revenue"]
    total_cost = report["total_cost"]
    total_profit = report["total_profit"]
    total_adjustment = report["total_adjustment"]
    best_month = report["best_month"]

    def text(x, y, value, size=10, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    text(50, 780, f"Monthly Profit Report - {selected_year}", 16, "F2")
    text(50, 760, f"Total Revenue: Rs {total_revenue:,.2f}", 10, "F1")
    text(250, 760, f"Total Cost: Rs {total_cost:,.2f}", 10, "F1")
    text(430, 760, f"Net Profit: Rs {total_profit:,.2f}", 10, "F1")
    best_name = best_month["month_name"] if best_month and best_month["profit"] > 0 else "N/A"
    text(50, 742, f"Best Month: {best_name}", 10, "F1")
    text(250, 742, f"Calculated: Rs {report['calculated_profit']:,.2f}", 10, "F1")
    text(430, 742, f"Adjustments: Rs {total_adjustment:,.2f}", 10, "F1")
    text(50, 726, "Sales, cost and profit are counted in proportion to cash received.", 8, "F1")

    table_top = 700
    row_h = 20
    text(50, table_top, "Month", 10, "F2")
    text(140, table_top, "Revenue (Rs)", 10, "F2")
    text(250, table_top, "Cost (Rs)", 10, "F2")
    text(350, table_top, "Calculated", 10, "F2")
    text(440, table_top, "Adjustment", 10, "F2")
    text(520, table_top, "Net", 10, "F2")
    line(50, table_top - 5, 570, table_top - 5)

    y = table_top - row_h
    for row in monthly_rows:
        text(50, y, row["month_name"], 10, "F1")
        text(140, y, f"{row['revenue']:,.2f}", 10, "F1")
        text(250, y, f"{row['cost']:,.2f}", 10, "F1")
        text(350, y, f"{row['calculated_profit']:,.2f}", 10, "F1")
        text(440, y, f"{row['adjustment']:,.2f}", 10, "F1")
        text(520, y, f"{row['profit']:,.2f}", 10, "F1")
        y -= row_h

    line(50, y + 6, 570, y + 6)
    text(50, y - 10, "Total", 10, "F2")
    text(140, y - 10, f"{total_revenue:,.2f}", 10, "F2")
    text(250, y - 10, f"{total_cost:,.2f}", 10, "F2")
    text(350, y - 10, f"{report['calculated_profit']:,.2f}", 10, "F2")
    text(440, y - 10, f"{total_adjustment:,.2f}", 10, "F2")
    text(520, y - 10, f"{total_profit:,.2f}", 10, "F2")

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


@profit_bp.route("/monthly", methods=["GET", "POST"])
@login_required
def monthly_profit():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {"adjustment_date": date.today().isoformat(), "direction": "increase"}

    try:
        ensure_invoice_payments_table(db, cursor)
        ensure_profit_adjustments_table(db, cursor)

        if request.method == "POST" and request.form.get("action") == "add_adjustment":
            form_data = request.form.to_dict()
            adjustment_date = clean_date(
                request.form.get("adjustment_date") or date.today().isoformat(),
                "adjustment_date",
                errors,
                label="Adjustment date",
            )
            amount = clean_positive_decimal(
                request.form.get("amount"),
                "amount",
                errors,
                min_val=0.01,
                label="Amount",
            )
            reason = clean_optional_string(
                request.form.get("reason"), "reason", errors, max_len=255, label="Reason"
            )
            direction = (request.form.get("direction") or "increase").strip().lower()
            if direction not in {"increase", "decrease"}:
                errors.add("direction", "Choose whether this increases or decreases profit.")

            if errors.valid:
                signed = float(amount) if direction == "increase" else -float(amount)
                cursor.execute(
                    """
                    INSERT INTO ProfitAdjustments (AdjustmentDate, Amount, Reason, UserID)
                    VALUES (?, ?, ?, ?)
                    """,
                    (adjustment_date, signed, reason or None, request_user_id()),
                )
                db.commit()
                clear_year_profit_cache()
                flash(
                    f"Profit {'increased' if signed > 0 else 'decreased'} by "
                    f"Rs {abs(signed):,.2f}.",
                    "success",
                )
                return redirect(
                    url_for("profit.monthly_profit", year=adjustment_date[:4] or selected_year)
                )
            flash(errors.first(), "danger")

        report = _monthly_profit_data(cursor, selected_year)
        return render_template(
            "profit/monthly.html",
            years=report["years"] or [report["selected_year"]],
            adjustments=list_profit_adjustments(cursor, report["selected_year"]),
            errors=errors.errors,
            form_data=form_data,
            **{k: v for k, v in report.items() if k != "years"},
        )

    except Exception as e:
        db.rollback()
        flash(f"Error loading profit report: {str(e)}", "danger")
        return render_template(
            "profit/monthly.html",
            years=[selected_year],
            selected_year=selected_year,
            monthly_rows=[],
            total_revenue=0,
            total_cost=0,
            calculated_profit=0,
            total_adjustment=0,
            total_profit=0,
            best_month=None,
            adjustments=[],
            errors=errors.errors,
            form_data=form_data,
        )

    finally:
        cursor.close()


@profit_bp.route("/adjustments/<int:id>/delete", methods=["POST"])
@login_required
def delete_profit_adjustment(id):
    selected_year = request.form.get("year", type=int) or date.today().year
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_profit_adjustments_table(db, cursor)
        cursor.execute(
            f"DELETE FROM ProfitAdjustments WHERE AdjustmentID = ? AND {owner_sql()}",
            (id,),
        )
        removed = int(cursor.rowcount or 0)
        db.commit()
        clear_year_profit_cache()
        if removed:
            flash("Profit adjustment removed.", "success")
        else:
            flash("Profit adjustment not found.", "danger")
    except Exception as e:
        db.rollback()
        flash(f"Error removing profit adjustment: {str(e)}", "danger")
    finally:
        cursor.close()

    return redirect(url_for("profit.monthly_profit", year=selected_year))


@profit_bp.route("/monthly/pdf")
@login_required
def monthly_profit_pdf():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_invoice_payments_table(db, cursor)
        ensure_profit_adjustments_table(db, cursor)
        report = _monthly_profit_data(cursor, selected_year)
        selected_year = report["selected_year"]

        pdf = _build_profit_pdf(report)
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"profit_report_{selected_year}.pdf",
        )

    except Exception as e:
        flash(f"Error generating profit PDF: {str(e)}", "danger")
        return redirect(url_for("profit.monthly_profit", year=selected_year))

    finally:
        cursor.close()
