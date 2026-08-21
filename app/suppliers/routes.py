from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection
from app.perf import pagination_meta, parse_page, parse_page_size
from app.tenancy import owner_sql, request_user_id

from app.validators import ValidationErrors, clean_string, clean_phone



suppliers_bp = Blueprint("suppliers", __name__, url_prefix="/suppliers")





def _validate_supplier_form(form, errors):

    return {

        "supplier_name": clean_string(

            form.get("supplier_name"), "supplier_name", errors, max_len=60, label="Supplier name"

        ),

        "contact_no": clean_phone(form.get("contact_no"), "contact_no", errors, required=False),

    }





@suppliers_bp.route("/list", methods=["GET", "POST"])

@login_required

def list_suppliers():

    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        search = request.args.get("search", "")
        page = parse_page(request.args.get("page"))
        page_size = parse_page_size(request.args.get("page_size"))

        where_sql = f"WHERE {owner_sql()}"
        params = []

        if search:
            where_sql += " AND SupplierName LIKE ?"
            params.append(f"%{search}%")

        cursor.execute(
            f"SELECT COUNT(*) AS TotalCount FROM Supplier {where_sql}",
            params or (),
        )
        pagination = pagination_meta(int(cursor.fetchone().TotalCount or 0), page, page_size)

        query = f"""
            SELECT *
            FROM Supplier
            {where_sql}
            ORDER BY SupplierName
            LIMIT ? OFFSET ?
        """
        cursor.execute(
            query,
            params + [pagination["page_size"], pagination["offset"]],
        )
        suppliers = cursor.fetchall()

        cursor.close()



        return render_template(

            "suppliers/list.html",

            suppliers=suppliers,

            search=search,

            pagination=pagination,

        )



    except Exception as e:

        flash(f"Error loading suppliers: {str(e)}", "danger")

        return redirect(url_for("dashboard.dashboard"))





@suppliers_bp.route("/create", methods=["GET", "POST"])

@login_required

def create_supplier():

    errors = ValidationErrors()

    form_data = {}



    if request.method == "POST":

        form_data = request.form.to_dict()

        data = _validate_supplier_form(request.form, errors)



        if not errors.valid:

            flash(errors.first(), "danger")

            return render_template(

                "suppliers/form.html",

                supplier=None,

                errors=errors.errors,

                form_data=form_data,

            )



        try:

            db = get_db_connection(app)

            cursor = db.cursor()



            cursor.execute(

                "INSERT INTO Supplier (SupplierName, ContactNo, UserID) VALUES (?, ?, ?)",

                (data["supplier_name"], data["contact_no"], request_user_id()),

            )



            db.commit()

            cursor.close()



            flash("Supplier created successfully", "success")

            return redirect(url_for("suppliers.list_suppliers"))



        except Exception as e:

            flash(f"Error creating supplier: {str(e)}", "danger")



    return render_template(

        "suppliers/form.html",

        supplier=None,

        errors=errors.errors,

        form_data=form_data,

    )





@suppliers_bp.route("/edit/<int:id>", methods=["GET", "POST"])

@login_required

def edit_supplier(id):

    errors = ValidationErrors()

    form_data = {}



    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        cursor.execute(f"SELECT * FROM Supplier WHERE SupplierID = ? AND {owner_sql()}", (id,))

        supplier = cursor.fetchone()



        if not supplier:

            flash("Supplier not found", "danger")

            return redirect(url_for("suppliers.list_suppliers"))



        if request.method == "POST":

            form_data = request.form.to_dict()

            data = _validate_supplier_form(request.form, errors)



            if not errors.valid:

                flash(errors.first(), "danger")

                return render_template(

                    "suppliers/form.html",

                    supplier=supplier,

                    errors=errors.errors,

                    form_data=form_data,

                )



            cursor.execute(

                f"UPDATE Supplier SET SupplierName = ?, ContactNo = ? WHERE SupplierID = ? AND {owner_sql()}",

                (data["supplier_name"], data["contact_no"], id),

            )



            db.commit()

            cursor.close()



            flash("Supplier updated successfully", "success")

            return redirect(url_for("suppliers.list_suppliers"))



        cursor.close()



        return render_template(

            "suppliers/form.html",

            supplier=supplier,

            errors=errors.errors,

            form_data=form_data,

        )



    except Exception as e:

        flash(f"Error: {str(e)}", "danger")

        return redirect(url_for("suppliers.list_suppliers"))





@suppliers_bp.route("/delete/<int:id>", methods=["POST"])

@login_required

def delete_supplier(id):

    try:

        db = get_db_connection(app)

        cursor = db.cursor()



        cursor.execute(f"SELECT COUNT(*) FROM Purchases WHERE SupplierID = ? AND {owner_sql()}", (id,))

        if cursor.fetchone()[0] > 0:

            cursor.close()

            flash("Cannot delete supplier that already has purchases recorded against it.", "danger")

            return redirect(url_for("suppliers.list_suppliers"))



        cursor.execute(f"DELETE FROM Supplier WHERE SupplierID = ? AND {owner_sql()}", (id,))

        db.commit()

        cursor.close()



        flash("Supplier deleted successfully", "success")



    except Exception as e:

        flash(f"Error deleting supplier: {str(e)}", "danger")



    return redirect(url_for("suppliers.list_suppliers"))

