from datetime import date

from flask import Blueprint, flash, render_template, request
from flask_login import login_required

from app import app
from app.db import get_db_connection

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


@reports_bp.route("/monthly-sales")
@login_required
def monthly_sales():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        cursor.execute(
            """
            SELECT DISTINCT YEAR([Date]) AS SalesYear
            FROM Invoices
            ORDER BY SalesYear DESC
            """
        )
        years = [row.SalesYear for row in cursor.fetchall()]
        if selected_year not in years and years:
            selected_year = years[0]

        cursor.execute(
            """
            SELECT
                MONTH([Date]) AS SalesMonth,
                COUNT(*) AS InvoiceCount,
                ISNULL(SUM(TotalAmount), 0) AS TotalSales
            FROM Invoices
            WHERE YEAR([Date]) = ?
            GROUP BY MONTH([Date])
            ORDER BY SalesMonth
            """,
            (selected_year,),
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

        return render_template(
            "reports/monthly_sales.html",
            years=years or [selected_year],
            selected_year=selected_year,
            monthly_rows=monthly_rows,
            total_sales=total_sales,
            total_invoices=total_invoices,
            best_month=best_month,
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
        )

    finally:
        cursor.close()
