from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file

from flask_login import login_required

from app import app
from app.db import execute_query, execute_query_one, execute_update, get_db_connection
from app.tenancy import next_owner_no, next_table_id, owner_sql, request_user_id
from app.items.import_utils import build_items_template_xlsx, import_items, parse_items_xlsx
from app.validators import (
    ValidationErrors,
    clean_string,
    clean_optional_select_id,
    clean_positive_decimal,
    clean_positive_int,
)

items_bp = Blueprint("items", __name__, url_prefix="/items")

QTY_FILTERS = {
    "lt10": ("Qty less than 10", " AND COALESCE(i.Qty, 0) < 10"),
    "lt5": ("Qty less than 5", " AND COALESCE(i.Qty, 0) < 5"),
    "eq0": ("Qty equal to 0", " AND COALESCE(i.Qty, 0) = 0"),
    "gt10": ("Qty greater than 10", " AND COALESCE(i.Qty, 0) > 10"),
}


def _validate_item_form(form, errors):
    return {
        "item_name": clean_string(form.get("item_name"), "item_name", errors, max_len=100, label="Item name"),
        "category_id": clean_optional_select_id(form.get("category_id"), "category_id", errors, label="Category"),
        "purchase_rate": clean_positive_decimal(form.get("purchase_rate"), "purchase_rate", errors, label="Purchase rate"),
        "sale_rate": clean_positive_decimal(form.get("sale_rate"), "sale_rate", errors, label="Sale rate"),
        "qty": clean_positive_int(form.get("qty"), "qty", errors, min_val=0, label="Quantity"),
    }


def _check_duplicate_item_name(item_name, errors, exclude_item_id=None):
    """Block creating/renaming an item to a name that already exists for this
    account (case- and whitespace-insensitive). Duplicate item rows track
    stock independently, which silently corrupts anything that reads Qty
    (e.g. Buy Suggestions can call one copy "out of stock" while another
    copy of the same item still has stock on the list page).
    """
    query = f"""
        SELECT ItemID, ItemName FROM Item
        WHERE LOWER(LTRIM(RTRIM(ItemName))) = LOWER(LTRIM(RTRIM(?)))
          AND {owner_sql()}
    """
    params = [item_name]
    if exclude_item_id is not None:
        query += " AND ItemID != ?"
        params.append(exclude_item_id)

    existing = execute_query_one(app, query, tuple(params))
    if existing:
        errors.add(
            "item_name",
            f'An item named "{existing.ItemName}" already exists. Edit that item to change its '
            "quantity/rate instead of creating a duplicate.",
        )


def _find_duplicate_item_names(app):
    """Normalized (lower/trimmed) names that have more than one Item row
    for the current account. Pre-existing duplicates from before the
    create/edit validation was added still need a one-time manual merge.
    """
    rows = execute_query(
        app,
        f"""
        SELECT LOWER(LTRIM(RTRIM(ItemName))) AS NormName
        FROM Item
        WHERE {owner_sql()}
        GROUP BY LOWER(LTRIM(RTRIM(ItemName)))
        HAVING COUNT(*) > 1
        """,
    )
    return [row.NormName for row in rows]


def _duplicate_item_groups(app):
    groups = []
    for norm_name in _find_duplicate_item_names(app):
        rows = execute_query(
            app,
            f"""
            SELECT
                i.ItemID,
                i.ItemName,
                c.CategoryName,
                COALESCE(i.PurchaseRate, 0) AS PurchaseRate,
                COALESCE(i.SaleRate, 0) AS SaleRate,
                COALESCE(i.Qty, 0) AS Qty,
                (SELECT COUNT(*) FROM PurchaseDetails WHERE ItemID = i.ItemID) AS PurchaseLines,
                (SELECT COUNT(*) FROM InvoiceDetails WHERE ItemID = i.ItemID) AS InvoiceLines,
                (SELECT COUNT(*) FROM QuotationDetails WHERE ItemID = i.ItemID) AS QuotationLines
            FROM Item i
            LEFT JOIN Category c ON i.CategoryID = c.CategoryID
            WHERE LOWER(LTRIM(RTRIM(i.ItemName))) = ? AND {owner_sql("i")}
            ORDER BY i.ItemID ASC
            """,
            (norm_name,),
        )
        if len(rows) < 2:
            continue

        total_qty = sum(int(row.Qty or 0) for row in rows)
        suggested_keep_id = max(
            rows,
            key=lambda row: (
                int(row.PurchaseLines or 0) + int(row.InvoiceLines or 0) + int(row.QuotationLines or 0),
                int(row.Qty or 0),
                -int(row.ItemID),
            ),
        ).ItemID

        groups.append(
            {
                "name": rows[0].ItemName,
                # Not "items" -- that shadows dict.items() when accessed as
                # group.items in Jinja (getattr() wins over getitem() there),
                # which silently returns a bound method instead of the rows.
                "rows": rows,
                "total_qty": total_qty,
                "suggested_keep_id": suggested_keep_id,
            }
        )

    return groups


@items_bp.route("/list")
@login_required
def list_items():
    try:
        search = request.args.get("search", "", type=str)
        category_id = request.args.get("category_id", "", type=str)
        qty_filter = request.args.get("qty_filter", "", type=str)
        if qty_filter not in QTY_FILTERS:
            qty_filter = ""

        query = f"""
            SELECT
                i.ItemID,
                COALESCE(i.ItemNo, i.ItemID) AS ItemNo,
                i.ItemName,
                i.CategoryID,
                i.PurchaseRate,
                i.SaleRate,
                i.Qty,
                c.CategoryName
            FROM Item i
            LEFT JOIN Category c ON i.CategoryID = c.CategoryID
            WHERE {owner_sql("i")}
        """

        params = []

        if search:
            # LIKE is case-sensitive on PostgreSQL, unlike SQL Server
            query += " AND LOWER(i.ItemName) LIKE LOWER(?)"
            params.append(f"%{search}%")

        if category_id:
            query += " AND i.CategoryID = ?"
            params.append(int(category_id))

        if qty_filter:
            query += QTY_FILTERS[qty_filter][1]

        query += " ORDER BY COALESCE(i.ItemNo, i.ItemID), i.ItemID"

        items = execute_query(app, query, tuple(params) if params else None)
        categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")
        duplicate_groups_count = len(_find_duplicate_item_names(app))

        return render_template(
            "items/list.html",
            items=items,
            categories=categories,
            search=search,
            category_id=category_id,
            qty_filter=qty_filter,
            qty_filters=QTY_FILTERS,
            duplicate_groups_count=duplicate_groups_count,
        )

    except Exception as e:
        app.logger.exception("Error loading items")
        flash(f"Error loading items: {str(e)}", "danger")
        return render_template(
            "items/list.html",
            items=[],
            categories=[],
            search="",
            category_id="",
            qty_filter="",
            qty_filters=QTY_FILTERS,
            duplicate_groups_count=0,
        )


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
        app.logger.exception("Excel import failed")
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
    categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")

    if request.method == "POST":
        form_data = request.form.to_dict()
        data = _validate_item_form(request.form, errors)

        if errors.valid:
            _check_duplicate_item_name(data["item_name"], errors)

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
            db = get_db_connection(app)
            cursor = db.cursor()
            next_id = next_table_id(cursor, "Item", "ItemID")
            next_no = next_owner_no(cursor, "Item", "ItemNo")
            cursor.execute(
                """
                INSERT INTO Item (ItemID, ItemNo, ItemName, CategoryID, PurchaseRate, SaleRate, Qty, UserID)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_id,
                    next_no,
                    data["item_name"],
                    data["category_id"],
                    data["purchase_rate"],
                    data["sale_rate"],
                    data["qty"],
                    request_user_id(),
                ),
            )
            db.commit()
            cursor.close()

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
        item = execute_query_one(app, f"SELECT * FROM Item WHERE ItemID = ? AND {owner_sql()}", (id,))

        if not item:
            flash("Item not found", "danger")
            return redirect(url_for("items.list_items"))

        categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")

        if request.method == "POST":
            form_data = request.form.to_dict()
            data = _validate_item_form(request.form, errors)

            name_changed = (data.get("item_name") or "").strip().lower() != (item.ItemName or "").strip().lower()
            if errors.valid and name_changed:
                _check_duplicate_item_name(data["item_name"], errors, exclude_item_id=id)

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
                f"""
                UPDATE Item
                SET ItemName = ?, CategoryID = ?, PurchaseRate = ?,
                    SaleRate = ?, Qty = ?
                WHERE ItemID = ? AND {owner_sql()}
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

        execute_update(app, f"DELETE FROM Item WHERE ItemID = ? AND {owner_sql()}", (id,))
        flash("Item deleted successfully", "success")

    except Exception as e:
        flash(f"Error deleting item: {str(e)}", "danger")

    return redirect(url_for("items.list_items"))


@items_bp.route("/duplicates")
@login_required
def duplicate_items():
    try:
        groups = _duplicate_item_groups(app)
    except Exception as e:
        app.logger.exception("Error loading duplicate items")
        flash(f"Error loading duplicate items: {str(e)}", "danger")
        groups = []

    return render_template("items/duplicates.html", groups=groups)


@items_bp.route("/duplicates/merge", methods=["POST"])
@login_required
def merge_duplicate_items():
    try:
        item_ids = [int(x) for x in request.form.get("item_ids", "").split(",") if x.strip().isdigit()]
        keep_id = request.form.get("keep_id", type=int)

        if len(item_ids) < 2 or keep_id not in item_ids:
            flash("Could not merge: pick which item to keep in each group.", "danger")
            return redirect(url_for("items.duplicate_items"))

        db = get_db_connection(app)
        cursor = db.cursor()

        placeholders = ", ".join(["?"] * len(item_ids))
        cursor.execute(
            f"SELECT ItemID, ItemName, COALESCE(Qty, 0) AS Qty FROM Item "
            f"WHERE {owner_sql()} AND ItemID IN ({placeholders})",
            tuple(item_ids),
        )
        rows = {row.ItemID: row for row in cursor.fetchall()}

        # Re-check ownership/membership server-side rather than trusting the
        # posted list, since these ids came straight from hidden form fields.
        if len(rows) != len(item_ids) or keep_id not in rows:
            cursor.close()
            flash("Could not merge: some items were not found.", "danger")
            return redirect(url_for("items.duplicate_items"))

        merge_ids = [i for i in item_ids if i != keep_id]
        total_qty = sum(int(row.Qty or 0) for row in rows.values())

        for other_id in merge_ids:
            cursor.execute("UPDATE PurchaseDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
            cursor.execute("UPDATE InvoiceDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
            cursor.execute("UPDATE QuotationDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
            cursor.execute("UPDATE StockHistory SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
            cursor.execute(f"DELETE FROM Item WHERE ItemID = ? AND {owner_sql()}", (other_id,))

        cursor.execute(f"UPDATE Item SET Qty = ? WHERE ItemID = ? AND {owner_sql()}", (total_qty, keep_id))

        db.commit()
        cursor.close()

        flash(
            f'Merged {len(merge_ids)} duplicate row(s) into "{rows[keep_id].ItemName}". '
            f"Combined quantity: {total_qty}.",
            "success",
        )

    except Exception as e:
        app.logger.exception("Error merging duplicate items")
        flash(f"Error merging items: {str(e)}", "danger")

    return redirect(url_for("items.duplicate_items"))
