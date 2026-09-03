from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.list_pdf import build_invoice_style_report_pdf, format_money
from app.tenancy import owner_sql
from app.payments import ensure_invoice_payments_table

reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def _build_monthly_sales_pdf(selected_year, monthly_rows, total_sales, total_invoices, best_month):
    best_month_name = best_month["month_name"] if best_month and best_month["total_sales"] > 0 else "N/A"
    columns = [
        {"label": "MONTH", "width": 200, "get": lambda row, _i: row["month_name"], "align": "left"},
        {
            "label": "INVOICES",
            "width": 120,
            "get": lambda row, _i: str(int(row["invoice_count"] or 0)),
            "align": "right",
        },
        {
            "label": "TOTAL SALES",
            "width": 223,
            "get": lambda row, _i: format_money(row["total_sales"]),
            "align": "right",
        },
    ]
    return build_invoice_style_report_pdf(
        "Monthly Sales Report",
        info_lines=[
            f"Year: {selected_year}",
            f"Best Month: {best_month_name}",
        ],
        columns=columns,
        rows=monthly_rows,
        summary_rows=[
            {"label": "TOTAL INVOICES", "value": str(int(total_invoices or 0))},
            {"label": "TOTAL SALES", "value": format_money(total_sales), "highlight": True},
        ],
    )


def _monthly_sales_data(cursor, selected_year):
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

    year_start = date(int(selected_year), 1, 1)
    year_end = date(int(selected_year) + 1, 1, 1)

    cursor.execute(
        f"""
        SELECT
            MONTH([Date]) AS SalesMonth,
            COUNT(*) AS InvoiceCount,
            ISNULL(SUM(TotalAmount), 0) AS TotalSales
        FROM Invoices
        WHERE [Date] >= ? AND [Date] < ? AND {owner_sql()}
        GROUP BY MONTH([Date])
        ORDER BY SalesMonth
        """,
        (year_start, year_end),
    )
    rows_by_month = {row.SalesMonth: row for row in cursor.fetchall()}

    monthly_rows = []
    total_sales = 0
    total_invoices = 0

    for month_number, month_name in enumerate(MONTHS, start=1):
        row = rows_by_month.get(month_number)
        sales = float(row.TotalSales) if row else 0
        invoice_count = int(row.InvoiceCount) if row else 0
        total_sales += sales
        total_invoices += invoice_count
        monthly_rows.append(
            {
                "month_number": month_number,
                "month_name": month_name,
                "invoice_count": invoice_count,
                "total_sales": sales,
            }
        )

    best_month = max(monthly_rows, key=lambda row: row["total_sales"], default=None)

    # Payment status breakdown for pie chart
    cursor.execute(
        f"""
        SELECT
            COALESCE(PaymentStatus, 'Unpaid') AS PaymentStatus,
            COUNT(*) AS InvoiceCount,
            ISNULL(SUM(TotalAmount), 0) AS TotalAmount
        FROM Invoices
        WHERE [Date] >= ? AND [Date] < ? AND {owner_sql()}
        GROUP BY COALESCE(PaymentStatus, 'Unpaid')
        """,
        (year_start, year_end),
    )

    status_rows = cursor.fetchall()
    paid_count = 0
    unpaid_count = 0
    partial_count = 0
    paid_amount = 0.0
    unpaid_amount = 0.0
    partial_amount = 0.0
    for s in status_rows:
        status = str(s.PaymentStatus).strip().lower()
        if status == "paid":
            paid_count = int(s.InvoiceCount)
            paid_amount = float(s.TotalAmount)
        elif status == "partial":
            partial_count = int(s.InvoiceCount)
            partial_amount = float(s.TotalAmount)
        else:
            unpaid_count += int(s.InvoiceCount)
            unpaid_amount += float(s.TotalAmount)

    payment_status = {
        "paid_count": paid_count,
        "unpaid_count": unpaid_count,
        "partial_count": partial_count,
        "paid_amount": paid_amount,
        "unpaid_amount": unpaid_amount,
        "partial_amount": partial_amount,
    }

    return years, selected_year, monthly_rows, total_sales, total_invoices, best_month, payment_status


@reports_bp.route("/monthly-sales")
@login_required
def monthly_sales():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_invoice_payments_table(db, cursor)
        years, selected_year, monthly_rows, total_sales, total_invoices, best_month, payment_status = \
            _monthly_sales_data(cursor, selected_year)

        return render_template(
            "reports/monthly_sales.html",
            years=years or [selected_year],
            selected_year=selected_year,
            monthly_rows=monthly_rows,
            total_sales=total_sales,
            total_invoices=total_invoices,
            best_month=best_month,
            payment_status=payment_status,
        )

    except Exception as e:
        flash(f"Error loading monthly sales report: {str(e)}", "danger")
        return render_template(
            "reports/monthly_sales.html",
            years=[selected_year],
            selected_year=selected_year,
            monthly_rows=[],
            total_sales=0,
            total_invoices=0,
            best_month=None,
            payment_status={"paid_count":0,"unpaid_count":0,"partial_count":0,"paid_amount":0,"unpaid_amount":0,"partial_amount":0},
        )

    finally:
        cursor.close()


@reports_bp.route("/monthly-sales/pdf")
@login_required
def monthly_sales_pdf():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_invoice_payments_table(db, cursor)
        years, selected_year, monthly_rows, total_sales, total_invoices, best_month, _ = _monthly_sales_data(
            cursor, selected_year
        )
        pdf = _build_monthly_sales_pdf(selected_year, monthly_rows, total_sales, total_invoices, best_month)
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"monthly_sales_{selected_year}.pdf",
        )

    except Exception as e:
        flash(f"Error generating monthly sales PDF: {str(e)}", "danger")
        return redirect(url_for("reports.monthly_sales", year=selected_year))

    finally:
        cursor.close()
