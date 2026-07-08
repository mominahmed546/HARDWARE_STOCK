from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection

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



        query = "SELECT * FROM Supplier WHERE 1=1"

        params = []



        if search:

            query += " AND SupplierName LIKE ?"

            params.append(f"%{search}%")



        query += " ORDER BY SupplierName"



        cursor.execute(query, params or ())

        suppliers = cursor.fetchall()

        cursor.close()



        return render_template(

            "suppliers/list.html",

            suppliers=suppliers,

            search=search,

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

                "INSERT INTO Supplier (SupplierName, ContactNo) VALUES (?, ?)",

                (data["supplier_name"], data["contact_no"]),

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



        cursor.execute("SELECT * FROM Supplier WHERE SupplierID = ?", (id,))

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

                "UPDATE Supplier SET SupplierName = ?, ContactNo = ? WHERE SupplierID = ?",

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



        cursor.execute("DELETE FROM Supplier WHERE SupplierID = ?", (id,))

        db.commit()

        cursor.close()



        flash("Supplier deleted successfully", "success")



    except Exception as e:

        flash(f"Error deleting supplier: {str(e)}", "danger")



    return redirect(url_for("suppliers.list_suppliers"))

