from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file

from flask_login import login_required

from app import app
from app.db import execute_query, execute_query_one, execute_update
from app.items.import_utils import build_items_template_xlsx, import_items, parse_items_xlsx
from app.validators import (
    ValidationErrors,
    clean_string,
    clean_optional_select_id,
    clean_positive_decimal,
    clean_positive_int,
)

items_bp = Blueprint("items", __name__, url_prefix="/items")


def _validate_item_form(form, errors):
    return {
        "item_name": clean_string(form.get("item_name"), "item_name", errors, max_len=100, label="Item name"),
        "category_id": clean_optional_select_id(form.get("category_id"), "category_id", errors, label="Category"),
        "purchase_rate": clean_positive_decimal(form.get("purchase_rate"), "purchase_rate", errors, label="Purchase rate"),
        "sale_rate": clean_positive_decimal(form.get("sale_rate"), "sale_rate", errors, label="Sale rate"),
        "qty": clean_positive_int(form.get("qty"), "qty", errors, min_val=0, label="Quantity"),
    }


@items_bp.route("/list")
@login_required
def list_items():
    try:
        search = request.args.get("search", "", type=str)
        category_id = request.args.get("category_id", "", type=str)

        query = """
            SELECT
                COALESCE(
                    MIN(CASE WHEN i.Qty > 0 THEN i.ItemID END),
                    MIN(CASE WHEN i.PurchaseRate > 0 OR i.SaleRate > 0 THEN i.ItemID END),
                    MIN(i.ItemID)
                ) AS ItemID,
                MIN(i.ItemName) AS ItemName,
                i.CategoryID,
                MAX(i.PurchaseRate) AS PurchaseRate,
                MAX(i.SaleRate) AS SaleRate,
                SUM(i.Qty) AS Qty,
                c.CategoryName
            FROM Item i
            LEFT JOIN Category c ON i.CategoryID = c.CategoryID
            WHERE 1=1
        """

        params = []

        if search:
            query += " AND i.ItemName LIKE ?"
            params.append(f"%{search}%")

        if category_id:
            query += " AND i.CategoryID = ?"
            params.append(int(category_id))

        query += """
            GROUP BY LOWER(LTRIM(RTRIM(i.ItemName))), i.CategoryID, c.CategoryName
            ORDER BY MIN(i.ItemName), c.CategoryName
        """

        items = execute_query(app, query, tuple(params) if params else None)
        categories = execute_query(app, "SELECT * FROM Category ORDER BY CategoryName")

        return render_template(
            "items/list.html",
            items=items,
            categories=categories,
            search=search,
            category_id=category_id,
        )

    except Exception as e:
        flash(f"Error loading items: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))


ALLOWED_UPLOAD_REDIRECTS = {
    "purchases.create_purchase",
    "items.list_items",
}


def _upload_redirect():
    target = request.form.get("redirect_to", "purchases.create_purchase")
    if target not in ALLOWED_UPLOAD_REDIRECTS:
        target = "purchases.create_purchase"
    return redirect(url_for(target))


@items_bp.route("/upload", methods=["POST"])
@login_required
def upload_items():
    upload_file = request.files.get("file")

    if not upload_file or not upload_file.filename:
        flash("Please select an Excel file to upload.", "danger")
        return _upload_redirect()

    if not upload_file.filename.lower().endswith(".xlsx"):
        flash("Only .xlsx files are supported.", "danger")
        return _upload_redirect()

    try:
        valid_items, row_errors = parse_items_xlsx(upload_file.stream)
        inserted, updated, import_errors = import_items(app, valid_items)
        row_errors.extend(import_errors)

        flash(f"Import complete: {inserted} new item(s), {updated} existing item(s) updated.", "success")

        if row_errors:
            preview = "; ".join(row_errors[:3])
            extra = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
            flash(f"{len(row_errors)} row(s) skipped: {preview}{extra}", "danger")

    except Exception as e:
        flash(f"Import failed: {str(e)}", "danger")

    return _upload_redirect()


@items_bp.route("/template")
@login_required
def download_items_template():
    template_file = build_items_template_xlsx()
    return send_file(
        template_file,
        as_attachment=True,
        download_name="items_import_template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@items_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_item():
    errors = ValidationErrors()
    form_data = {}
    categories = execute_query(app, "SELECT * FROM Category ORDER BY CategoryName")

    if request.method == "POST":
        form_data = request.form.to_dict()
        data = _validate_item_form(request.form, errors)

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template(
                "items/form.html",
                item=None,
                categories=categories,
                errors=errors.errors,
                form_data=form_data,
            )

        try:
            execute_update(
                app,
                """
                INSERT INTO Item (ItemName, CategoryID, PurchaseRate, SaleRate, Qty)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    data["item_name"],
                    data["category_id"],
                    data["purchase_rate"],
                    data["sale_rate"],
                    data["qty"],
                ),
            )

            flash("Item created successfully", "success")
            return redirect(url_for("items.list_items"))

        except Exception as e:
            flash(f"Error creating item: {str(e)}", "danger")

    return render_template(
        "items/form.html",
        item=None,
        categories=categories,
        errors=errors.errors,
        form_data=form_data,
    )


@items_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_item(id):
    errors = ValidationErrors()
    form_data = {}

    try:
        item = execute_query_one(app, "SELECT * FROM Item WHERE ItemID = ?", (id,))

        if not item:
            flash("Item not found", "danger")
            return redirect(url_for("items.list_items"))

        categories = execute_query(app, "SELECT * FROM Category ORDER BY CategoryName")

        if request.method == "POST":
            form_data = request.form.to_dict()
            data = _validate_item_form(request.form, errors)

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "items/form.html",
                    item=item,
                    categories=categories,
                    errors=errors.errors,
                    form_data=form_data,
                )

            execute_update(
                app,
                """
                UPDATE Item
                SET ItemName = ?, CategoryID = ?, PurchaseRate = ?,
                    SaleRate = ?, Qty = ?
                WHERE ItemID = ?
                """,
                (
                    data["item_name"],
                    data["category_id"],
                    data["purchase_rate"],
                    data["sale_rate"],
                    data["qty"],
                    id,
                ),
            )

            flash("Item updated successfully", "success")
            return redirect(url_for("items.list_items"))

        return render_template(
            "items/form.html",
            item=item,
            categories=categories,
            errors=errors.errors,
            form_data=form_data,
        )

    except Exception as e:
        flash(f"Error: {str(e)}", "danger")
        return redirect(url_for("items.list_items"))


@items_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_item(id):
    try:
        usage = execute_query_one(
            app,
            """
            SELECT
                (SELECT COUNT(*) FROM InvoiceDetails WHERE ItemID = ?) AS InvoiceCount,
                (SELECT COUNT(*) FROM PurchaseDetails WHERE ItemID = ?) AS PurchaseCount
            """,
            (id, id),
        )

        invoice_count = usage.InvoiceCount if usage else 0
        purchase_count = usage.PurchaseCount if usage else 0

        if invoice_count or purchase_count:
            flash(
                "This item cannot be deleted because it is already used in "
                f"{invoice_count} invoice detail(s) and {purchase_count} purchase detail(s).",
                "danger",
            )
            return redirect(url_for("items.list_items"))

        execute_update(app, "DELETE FROM Item WHERE ItemID = ?", (id,))
        flash("Item deleted successfully", "success")

    except Exception as e:
        flash(f"Error deleting item: {str(e)}", "danger")

    return redirect(url_for("items.list_items"))
