from datetime import date
from io import BytesIO

from flask import Blueprint, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection

profit_bp = Blueprint("profit", __name__, url_prefix="/profit")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _monthly_profit_data(cursor, selected_year):
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

    # Profit per invoice line = (sale_rate - purchase_rate) * qty, grouped by invoice month
    cursor.execute(
        """
        SELECT
            MONTH(i.[Date]) AS SalesMonth,
            ISNULL(SUM(id.Qty * id.Rate), 0)                          AS Revenue,
            ISNULL(SUM(id.Qty * COALESCE(it.PurchaseRate, 0)), 0)     AS Cost,
            ISNULL(SUM(id.Qty * (id.Rate - COALESCE(it.PurchaseRate, 0))), 0) AS Profit
        FROM Invoices i
        JOIN InvoiceDetails id ON id.InvoiceID = i.InvoiceID
        LEFT JOIN Item it ON it.ItemID = id.ItemID
        WHERE YEAR(i.[Date]) = ?
        GROUP BY MONTH(i.[Date])
        ORDER BY SalesMonth
        """,
        (selected_year,),
    )
    rows_by_month = {row.SalesMonth: row for row in cursor.fetchall()}

    monthly_rows = []
    total_revenue = 0.0
    total_cost = 0.0
    total_profit = 0.0

    for month_number, month_name in enumerate(MONTHS, start=1):
        row = rows_by_month.get(month_number)
        revenue = float(row.Revenue) if row else 0.0
        cost = float(row.Cost) if row else 0.0
        profit = float(row.Profit) if row else 0.0
        total_revenue += revenue
        total_cost += cost
        total_profit += profit
        monthly_rows.append({
            "month_number": month_number,
            "month_name": month_name,
            "revenue": revenue,
            "cost": cost,
            "profit": profit,
        })

    best_month = max(monthly_rows, key=lambda r: r["profit"], default=None)
    return years, selected_year, monthly_rows, total_revenue, total_cost, total_profit, best_month


def _build_profit_pdf(selected_year, monthly_rows, total_revenue, total_cost, total_profit, best_month):
    commands = []

    def text(x, y, value, size=10, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({_pdf_escape(value)}) Tj ET")

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    text(50, 780, f"Monthly Profit Report - {selected_year}", 16, "F2")
    text(50, 760, f"Total Revenue: Rs {total_revenue:,.2f}", 10, "F1")
    text(250, 760, f"Total Cost: Rs {total_cost:,.2f}", 10, "F1")
    text(430, 760, f"Total Profit: Rs {total_profit:,.2f}", 10, "F1")
    best_name = best_month["month_name"] if best_month and best_month["profit"] > 0 else "N/A"
    text(50, 742, f"Best Month: {best_name}", 10, "F1")

    table_top = 710
    row_h = 20
    text(50, table_top, "Month", 10, "F2")
    text(175, table_top, "Revenue (Rs)", 10, "F2")
    text(315, table_top, "Cost (Rs)", 10, "F2")
    text(440, table_top, "Profit (Rs)", 10, "F2")
    line(50, table_top - 5, 560, table_top - 5)

    y = table_top - row_h
    for row in monthly_rows:
        text(50, y, row["month_name"], 10, "F1")
        text(175, y, f"{row['revenue']:,.2f}", 10, "F1")
        text(315, y, f"{row['cost']:,.2f}", 10, "F1")
        text(440, y, f"{row['profit']:,.2f}", 10, "F1")
        y -= row_h

    line(50, y + 6, 560, y + 6)
    text(50, y - 10, "Total", 10, "F2")
    text(175, y - 10, f"{total_revenue:,.2f}", 10, "F2")
    text(315, y - 10, f"{total_cost:,.2f}", 10, "F2")
    text(440, y - 10, f"{total_profit:,.2f}", 10, "F2")

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


@profit_bp.route("/monthly")
@login_required
def monthly_profit():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        years, selected_year, monthly_rows, total_revenue, total_cost, total_profit, best_month = \
            _monthly_profit_data(cursor, selected_year)

        return render_template(
            "profit/monthly.html",
            years=years or [selected_year],
            selected_year=selected_year,
            monthly_rows=monthly_rows,
            total_revenue=total_revenue,
            total_cost=total_cost,
            total_profit=total_profit,
            best_month=best_month,
        )

    except Exception as e:
        flash(f"Error loading profit report: {str(e)}", "danger")
        return render_template(
            "profit/monthly.html",
            years=[selected_year],
            selected_year=selected_year,
            monthly_rows=[],
            total_revenue=0,
            total_cost=0,
            total_profit=0,
            best_month=None,
        )

    finally:
        cursor.close()


@profit_bp.route("/monthly/pdf")
@login_required
def monthly_profit_pdf():
    selected_year = request.args.get("year", default=date.today().year, type=int)
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        years, selected_year, monthly_rows, total_revenue, total_cost, total_profit, best_month = \
            _monthly_profit_data(cursor, selected_year)

        pdf = _build_profit_pdf(selected_year, monthly_rows, total_revenue, total_cost, total_profit, best_month)
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
