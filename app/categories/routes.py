from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.validators import ValidationErrors, clean_string

categories_bp = Blueprint("categories", __name__, url_prefix="/categories")


def _validate_category_form(form, errors, is_edit=False):
    return {
        "category_name": clean_string(
            form.get("category_name"), "category_name", errors, max_len=50, label="Category name"
        ),
    }


@categories_bp.route("/list")
@login_required
def list_categories():
    try:
        db = get_db_connection(app)
        cursor = db.cursor()

        search = request.args.get("search", "")

        query = "SELECT * FROM Category WHERE 1=1"
        params = []

        if search:
            query += " AND CategoryName LIKE ?"
            params.append(f"%{search}%")

        query += " ORDER BY CategoryName"

        cursor.execute(query, params or ())
        categories = cursor.fetchall()
        cursor.close()

        return render_template(
            "categories/list.html",
            categories=categories,
            search=search,
        )

    except Exception as e:
        flash(f"Error loading categories: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))


@categories_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_category():
    errors = ValidationErrors()
    form_data = {}

    if request.method == "POST":
        form_data = request.form.to_dict()
        data = _validate_category_form(request.form, errors)

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template(
                "categories/form.html",
                category=None,
                errors=errors.errors,
                form_data=form_data,
            )

        try:
            db = get_db_connection(app)
            cursor = db.cursor()

            cursor.execute("SELECT ISNULL(MAX(CategoryID), 0) + 1 FROM Category")
            next_category_id = cursor.fetchone()[0]

            cursor.execute(
                "INSERT INTO Category (CategoryID, CategoryName) VALUES (?, ?)",
                (next_category_id, data["category_name"]),
            )
            db.commit()
            cursor.close()

            flash("Category created successfully", "success")
            return redirect(url_for("categories.list_categories"))

        except Exception as e:
            flash(f"Error creating category: {str(e)}", "danger")

    return render_template(
        "categories/form.html",
        category=None,
        errors=errors.errors,
        form_data=form_data,
    )


@categories_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_category(id):
    errors = ValidationErrors()
    form_data = {}

    try:
        db = get_db_connection(app)
        cursor = db.cursor()

        cursor.execute("SELECT * FROM Category WHERE CategoryID = ?", (id,))
        category = cursor.fetchone()

        if not category:
            flash("Category not found", "danger")
            return redirect(url_for("categories.list_categories"))

        if request.method == "POST":
            form_data = request.form.to_dict()
            data = _validate_category_form(request.form, errors, is_edit=True)

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "categories/form.html",
                    category=category,
                    errors=errors.errors,
                    form_data=form_data,
                )

            cursor.execute(
                "UPDATE Category SET CategoryName = ? WHERE CategoryID = ?",
                (data["category_name"], id),
            )
            db.commit()
            cursor.close()

            flash("Category updated successfully", "success")
            return redirect(url_for("categories.list_categories"))

        cursor.close()

        return render_template(
            "categories/form.html",
            category=category,
            errors=errors.errors,
            form_data=form_data,
        )

    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
        return redirect(url_for("categories.list_categories"))


@categories_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_category(id):
    try:
        db = get_db_connection(app)
        cursor = db.cursor()

        cursor.execute("SELECT COUNT(*) FROM Item WHERE CategoryID = ?", (id,))
        if cursor.fetchone()[0] > 0:
            flash("Cannot delete category that is used by items.", "danger")
            return redirect(url_for("categories.list_categories"))

        cursor.execute("DELETE FROM Category WHERE CategoryID = ?", (id,))
        db.commit()
        cursor.close()

        flash("Category deleted successfully", "success")

    except Exception as e:
        flash(f"Error deleting category: {str(e)}", "danger")

    return redirect(url_for("categories.list_categories"))
