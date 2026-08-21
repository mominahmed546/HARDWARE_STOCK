from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection
from app.perf import pagination_meta, parse_page, parse_page_size
from app.tenancy import owner_sql, request_user_id

from app.validators import ValidationErrors, clean_string, clean_phone, clean_positive_decimal



customers_bp = Blueprint('customers', __name__, url_prefix='/customers')
_CUSTOMERS_SCHEMA_READY = False





def _ensure_previous_balance_column(db, cursor):
    global _CUSTOMERS_SCHEMA_READY
    if _CUSTOMERS_SCHEMA_READY:
        return
    cursor.execute(
        """
        ALTER TABLE Customers
        ADD COLUMN IF NOT EXISTS PreviousBalance NUMERIC(12, 2) DEFAULT 0
        """
    )
    db.commit()
    _CUSTOMERS_SCHEMA_READY = True


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


def _validate_customer_form(form, errors):

    return {

        "customer_name": clean_string(

            form.get("customer_name"), "customer_name", errors, max_len=50, label="Customer name"

        ),

        "contact_no": clean_phone(form.get("contact_no"), "contact_no", errors, required=False),

    }





@customers_bp.route('/list')

@login_required

def list_customers():

    try:

        db = get_db_connection(app)

        cursor = db.cursor()

        _ensure_previous_balance_column(db, cursor)



        search = request.args.get('search', '')
        page = parse_page(request.args.get("page"))
        page_size = parse_page_size(request.args.get("page_size"))

        where_sql = f"WHERE {owner_sql()}"
        params = []

        if search:
            where_sql += " AND CustomerName LIKE ?"
            params.append(f"%{search}%")

        cursor.execute(
            f"SELECT COUNT(*) AS TotalCount FROM Customers {where_sql}",
            params or (),
        )
        pagination = pagination_meta(int(cursor.fetchone().TotalCount or 0), page, page_size)

        query = f"""
            SELECT
                CustomerID,
                CustomerName,
                ContactNo,
                COALESCE(PreviousBalance, 0) AS PreviousBalance
            FROM Customers
            {where_sql}
            ORDER BY CustomerName
            LIMIT ? OFFSET ?
        """

        cursor.execute(
            query,
            params + [pagination["page_size"], pagination["offset"]],
        )

        customers = cursor.fetchall()

        cursor.close()



        return render_template(

            'customers/list.html',

            customers=customers,

            search=search,

            pagination=pagination,

        )



    except Exception as e:

        flash(f'Error loading customers: {str(e)}', 'danger')

        return redirect(url_for('dashboard.dashboard'))





@customers_bp.route('/create', methods=['GET', 'POST'])

@login_required

def create_customer():

    errors = ValidationErrors()

    form_data = {}



    if request.method == 'POST':

        form_data = request.form.to_dict()

        data = _validate_customer_form(request.form, errors)



        if not errors.valid:

            flash(errors.first(), 'danger')

            return render_template(

                'customers/form.html',

                customer=None,

                errors=errors.errors,

                form_data=form_data,

            )



        try:

            db = get_db_connection(app)

            cursor = db.cursor()

            _ensure_previous_balance_column(db, cursor)

            previous_balance = clean_positive_decimal(
                request.form.get("previous_balance"),
                "previous_balance",
                errors,
                required=False,
                min_val=0,
                label="Previous balance",
            )

            if previous_balance is None:
                previous_balance = 0

            if not errors.valid:
                flash(errors.first(), "danger")
                cursor.close()
                return render_template(
                    "customers/form.html",
                    customer=None,
                    errors=errors.errors,
                    form_data=form_data,
                )



            cursor.execute(

                "INSERT INTO Customers (CustomerName, ContactNo, PreviousBalance, UserID) VALUES (?, ?, ?, ?)",

                (data["customer_name"], data["contact_no"], previous_balance, request_user_id())

            )



            db.commit()

            cursor.close()



            flash('Customer created successfully', 'success')

            return redirect(url_for('customers.list_customers'))



        except Exception as e:

            flash(f'Error creating customer: {str(e)}', 'danger')



    return render_template(

        'customers/form.html',

        customer=None,

        errors=errors.errors,

        form_data=form_data,

    )





@customers_bp.route('/edit/<int:id>', methods=['GET', 'POST'])

@login_required

def edit_customer(id):

    errors = ValidationErrors()

    form_data = {}



    try:

        db = get_db_connection(app)

        cursor = db.cursor()

        _ensure_previous_balance_column(db, cursor)



        cursor.execute(f"SELECT * FROM Customers WHERE CustomerID = ? AND {owner_sql()}", (id,))

        customer = cursor.fetchone()



        if not customer:

            flash('Customer not found', 'danger')

            return redirect(url_for('customers.list_customers'))



        if request.method == 'POST':

            form_data = request.form.to_dict()

            data = _validate_customer_form(request.form, errors)

            previous_balance = clean_positive_decimal(
                request.form.get("previous_balance"),
                "previous_balance",
                errors,
                required=False,
                min_val=0,
                label="Previous balance",
            )

            if previous_balance is None:
                previous_balance = 0



            if not errors.valid:

                flash(errors.first(), 'danger')

                return render_template(

                    'customers/form.html',

                    customer=customer,

                    errors=errors.errors,

                    form_data=form_data,

                )



            cursor.execute(

                f"UPDATE Customers SET CustomerName = ?, ContactNo = ?, PreviousBalance = ? WHERE CustomerID = ? AND {owner_sql()}",

                (data["customer_name"], data["contact_no"], previous_balance, id)

            )



            db.commit()

            cursor.close()



            flash('Customer updated successfully', 'success')

            return redirect(url_for('customers.list_customers'))



        cursor.close()



        return render_template(

            'customers/form.html',

            customer=customer,

            errors=errors.errors,

            form_data=form_data,

        )



    except Exception as e:

        flash(f'Error: {str(e)}', 'danger')

        return redirect(url_for('customers.list_customers'))





@customers_bp.route('/delete/<int:id>', methods=['POST'])

@login_required

def delete_customer(id):

    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        cursor.execute(f"DELETE FROM Customers WHERE CustomerID = ? AND {owner_sql()}", (id,))

        db.commit()

        cursor.close()



        flash('Customer deleted successfully', 'success')



    except Exception as e:

        flash(f'Error deleting customer: {str(e)}', 'danger')



    return redirect(url_for('customers.list_customers'))


@customers_bp.route('/<int:id>/ledger')
@login_required
def customer_ledger(id):
    return redirect(url_for("ledger.customer_ledger", id=id))

