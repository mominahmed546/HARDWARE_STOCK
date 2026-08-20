from flask import Blueprint, render_template, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection
from app.cash.routes import _cash_excluding_profit
from app.payments import (
    ensure_cash_accounts,
    ensure_invoice_payments_table,
    ensure_purchase_payments_table,
    get_cash_openings,
)
from app.tenancy import owner_sql
from datetime import date





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
        'cash_excluding_profit': 0,
        'profit_in_cash': 0,
    }



    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        cursor.execute(f"SELECT ISNULL(SUM(TotalAmount), 0) FROM Invoices WHERE {owner_sql()}")

        total_sales = cursor.fetchone()[0] or 0



        cursor.execute(f"SELECT ISNULL(SUM(TotalAmount), 0) FROM Purchases WHERE {owner_sql()}")

        total_purchases = cursor.fetchone()[0] or 0



        cursor.execute(f"SELECT COUNT(*) FROM Customers WHERE {owner_sql()}")

        total_customers = cursor.fetchone()[0] or 0



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

            SELECT TOP 5 MIN(ItemName) AS ItemName, SUM(Qty) AS Qty

            FROM Item
            WHERE {owner_sql()}
            GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID
            HAVING SUM(Qty) <= 0

            ORDER BY MIN(ItemName) ASC

            """

        )

        out_of_stock_items = cursor.fetchall()



        cursor.execute(

            f"""

            SELECT TOP 5 MIN(ItemName) AS ItemName, SUM(Qty) AS Qty

            FROM Item
            WHERE {owner_sql()}
            GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID
            HAVING SUM(Qty) > 0 AND SUM(Qty) < 10

            ORDER BY SUM(Qty) ASC

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
        cursor.execute(
            f"""
            SELECT
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Bank' THEN Amount ELSE 0 END), 0) AS BankAmount,
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Cash' THEN Amount ELSE 0 END), 0) AS CashAmount
            FROM InvoicePayments
            WHERE CAST(PaymentDate AS DATE) = CURRENT_DATE
              AND InvoiceID IN (SELECT InvoiceID FROM Invoices WHERE {owner_sql()})
            """,
        )
        today_row = cursor.fetchone()
        today_cash = float(today_row.CashAmount or 0) if today_row else 0
        today_bank = float(today_row.BankAmount or 0) if today_row else 0
        cursor.execute(
            f"""
            SELECT
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Bank' THEN Amount ELSE 0 END), 0) AS BankAmount,
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Cash' THEN Amount ELSE 0 END), 0) AS CashAmount
            FROM InvoicePayments
            WHERE CAST(PaymentDate AS DATE) <= CURRENT_DATE
              AND InvoiceID IN (SELECT InvoiceID FROM Invoices WHERE {owner_sql()})
            """,
        )
        all_row = cursor.fetchone()
        cash_received_total = float(all_row.CashAmount or 0) if all_row else 0
        bank_received_total = float(all_row.BankAmount or 0) if all_row else 0
        cursor.execute(
            f"""
            SELECT
                ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Bank' THEN pp.Amount ELSE 0 END), 0) AS BankPaid,
                ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Cash' THEN pp.Amount ELSE 0 END), 0) AS CashPaid
            FROM PurchasePayments pp
            JOIN Purchases p ON p.PurchaseID = pp.PurchaseID
            WHERE CAST(pp.PaymentDate AS DATE) <= CURRENT_DATE
              AND {owner_sql("p")}
            """,
        )
        purchases_row = cursor.fetchone()
        cash_paid_total = float(purchases_row.CashPaid or 0) if purchases_row else 0
        bank_paid_total = float(purchases_row.BankPaid or 0) if purchases_row else 0
        cash_in_hand = cash_opening + cash_received_total - cash_paid_total
        bank_balance = bank_opening + bank_received_total - bank_paid_total
        cash_excluding_profit, profit_in_cash, _capital = _cash_excluding_profit(
            cursor, date.today(), cash_opening, cash_in_hand
        )

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
            cash_in_hand=cash_in_hand,
            bank_balance=bank_balance,
            cash_excluding_profit=cash_excluding_profit,
            profit_in_cash=profit_in_cash,
        )



    except Exception as e:

        flash(f'Error loading dashboard: {str(e)}', 'danger')

        return render_template('dashboard/index.html', **defaults)

