from datetime import date

from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from app import app
from app.cash.routes import _cash_excluding_profit
from app.db import get_db_connection
from app.payments import (
    ensure_cash_accounts,
    ensure_invoice_payments_table,
    ensure_purchase_payments_table,
    get_cash_openings,
)
from app.perf import day_bounds, through_exclusive
from app.stock_constants import LOW_STOCK_THRESHOLD
from app.tenancy import owner_sql

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard/cash-capital")
@login_required
def cash_capital():
    """Load profit split after the dashboard shell renders (keeps Home fast)."""
    cash_in_hand = request.args.get("cash_in_hand", type=float)
    bank_balance = request.args.get("bank_balance", type=float)
    if cash_in_hand is None or bank_balance is None:
        return jsonify(error="Missing balances"), 400

    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        (
            cash_excluding_profit,
            profit_in_cash,
            year_profit,
            bank_excluding_profit,
            profit_in_bank,
        ) = _cash_excluding_profit(
            cursor,
            date.today(),
            0,
            cash_in_hand,
            bank_balance=bank_balance,
        )
        cursor.close()
        return jsonify(
            cash_excluding_profit=cash_excluding_profit,
            profit_in_cash=profit_in_cash,
            bank_excluding_profit=bank_excluding_profit,
            profit_in_bank=profit_in_bank,
            year_profit=year_profit,
        )
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@dashboard_bp.route("/dashboard")
@login_required
def dashboard():
    defaults = {
        "total_sales": 0,
        "total_purchases": 0,
        "total_customers": 0,
        "total_items": 0,
        "total_products": 0,
        "low_stock_items": [],
        "out_of_stock_items": [],
        "recent_customers": [],
        "today_cash": 0,
        "today_bank": 0,
        "cash_in_hand": 0,
        "bank_balance": 0,
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "cash_excluding_profit": 0,
        "bank_excluding_profit": 0,
        "profit_in_cash": 0,
        "profit_in_bank": 0,
    }

    try:
        db = get_db_connection(app)
        cursor = db.cursor()

        # Schema ensures are cached after the first call in this worker.
        ensure_invoice_payments_table(db, cursor)
        ensure_cash_accounts(db, cursor)
        ensure_purchase_payments_table(db, cursor)

        cursor.execute(
            f"""
            SELECT
                (SELECT COALESCE(SUM(TotalAmount), 0) FROM Invoices WHERE {owner_sql()}) AS TotalSales,
                (SELECT COALESCE(SUM(TotalAmount), 0) FROM Purchases WHERE {owner_sql()}) AS TotalPurchases,
                (SELECT COUNT(*) FROM Customers WHERE {owner_sql()}) AS TotalCustomers,
                (
                    SELECT COUNT(*)
                    FROM (
                        SELECT 1
                        FROM Item
                        WHERE {owner_sql()}
                        GROUP BY LOWER(BTRIM(ItemName)), CategoryID
                    ) grouped_items
                ) AS ItemCount
            """
        )
        summary = cursor.fetchone()
        total_sales = float(summary.TotalSales or 0)
        total_purchases = float(summary.TotalPurchases or 0)
        total_customers = int(summary.TotalCustomers or 0)
        total_items = int(summary.ItemCount or 0)

        # One Item scan for out-of-stock + low-stock panels.
        cursor.execute(
            f"""
            WITH grouped AS (
                SELECT
                    MIN(ItemName) AS ItemName,
                    SUM(Qty) AS Qty
                FROM Item
                WHERE {owner_sql()}
                GROUP BY LOWER(BTRIM(ItemName)), CategoryID
            ),
            out_stock AS (
                SELECT 'out' AS kind, ItemName, Qty
                FROM grouped
                WHERE Qty <= 0
                ORDER BY ItemName ASC
                LIMIT 5
            ),
            low_stock AS (
                SELECT 'low' AS kind, ItemName, Qty
                FROM grouped
                WHERE Qty > 0 AND Qty < {LOW_STOCK_THRESHOLD}
                ORDER BY Qty ASC, ItemName ASC
                LIMIT 5
            )
            SELECT kind, ItemName, Qty FROM out_stock
            UNION ALL
            SELECT kind, ItemName, Qty FROM low_stock
            """
        )
        out_of_stock_items = []
        low_stock_items = []
        for row in cursor.fetchall():
            if row.kind == "out":
                out_of_stock_items.append(row)
            else:
                low_stock_items.append(row)

        cursor.execute(
            f"""
            SELECT CustomerName, ContactNo
            FROM Customers
            WHERE {owner_sql()}
            ORDER BY CustomerID DESC
            LIMIT 5
            """
        )
        recent_customers = cursor.fetchall()

        cash_opening, bank_opening = get_cash_openings(cursor)

        today = date.today()
        today_start, today_end = day_bounds(today)
        until = through_exclusive(today)

        # Invoice payment totals (today + all-time through today) in one pass.
        # Half-open ranges keep PaymentDate indexes usable.
        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN p.PaymentDate >= ? AND p.PaymentDate < ?
                             AND COALESCE(p.PaymentMethod, 'Cash') = 'Cash'
                        THEN p.Amount ELSE 0
                    END
                ), 0) AS TodayCash,
                COALESCE(SUM(
                    CASE
                        WHEN p.PaymentDate >= ? AND p.PaymentDate < ?
                             AND COALESCE(p.PaymentMethod, 'Cash') = 'Bank'
                        THEN p.Amount ELSE 0
                    END
                ), 0) AS TodayBank,
                COALESCE(SUM(
                    CASE
                        WHEN p.PaymentDate < ?
                             AND COALESCE(p.PaymentMethod, 'Cash') = 'Cash'
                        THEN p.Amount ELSE 0
                    END
                ), 0) AS CashReceived,
                COALESCE(SUM(
                    CASE
                        WHEN p.PaymentDate < ?
                             AND COALESCE(p.PaymentMethod, 'Cash') = 'Bank'
                        THEN p.Amount ELSE 0
                    END
                ), 0) AS BankReceived
            FROM InvoicePayments p
            JOIN Invoices i ON i.InvoiceID = p.InvoiceID
            WHERE {owner_sql("i")}
              AND p.PaymentDate < ?
            """,
            (today_start, today_end, today_start, today_end, until, until, until),
        )
        pay_row = cursor.fetchone()
        today_cash = float(pay_row.TodayCash or 0) if pay_row else 0.0
        today_bank = float(pay_row.TodayBank or 0) if pay_row else 0.0
        cash_received_total = float(pay_row.CashReceived or 0) if pay_row else 0.0
        bank_received_total = float(pay_row.BankReceived or 0) if pay_row else 0.0

        cursor.execute(
            f"""
            SELECT
                COALESCE(SUM(
                    CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Cash'
                         THEN pp.Amount ELSE 0 END
                ), 0) AS CashPaid,
                COALESCE(SUM(
                    CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Bank'
                         THEN pp.Amount ELSE 0 END
                ), 0) AS BankPaid
            FROM PurchasePayments pp
            JOIN Purchases p ON p.PurchaseID = pp.PurchaseID
            WHERE pp.PaymentDate < ?
              AND {owner_sql("p")}
            """,
            (until,),
        )
        purchases_row = cursor.fetchone()
        cash_paid_total = float(purchases_row.CashPaid or 0) if purchases_row else 0.0
        bank_paid_total = float(purchases_row.BankPaid or 0) if purchases_row else 0.0

        cash_in_hand = cash_opening + cash_received_total - cash_paid_total
        bank_balance = bank_opening + bank_received_total - bank_paid_total

        cursor.close()

        return render_template(
            "dashboard/index.html",
            total_sales=total_sales,
            total_purchases=total_purchases,
            total_customers=total_customers,
            total_items=total_items,
            total_products=total_items,
            low_stock_items=low_stock_items,
            low_stock_threshold=LOW_STOCK_THRESHOLD,
            out_of_stock_items=out_of_stock_items,
            recent_customers=recent_customers,
            today_cash=today_cash,
            today_bank=today_bank,
            cash_in_hand=cash_in_hand,
            bank_balance=bank_balance,
        )

    except Exception as e:
        flash(f"Error loading dashboard: {str(e)}", "danger")
        return render_template("dashboard/index.html", **defaults)
