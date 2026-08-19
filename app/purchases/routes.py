from flask import Blueprint, render_template, request, redirect, url_for, flash

from flask_login import login_required



from app import app

from app.db import get_db_connection
from app.payments import ensure_cash_accounts, ensure_invoice_payments_table, get_cash_openings
from app.tenancy import next_owner_no, next_table_id, owner_sql, request_user_id

from app.validators import (

    ValidationErrors,

    clean_date,

    clean_select_id,

    clean_string,

    clean_positive_int,

    clean_positive_decimal,

)



purchases_bp = Blueprint("purchases", __name__, url_prefix="/purchases")


def normalize_purchase_payment_method(value):
    method = str(value or "").strip().lower()
    if method == "bank":
        return "Bank"
    if method == "credit":
        return "Credit"
    return "Cash"


def _ensure_purchase_payment_method_column(db, cursor):
    cursor.execute(
        """
        ALTER TABLE Purchases
        ADD COLUMN IF NOT EXISTS PaymentMethod VARCHAR(20) DEFAULT 'Cash'
        """
    )
    cursor.execute(
        """
        UPDATE Purchases
        SET PaymentMethod = 'Cash'
        WHERE PaymentMethod IS NULL OR BTRIM(PaymentMethod) = ''
        """
    )
    db.commit()





def _validate_purchase_form(form, errors):
    item_mode = (form.get("item_mode") or "existing").strip()

    if item_mode not in {"existing", "new"}:
        errors.add("item_mode", "Select whether this is an existing or new item.")

    data = {
        "purchase_date": clean_date(form.get("purchase_date"), "purchase_date", errors, label="Purchase date"),
        "supplier_id": clean_select_id(form.get("supplier_id"), "supplier_id", errors, label="Supplier"),
        "item_mode": item_mode,
        "quantity": clean_positive_int(form.get("quantity"), "quantity", errors, min_val=1, label="Quantity"),
        "purchase_rate": clean_positive_decimal(form.get("purchase_rate"), "purchase_rate", errors, label="Purchase rate"),
        "sale_rate": clean_positive_decimal(form.get("sale_rate"), "sale_rate", errors, label="Sale rate"),
        "payment_method": normalize_purchase_payment_method(form.get("payment_method")),
    }

    if item_mode == "existing":
        data["item_id"] = clean_select_id(form.get("item_id"), "item_id", errors, label="Item")
        data["item_name"] = None
        data["category_id"] = None
    else:
        data["item_id"] = None
        data["item_name"] = clean_string(form.get("item_name"), "item_name", errors, max_len=100, label="Item name")
        data["category_id"] = clean_select_id(form.get("category_id"), "category_id", errors, label="Category")

    return data





def _load_purchase_form_data(cursor):
    cursor.execute(f"SELECT SupplierID, SupplierName FROM Supplier WHERE {owner_sql()}")
    suppliers = cursor.fetchall()

    cursor.execute(
        f"""
        SELECT
            COALESCE(
                MIN(CASE WHEN i.Qty > 0 THEN i.ItemID END),
                MIN(CASE WHEN i.PurchaseRate > 0 OR i.SaleRate > 0 THEN i.ItemID END),
                MIN(i.ItemID)
            ) AS ItemID,
            MIN(i.ItemName) AS ItemName,
            i.CategoryID,
            c.CategoryName,
            MAX(i.PurchaseRate) AS PurchaseRate,
            MAX(i.SaleRate) AS SaleRate,
            SUM(i.Qty) AS Qty
        FROM Item i
        LEFT JOIN Category c ON i.CategoryID = c.CategoryID
        WHERE {owner_sql("i")}
        GROUP BY LOWER(LTRIM(RTRIM(i.ItemName))), i.CategoryID, c.CategoryName
        ORDER BY MIN(i.ItemName), c.CategoryName
        """
    )
    items = cursor.fetchall()

    cursor.execute(f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")
    categories = cursor.fetchall()

    return suppliers, items, categories





@purchases_bp.route("/create", methods=["GET", "POST"])

@login_required

def create_purchase():

    db = get_db_connection(app)

    cursor = db.cursor()

    errors = ValidationErrors()

    form_data = {}



    try:
        _ensure_purchase_payment_method_column(db, cursor)

        suppliers, items, categories = _load_purchase_form_data(cursor)



        if request.method == "POST":

            form_data = request.form.to_dict()

            data = _validate_purchase_form(request.form, errors)



            if not errors.valid:

                flash(errors.first(), "danger")

                return render_template(

                    "purchases/form.html",

                    suppliers=suppliers,

                    items=items,

                    categories=categories,

                    errors=errors.errors,

                    form_data=form_data,

                )



            total = data["quantity"] * data["purchase_rate"]

            if data["item_mode"] == "existing":
                cursor.execute(
                    f"SELECT ItemName FROM Item WHERE ItemID = ? AND {owner_sql()}",
                    (data["item_id"],),
                )
                item_row = cursor.fetchone()

                if not item_row:
                    errors.add("item_id", "Selected item was not found.")
                    flash(errors.first(), "danger")
                    return render_template(
                        "purchases/form.html",
                        suppliers=suppliers,
                        items=items,
                        categories=categories,
                        errors=errors.errors,
                        form_data=form_data,
                    )

                item_id = data["item_id"]
                item_name = item_row[0]

                cursor.execute(
                    f"""
                    UPDATE Item
                    SET Qty = Qty + ?, PurchaseRate = ?, SaleRate = ?
                    WHERE ItemID = ? AND {owner_sql()}
                    """,
                    (data["quantity"], data["purchase_rate"], data["sale_rate"], item_id),
                )
            else:
                item_name = data["item_name"]

                cursor.execute(
                    f"""
                    SELECT TOP 1 ItemID, ItemName
                    FROM Item
                    WHERE LOWER(LTRIM(RTRIM(ItemName))) = LOWER(LTRIM(RTRIM(?)))
                      AND CategoryID = ?
                      AND {owner_sql()}
                    ORDER BY Qty DESC, ItemID ASC
                    """,
                    (item_name, data["category_id"]),
                )
                existing_item = cursor.fetchone()

                if existing_item:
                    item_id = existing_item.ItemID
                    item_name = existing_item.ItemName
                    cursor.execute(
                        f"""
                        UPDATE Item
                        SET Qty = Qty + ?, PurchaseRate = ?, SaleRate = ?
                        WHERE ItemID = ? AND {owner_sql()}
                        """,
                        (data["quantity"], data["purchase_rate"], data["sale_rate"], item_id),
                    )
                else:
                    next_item_id = next_table_id(cursor, "Item", "ItemID")
                    next_item_no = next_owner_no(cursor, "Item", "ItemNo")
                    cursor.execute(
                        """
                        INSERT INTO Item (ItemID, ItemNo, ItemName, CategoryID, PurchaseRate, SaleRate, Qty, UserID)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            next_item_id,
                            next_item_no,
                            item_name,
                            data["category_id"],
                            data["purchase_rate"],
                            data["sale_rate"],
                            data["quantity"],
                            request_user_id(),
                        ),
                    )
                    item_id = next_item_id



            next_purchase_id = next_table_id(cursor, "Purchases", "PurchaseID")

            cursor.execute(
                """
                INSERT INTO Purchases (PurchaseID, PurchaseDate, SupplierID, TotalAmount, PaymentMethod, UserID)
                OUTPUT INSERTED.PurchaseID
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    next_purchase_id,
                    data["purchase_date"],
                    data["supplier_id"],
                    total,
                    data["payment_method"],
                    request_user_id(),
                ),
            )



            purchase_id = int(cursor.fetchone()[0])



            cursor.execute(

                """

                INSERT INTO PurchaseDetails

                (PurchaseID, ItemID, Particulars, Qty, PurchaseRate)

                VALUES (?, ?, ?, ?, ?)

                """,

                (purchase_id, item_id, item_name, data["quantity"], data["purchase_rate"]),

            )



            db.commit()

            flash("Purchase created successfully", "success")

            return redirect(url_for("purchases.list_purchases"))



        return render_template(

            "purchases/form.html",

            suppliers=suppliers,

            items=items,

            categories=categories,

            errors=errors.errors,

            form_data=form_data,

        )



    except Exception as e:

        db.rollback()

        app.logger.exception("Error creating purchase")
        flash(f"Error creating purchase: {str(e)}", "danger")

        suppliers, items, categories = [], [], []

        try:

            suppliers, items, categories = _load_purchase_form_data(cursor)

        except Exception:

            pass

        return render_template(

            "purchases/form.html",

            suppliers=suppliers,

            items=items,

            categories=categories,

            errors=errors.errors,

            form_data=form_data or request.form.to_dict(),

        )



    finally:

        cursor.close()





@purchases_bp.route("/list")

@login_required

def list_purchases():

    db = get_db_connection(app)

    cursor = db.cursor()



    try:
        ensure_invoice_payments_table(db, cursor)
        ensure_cash_accounts(db, cursor)
        _ensure_purchase_payment_method_column(db, cursor)

        search = request.args.get("search", "")



        query = f"""

            SELECT
                COALESCE(p.SupplierID, 0) AS SupplierID,
                COALESCE(s.SupplierName, 'N/A') AS SupplierName,
                MIN(p.PurchaseDate) AS FirstPurchaseDate,
                MAX(p.PurchaseDate) AS LastPurchaseDate,
                COUNT(DISTINCT p.PurchaseID) AS PurchaseCount,
                SUM(ISNULL(p.TotalAmount, 0)) AS TotalAmount,
                SUM(ISNULL(details.ItemLineCount, 0)) AS ItemLineCount,
                SUM(ISNULL(details.TotalQty, 0)) AS TotalQty

            FROM Purchases p

            LEFT JOIN Supplier s ON p.SupplierID = s.SupplierID

            LEFT JOIN (
                SELECT
                    pd.PurchaseID,
                    COUNT(*) AS ItemLineCount,
                    SUM(ISNULL(pd.Qty, 0)) AS TotalQty
                FROM PurchaseDetails pd
                GROUP BY pd.PurchaseID
            ) details ON details.PurchaseID = p.PurchaseID

            WHERE {owner_sql("p")}

        """



        params = []



        if search:

            query += """
                AND (
                    CAST(p.PurchaseID AS VARCHAR(20)) LIKE ?
                    OR s.SupplierName LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM PurchaseDetails pd
                        WHERE pd.PurchaseID = p.PurchaseID
                          AND pd.Particulars LIKE ?
                    )
                    OR CONVERT(VARCHAR(10), p.PurchaseDate, 103) LIKE ?
                )
            """

            params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])



        query += """
            GROUP BY COALESCE(p.SupplierID, 0), COALESCE(s.SupplierName, 'N/A')
            ORDER BY MAX(p.PurchaseDate) DESC, COALESCE(s.SupplierName, 'N/A')
        """



        cursor.execute(query, params or ())

        purchases = cursor.fetchall()

        cash_opening, bank_opening = get_cash_openings(cursor)
        cursor.execute(
            f"""
            SELECT
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Cash' THEN Amount ELSE 0 END), 0) AS CashAmount,
                ISNULL(SUM(CASE WHEN COALESCE(PaymentMethod, 'Cash') = 'Bank' THEN Amount ELSE 0 END), 0) AS BankAmount
            FROM InvoicePayments
            WHERE InvoiceID IN (SELECT InvoiceID FROM Invoices WHERE {owner_sql()})
            """
        )
        receipts = cursor.fetchone()
        cash_received = float(receipts.CashAmount or 0) if receipts else 0.0
        bank_received = float(receipts.BankAmount or 0) if receipts else 0.0

        cursor.execute(
            f"""
            SELECT ISNULL(SUM(COALESCE(TotalAmount, 0)), 0) AS TotalPurchases
            FROM Purchases
            WHERE {owner_sql()} AND COALESCE(PaymentMethod, 'Cash') = 'Cash'
            """
        )
        cash_purchase_row = cursor.fetchone()
        total_cash_purchases = float(cash_purchase_row.TotalPurchases or 0) if cash_purchase_row else 0.0
        cursor.execute(
            f"""
            SELECT ISNULL(SUM(COALESCE(TotalAmount, 0)), 0) AS TotalPurchases
            FROM Purchases
            WHERE {owner_sql()} AND COALESCE(PaymentMethod, 'Cash') = 'Bank'
            """
        )
        bank_purchase_row = cursor.fetchone()
        total_bank_purchases = float(bank_purchase_row.TotalPurchases or 0) if bank_purchase_row else 0.0
        cash_in_hand = cash_opening + cash_received - total_cash_purchases
        bank_balance = bank_opening + bank_received - total_bank_purchases



        return render_template(

            "purchases/list.html",

            purchases=purchases,

            search=search,
            cash_in_hand=cash_in_hand,
            bank_balance=bank_balance,
            total_cash_purchases=total_cash_purchases,
            total_bank_purchases=total_bank_purchases,

        )



    except Exception as e:

        flash(f"Error loading purchases: {str(e)}", "danger")

        return redirect(url_for("dashboard.dashboard"))



    finally:

        cursor.close()





@purchases_bp.route("/details/supplier/<int:supplier_id>")

@login_required

def purchase_details(supplier_id):

    db = get_db_connection(app)

    cursor = db.cursor()



    try:

        if supplier_id == 0:

            supplier_filter = "p.SupplierID IS NULL"

            params = []

        else:

            supplier_filter = "p.SupplierID = ?"

            params = [supplier_id]



        cursor.execute(

            f"""
            SELECT
                p.PurchaseID,
                p.PurchaseDate,
                p.TotalAmount,
                COALESCE(s.SupplierName, 'N/A') AS SupplierName,
                pd.Particulars,
                pd.Qty,
                pd.PurchaseRate,
                (ISNULL(pd.Qty, 0) * ISNULL(pd.PurchaseRate, 0)) AS LineTotal
            FROM Purchases p
            LEFT JOIN Supplier s ON p.SupplierID = s.SupplierID
            LEFT JOIN PurchaseDetails pd ON p.PurchaseID = pd.PurchaseID
            WHERE {supplier_filter} AND {owner_sql("p")}
            ORDER BY p.PurchaseDate DESC, p.PurchaseID DESC, pd.DetailID ASC
            """,

            params,

        )

        details = cursor.fetchall()



        if not details:

            flash("Purchase details not found.", "danger")

            return redirect(url_for("purchases.list_purchases"))



        return render_template(

            "purchases/details.html",

            details=details,

            supplier_name=details[0].SupplierName,

        )



    except Exception as e:

        flash(f"Error loading purchase details: {str(e)}", "danger")

        return redirect(url_for("purchases.list_purchases"))



    finally:

        cursor.close()




@purchases_bp.route("/delete/<int:id>", methods=["POST"])

@login_required

def delete_purchase(id):

    db = get_db_connection(app)

    cursor = db.cursor()



    try:

        cursor.execute(f"SELECT PurchaseID FROM Purchases WHERE PurchaseID = ? AND {owner_sql()}", (id,))

        purchase = cursor.fetchone()



        if not purchase:

            flash("Purchase not found.", "danger")

            return redirect(url_for("purchases.list_purchases"))



        cursor.execute(

            """
            SELECT pd.ItemID, pd.Qty, i.ItemName, i.Qty AS CurrentQty
            FROM PurchaseDetails pd
            LEFT JOIN Item i ON pd.ItemID = i.ItemID
            WHERE pd.PurchaseID = ?
            """,

            (id,),

        )

        details = cursor.fetchall()



        for detail in details:

            if detail.ItemID is None:

                continue



            if detail.CurrentQty is not None and detail.CurrentQty < detail.Qty:

                flash(

                    f"Cannot delete this purchase because {detail.ItemName} has only "
                    f"{detail.CurrentQty} in stock, but this purchase added {detail.Qty}.",

                    "danger",

                )

                return redirect(url_for("purchases.list_purchases"))



        for detail in details:

            if detail.ItemID is None:

                continue



            cursor.execute(

                f"""
                UPDATE Item
                SET Qty = Qty - ?
                WHERE ItemID = ? AND {owner_sql()}
                """,

                (detail.Qty, detail.ItemID),

            )



        cursor.execute("DELETE FROM StockHistory WHERE PurchaseID = ?", (id,))

        cursor.execute("DELETE FROM PurchaseDetails WHERE PurchaseID = ?", (id,))

        cursor.execute(f"DELETE FROM Purchases WHERE PurchaseID = ? AND {owner_sql()}", (id,))



        db.commit()

        flash("Purchase deleted successfully. Stock quantities were reduced.", "success")



    except Exception as e:

        db.rollback()

        flash(f"Error deleting purchase: {str(e)}", "danger")



    finally:

        cursor.close()



    return redirect(url_for("purchases.list_purchases"))


@purchases_bp.route("/delete/supplier/<int:supplier_id>", methods=["POST"])
@login_required
def delete_supplier_purchases(supplier_id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        if supplier_id == 0:
            cursor.execute(
                f"SELECT PurchaseID FROM Purchases WHERE SupplierID IS NULL AND {owner_sql()} ORDER BY PurchaseID"
            )
            supplier_label = "N/A"
        else:
            cursor.execute(
                f"SELECT SupplierName FROM Supplier WHERE SupplierID = ? AND {owner_sql()}",
                (supplier_id,),
            )
            supplier_row = cursor.fetchone()

            if not supplier_row:
                flash("Supplier not found.", "danger")
                return redirect(url_for("purchases.list_purchases"))

            supplier_label = supplier_row.SupplierName
            cursor.execute(
                f"SELECT PurchaseID FROM Purchases WHERE SupplierID = ? AND {owner_sql()} ORDER BY PurchaseID",
                (supplier_id,),
            )

        purchase_rows = cursor.fetchall()
        purchase_ids = [row.PurchaseID for row in purchase_rows]

        if not purchase_ids:
            flash("No purchases found for this supplier.", "danger")
            return redirect(url_for("purchases.list_purchases"))

        placeholders = ", ".join("?" for _ in purchase_ids)

        cursor.execute(
            f"""
            SELECT pd.PurchaseID, pd.ItemID, pd.Qty, i.ItemName, i.Qty AS CurrentQty
            FROM PurchaseDetails pd
            LEFT JOIN Item i ON pd.ItemID = i.ItemID
            WHERE pd.PurchaseID IN ({placeholders})
            """,
            tuple(purchase_ids),
        )
        details = cursor.fetchall()

        for detail in details:
            if detail.ItemID is None:
                continue

            if detail.CurrentQty is not None and detail.CurrentQty < detail.Qty:
                flash(
                    f"Cannot delete supplier purchases because {detail.ItemName} has only "
                    f"{detail.CurrentQty} in stock, but one purchase added {detail.Qty}.",
                    "danger",
                )
                return redirect(url_for("purchases.list_purchases"))

        for detail in details:
            if detail.ItemID is None:
                continue

            cursor.execute(
                f"""
                UPDATE Item
                SET Qty = Qty - ?
                WHERE ItemID = ? AND {owner_sql()}
                """,
                (detail.Qty, detail.ItemID),
            )

        cursor.execute(
            f"DELETE FROM StockHistory WHERE PurchaseID IN ({placeholders})",
            tuple(purchase_ids),
        )
        cursor.execute(
            f"DELETE FROM PurchaseDetails WHERE PurchaseID IN ({placeholders})",
            tuple(purchase_ids),
        )
        cursor.execute(
            f"DELETE FROM Purchases WHERE PurchaseID IN ({placeholders}) AND {owner_sql()}",
            tuple(purchase_ids),
        )

        db.commit()
        flash(f"Deleted all purchases for supplier {supplier_label}.", "success")

    except Exception as e:
        db.rollback()
        flash(f"Error deleting supplier purchases: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(url_for("purchases.list_purchases"))


@purchases_bp.route("/edit/<int:id>", methods=["GET", "POST"])

@login_required

def edit_purchase_items(id):

    db = get_db_connection(app)

    cursor = db.cursor()

    errors = ValidationErrors()

    form_data = {}



    try:
        _ensure_purchase_payment_method_column(db, cursor)

        cursor.execute(
            f"""
            SELECT PurchaseID, PurchaseDate, SupplierID, TotalAmount, COALESCE(PaymentMethod, 'Cash') AS PaymentMethod
            FROM Purchases
            WHERE PurchaseID = ? AND {owner_sql()}
            """,
            (id,),
        )

        purchase = cursor.fetchone()



        if not purchase:

            flash("Purchase not found", "danger")

            return redirect(url_for("purchases.list_purchases"))



        cursor.execute(

            """

            SELECT
                pd.DetailID,
                pd.PurchaseID,
                pd.ItemID,
                pd.Particulars,
                pd.Qty,
                pd.PurchaseRate,
                (pd.Qty * pd.PurchaseRate) AS LineTotal,
                i.ItemName

            FROM PurchaseDetails pd

            LEFT JOIN Item i ON pd.ItemID = i.ItemID

            WHERE pd.PurchaseID = ?

        """,

            (id,),

        )

        items = cursor.fetchall()



        cursor.execute(f"SELECT * FROM Supplier WHERE {owner_sql()}")

        suppliers = cursor.fetchall()



        cursor.execute(
            f"""
            SELECT
                COALESCE(
                    MIN(CASE WHEN i.Qty > 0 THEN i.ItemID END),
                    MIN(CASE WHEN i.PurchaseRate > 0 OR i.SaleRate > 0 THEN i.ItemID END),
                    MIN(i.ItemID)
                ) AS ItemID,
                MIN(i.ItemName) AS ItemName,
                i.CategoryID,
                c.CategoryName,
                SUM(i.Qty) AS Qty,
                MAX(i.PurchaseRate) AS PurchaseRate
            FROM Item i
            LEFT JOIN Category c ON i.CategoryID = c.CategoryID
            WHERE {owner_sql("i")}
            GROUP BY LOWER(LTRIM(RTRIM(i.ItemName))), i.CategoryID, c.CategoryName
            ORDER BY MIN(i.ItemName), c.CategoryName
            """
        )

        items_list = cursor.fetchall()



        if request.method == "POST":

            form_data = request.form.to_dict()

            supplier_id = clean_select_id(request.form.get("supplier_id"), "supplier_id", errors, label="Supplier")
            payment_method = normalize_purchase_payment_method(request.form.get("payment_method"))

            item_ids = request.form.getlist("item_id[]")

            quantities = request.form.getlist("quantity[]")



            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "purchases/edit.html",
                    purchase=purchase,
                    items=items,
                    suppliers=suppliers,
                    items_list=items_list,
                    errors=errors.errors,
                    form_data=form_data,
                )

            if not any(item_ids) or not any(quantities):

                errors.add("item_id[]", "At least one item with quantity is required.")

                flash(errors.first(), "danger")

                return render_template(

                    "purchases/edit.html",

                    purchase=purchase,

                    items=items,

                    suppliers=suppliers,

                    items_list=items_list,

                    errors=errors.errors,

                    form_data=form_data,

                )



            cursor.execute("DELETE FROM PurchaseDetails WHERE PurchaseID = ?", (id,))



            total_amount = 0

            has_valid_line = False



            for index, (item_id, qty) in enumerate(zip(item_ids, quantities)):

                if not item_id and not qty:

                    continue



                if not item_id or not qty:

                    errors.add("item_id[]", "Each row must have both an item and quantity.")

                    break



                qty_value = clean_positive_int(qty, "quantity[]", errors, min_val=1, label="Quantity")

                item_value = clean_select_id(item_id, "item_id[]", errors, label="Item")



                if not errors.valid:

                    break



                cursor.execute(

                    f"SELECT ItemName, PurchaseRate FROM Item WHERE ItemID = ? AND {owner_sql()}",

                    (item_value,),

                )

                rate_row = cursor.fetchone()



                if not rate_row:

                    errors.add("item_id[]", "Selected item was not found.")

                    break



                item_name = rate_row[0]

                rate = float(rate_row[1])

                total = qty_value * rate

                total_amount += total

                has_valid_line = True



                cursor.execute(

                    """

                    INSERT INTO PurchaseDetails

                    (PurchaseID, ItemID, Particulars, Qty, PurchaseRate)

                    VALUES (?, ?, ?, ?, ?)

                """,

                    (id, item_value, item_name, qty_value, rate),

                )



            if not errors.valid or not has_valid_line:

                db.rollback()

                if not has_valid_line and errors.valid:

                    errors.add("item_id[]", "At least one valid item line is required.")

                flash(errors.first(), "danger")

                return render_template(

                    "purchases/edit.html",

                    purchase=purchase,

                    items=items,

                    suppliers=suppliers,

                    items_list=items_list,

                    errors=errors.errors,

                    form_data=form_data,

                )



            cursor.execute(

                f"""

                UPDATE Purchases

                SET SupplierID = ?, TotalAmount = ?, PaymentMethod = ?

                WHERE PurchaseID = ? AND {owner_sql()}

            """,

                (supplier_id, total_amount, payment_method, id),

            )



            db.commit()

            flash("Purchase updated successfully", "success")

            return redirect(url_for("purchases.list_purchases"))



        return render_template(

            "purchases/edit.html",

            purchase=purchase,

            items=items,

            suppliers=suppliers,

            items_list=items_list,

            errors=errors.errors,

            form_data=form_data,

        )



    except Exception as e:

        db.rollback()

        flash(f"Error updating purchase: {str(e)}", "danger")

        return redirect(url_for("purchases.list_purchases"))



    finally:

        cursor.close()

