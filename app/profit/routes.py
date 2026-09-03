import time
from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.cogs import purchase_unit_cost_join, sold_line_cost_sql
from app.db import get_db_connection
from app.list_pdf import build_invoice_style_report_pdf, format_money
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
    best_month = report["best_month"]
    best_name = best_month["month_name"] if best_month and best_month["profit"] > 0 else "N/A"
    net_profit = float(report["total_profit"] or 0)
    net_color = (0.86, 0.08, 0.24) if net_profit < 0 else None
    columns = [
        {"label": "MONTH", "width": 88, "get": lambda row, _i: row["month_name"], "align": "left"},
        {"label": "REVENUE", "width": 91, "get": lambda row, _i: format_money(row["revenue"]), "align": "right"},
        {"label": "COST", "width": 91, "get": lambda row, _i: format_money(row["cost"]), "align": "right"},
        {
            "label": "CALCULATED",
            "width": 91,
            "get": lambda row, _i: format_money(row["calculated_profit"]),
            "align": "right",
        },
        {
            "label": "ADJUSTMENT",
            "width": 91,
            "get": lambda row, _i: format_money(row["adjustment"]),
            "align": "right",
        },
        {"label": "NET", "width": 91, "get": lambda row, _i: format_money(row["profit"]), "align": "right"},
    ]
    return build_invoice_style_report_pdf(
        "Monthly Profit Report",
        info_lines=[
            f"Year: {report['selected_year']}",
            f"Best Month: {best_name}",
            "Sales, cost and profit are counted in proportion to cash received.",
        ],
        columns=columns,
        rows=report["monthly_rows"],
        summary_rows=[
            {"label": "TOTAL REVENUE", "value": format_money(report["total_revenue"]), "highlight": True},
            {"label": "TOTAL COST", "value": format_money(report["total_cost"])},
            {"label": "CALCULATED", "value": format_money(report["calculated_profit"])},
            {"label": "ADJUSTMENTS", "value": format_money(report["total_adjustment"])},
            {
                "label": "NET PROFIT",
                "value": format_money(net_profit),
                "highlight": True,
                "color": net_color,
            },
        ],
    )


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
