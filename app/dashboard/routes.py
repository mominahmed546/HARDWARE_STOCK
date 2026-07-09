from flask import Blueprint, render_template, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection





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

        'recent_customers': [],

    }



    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        cursor.execute("SELECT ISNULL(SUM(TotalAmount), 0) FROM Invoices")

        total_sales = cursor.fetchone()[0] or 0



        cursor.execute("SELECT ISNULL(SUM(TotalAmount), 0) FROM Purchases")

        total_purchases = cursor.fetchone()[0] or 0



        cursor.execute("SELECT COUNT(*) FROM Customers")

        total_customers = cursor.fetchone()[0] or 0



        cursor.execute(

            """

            SELECT COUNT(*) AS ItemCount

            FROM (

                SELECT LOWER(LTRIM(RTRIM(ItemName))) AS ItemKey, CategoryID

                FROM Item

                GROUP BY LOWER(LTRIM(RTRIM(ItemName))), CategoryID

            ) grouped_items

            """

        )

        total_items = cursor.fetchone()[0] or 0



        cursor.execute(

            """

            SELECT TOP 5 ItemName, Qty

            FROM Item

            WHERE Qty <= 10

            ORDER BY Qty ASC

            """

        )

        low_stock_items = cursor.fetchall()



        cursor.execute(

            """

            SELECT TOP 5 CustomerName, ContactNo

            FROM Customers

            ORDER BY CustomerID DESC

            """

        )

        recent_customers = cursor.fetchall()



        cursor.close()



        return render_template(

            'dashboard/index.html',

            total_sales=total_sales,

            total_purchases=total_purchases,

            total_customers=total_customers,

            total_items=total_items,

            total_products=total_items,

            low_stock_items=low_stock_items,

            recent_customers=recent_customers,

        )



    except Exception as e:

        flash(f'Error loading dashboard: {str(e)}', 'danger')

        return render_template('dashboard/index.html', **defaults)

