from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection

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


@ledger_bp.route("/list")
@login_required
def list_ledger():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        _ensure_previous_balance_column(db, cursor)
        _ensure_invoice_payment_status_column(db, cursor)

        search = request.args.get("search", "")
        query = """
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
                    SUM(
                        CASE
                            WHEN COALESCE(i.PaymentStatus, 'Unpaid') = 'Paid' THEN i.TotalAmount
                            ELSE 0
                        END
                    ) AS TotalPaid
                FROM Invoices i
                GROUP BY i.CustomerID
            ) inv ON inv.CustomerID = c.CustomerID
            WHERE 1=1
        """
        params = []

        if search:
            query += " AND c.CustomerName LIKE ?"
            params.append(f"%{search}%")

        query += " ORDER BY c.CustomerName"

        cursor.execute(query, params or ())
        ledgers = cursor.fetchall()

        return render_template("ledger/list.html", ledgers=ledgers, search=search)

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

        cursor.execute(
            """
            SELECT
                CustomerID,
                CustomerName,
                ContactNo,
                COALESCE(PreviousBalance, 0) AS PreviousBalance
            FROM Customers
            WHERE CustomerID = ?
            """,
            (id,),
        )
        customer = cursor.fetchone()

        if not customer:
            flash("Customer not found.", "danger")
            return redirect(url_for("ledger.list_ledger"))

        cursor.execute(
            """
            SELECT
                i.InvoiceID,
                i.[Date] AS InvoiceDate,
                i.TotalAmount,
                COALESCE(i.PaymentStatus, 'Unpaid') AS PaymentStatus
            FROM Invoices i
            WHERE i.CustomerID = ?
            ORDER BY i.[Date] DESC, i.InvoiceID DESC
            """,
            (id,),
        )
        invoices = cursor.fetchall()

        opening_balance = float(customer.PreviousBalance or 0)
        total_invoiced = sum(float(invoice.TotalAmount or 0) for invoice in invoices)
        total_paid = sum(
            float(invoice.TotalAmount or 0)
            for invoice in invoices
            if (invoice.PaymentStatus or "Unpaid") == "Paid"
        )
        outstanding = opening_balance + total_invoiced - total_paid

        return render_template(
            "ledger/customer.html",
            customer=customer,
            invoices=invoices,
            opening_balance=opening_balance,
            total_invoiced=total_invoiced,
            total_paid=total_paid,
            outstanding=outstanding,
        )

    except Exception as e:
        flash(f"Error loading customer ledger: {str(e)}", "danger")
        return redirect(url_for("ledger.list_ledger"))

    finally:
        cursor.close()
