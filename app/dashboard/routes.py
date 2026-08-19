from flask import Blueprint, render_template, flash

from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.payments import ensure_cash_accounts, ensure_invoice_payments_table, ensure_purchase_payments_table, get_cash_openings
from app.perf import fetch_drawer_balances, fetch_invoice_payment_totals
from app.tenancy import owner_sql

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/dashboard')
@login_required
def dashboard():
    defaults = {
        'total_sales': 0,
        'total_purchases': 0,
        'total_customers': 0,
        'total_items': 0,
        'total_products': 0,
        'low_stock_items': [],
        'out_of_stock_items': [],
        'recent_customers': [],
        'today_cash': 0,
        'today_bank': 0,
        'cash_in_hand': 0,
        'bank_balance': 0,
    }

    try:
        db = get_db_connection(app)
        cursor = db.cursor()

        cursor.execute(
            f"""
            SELECT
                (SELECT COALESCE(SUM(TotalAmount), 0) FROM Invoices WHERE {owner_sql()}) AS TotalSales,
                (SELECT COALESCE(SUM(TotalAmount), 0) FROM Purchases WHERE {owner_sql()}) AS TotalPurchases,
                (SELECT COUNT(*) FROM Customers WHERE {owner_sql()}) AS TotalCustomers
            """
        )
        summary = cursor.fetchone()
        total_sales = summary.TotalSales or 0
        total_purchases = summary.TotalPurchases or 0
        total_customers = summary.TotalCustomers or 0

        cursor.execute(
            f"""
            SELECT COUNT(*) AS ItemCount
            FROM (
                SELECT LOWER(LTRIM(RTRIM(ItemName))) AS ItemKey, CategoryID
                FROM Item
                WHERE {owner_sql()}
                GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID
            ) grouped_items
            """
        )
        total_items = cursor.fetchone()[0] or 0

        cursor.execute(
            f"""
            WITH grouped AS (
                SELECT
                    MIN(ItemName) AS ItemName,
                    SUM(Qty) AS Qty
                FROM Item
                WHERE {owner_sql()}
                GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID
            )
            SELECT 'out' AS kind, ItemName, Qty
            FROM grouped
            WHERE Qty <= 0
            ORDER BY ItemName ASC
            LIMIT 5
            """
        )
        out_of_stock_items = cursor.fetchall()

        cursor.execute(
            f"""
            WITH grouped AS (
                SELECT
                    MIN(ItemName) AS ItemName,
                    SUM(Qty) AS Qty
                FROM Item
                WHERE {owner_sql()}
                GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID
            )
            SELECT 'low' AS kind, ItemName, Qty
            FROM grouped
            WHERE Qty > 0 AND Qty < 10
            ORDER BY Qty ASC
            LIMIT 5
            """
        )
        low_stock_items = cursor.fetchall()

        cursor.execute(
            f"""
            SELECT TOP 5 CustomerName, ContactNo
            FROM Customers
            WHERE {owner_sql()}
            ORDER BY CustomerID DESC
            """
        )
        recent_customers = cursor.fetchall()

        ensure_invoice_payments_table(db, cursor)
        ensure_cash_accounts(db, cursor)
        ensure_purchase_payments_table(db, cursor)
        cash_opening, bank_opening = get_cash_openings(cursor)
        today_cash, today_bank = fetch_invoice_payment_totals(cursor, today_only=True)
        balances = fetch_drawer_balances(cursor, cash_opening, bank_opening)

        cursor.close()

        return render_template(
            'dashboard/index.html',
            total_sales=total_sales,
            total_purchases=total_purchases,
            total_customers=total_customers,
            total_items=total_items,
            total_products=total_items,
            low_stock_items=low_stock_items,
            out_of_stock_items=out_of_stock_items,
            recent_customers=recent_customers,
            today_cash=today_cash,
            today_bank=today_bank,
            cash_in_hand=balances["cash_in_hand"],
            bank_balance=balances["bank_balance"],
        )

    except Exception as e:
        flash(f'Error loading dashboard: {str(e)}', 'danger')
        return render_template('dashboard/index.html', **defaults)
