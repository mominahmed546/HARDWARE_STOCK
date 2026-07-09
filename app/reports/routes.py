from datetime import date
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
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


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_monthly_sales_pdf(selected_year, monthly_rows, total_sales, total_invoices, best_month):
    commands = []

    def text(x, y, value, size=10, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    text(50, 780, f"Monthly Sales Report - {selected_year}", 16, "F2")
    text(50, 760, f"Total Sales: Rs {float(total_sales or 0):,.2f}", 11, "F1")
    text(300, 760, f"Total Invoices: {int(total_invoices or 0)}", 11, "F1")
    best_month_name = best_month["month_name"] if best_month and best_month["total_sales"] > 0 else "N/A"
    text(50, 742, f"Best Month: {best_month_name}", 11, "F1")

    table_top = 710
    row_height = 20
    text(60, table_top, "Month", 10, "F2")
    text(300, table_top, "Invoices", 10, "F2")
    text(420, table_top, "Total Sales", 10, "F2")
    line(50, table_top - 5, 560, table_top - 5)

    y = table_top - row_height
    for row in monthly_rows:
        text(60, y, row["month_name"], 10, "F1")
        text(320, y, str(row["invoice_count"]), 10, "F1")
        text(420, y, f"Rs {float(row['total_sales'] or 0):,.2f}", 10, "F1")
        y -= row_height

    line(50, y + 6, 560, y + 6)
    text(60, y - 10, "Total", 10, "F2")
    text(320, y - 10, str(int(total_invoices or 0)), 10, "F2")
    text(420, y - 10, f"Rs {float(total_sales or 0):,.2f}", 10, "F2")

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


def _monthly_sales_data(cursor, selected_year):
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
    return years, selected_year, monthly_rows, total_sales, total_invoices, best_month


@reports_bp.route("/monthly-sales")
@login_required
def monthly_sales():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        years, selected_year, monthly_rows, total_sales, total_invoices, best_month = _monthly_sales_data(
            cursor, selected_year
        )

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


@reports_bp.route("/monthly-sales/pdf")
@login_required
def monthly_sales_pdf():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        years, selected_year, monthly_rows, total_sales, total_invoices, best_month = _monthly_sales_data(
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
