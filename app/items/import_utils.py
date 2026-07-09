import io
import re
from datetime import date

from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.validators import ValidationErrors, clean_string, clean_positive_decimal, clean_positive_int

HEADER_ALIASES = {
    "item_name": {"item_name", "itemname", "item", "name", "product", "product_name"},
    "category": {"category", "category_name", "categoryname"},
    "supplier_name": {"supplier", "supplier_name", "suppliername"},
    "purchase_rate": {"purchase_rate", "purchaserate", "purchase", "purchase_price", "cost"},
    "sale_rate": {"sale_rate", "salerate", "sale", "sale_price", "price"},
    "qty": {"qty", "quantity", "stock", "amount"},
}


def _normalize_header(value):
    if value is None:
        return ""
    return re.sub(r"[\s\-]+", "_", str(value).strip().lower())


def _detect_columns(header_row):
    mapping = {}
    for index, cell in enumerate(header_row):
        key = _normalize_header(cell)
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = index
    return mapping


def _validate_row(row_number, row_data, errors):
    return {
        "item_name": clean_string(
            row_data.get("item_name"), f"row_{row_number}_item_name", errors, max_len=100, label=f"Row {row_number} item name"
        ),
        "category": clean_string(
            row_data.get("category"), f"row_{row_number}_category", errors, max_len=50, label=f"Row {row_number} category"
        ),
        "supplier_name": clean_string(
            row_data.get("supplier_name"), f"row_{row_number}_supplier_name", errors, max_len=60, label=f"Row {row_number} supplier name"
        ),
        "purchase_rate": clean_positive_decimal(
            row_data.get("purchase_rate"), f"row_{row_number}_purchase_rate", errors, label=f"Row {row_number} purchase rate"
        ),
        "sale_rate": clean_positive_decimal(
            row_data.get("sale_rate"), f"row_{row_number}_sale_rate", errors, label=f"Row {row_number} sale rate"
        ),
        "qty": clean_positive_int(
            row_data.get("qty"), f"row_{row_number}_qty", errors, min_val=0, label=f"Row {row_number} quantity"
        ),
    }


def _cell_value(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_items_xlsx(file_stream):
    workbook = None

    try:
        if hasattr(file_stream, "seek"):
            file_stream.seek(0)

        workbook = load_workbook(file_stream, read_only=True, data_only=True)
        sheet = workbook.active
        rows_iter = sheet.iter_rows(values_only=True)

        first_non_empty = None
        for row in rows_iter:
            if any(cell is not None and str(cell).strip() for cell in row):
                first_non_empty = row
                break

        if first_non_empty is None:
            raise ValueError("The Excel file is empty.")

        required_fields = {"item_name", "category", "supplier_name", "purchase_rate", "sale_rate", "qty"}
        header_map = _detect_columns(first_non_empty)

        if header_map and required_fields.issubset(header_map):
            start_row_number = 2
            data_rows_iter = rows_iter
        else:
            if len(first_non_empty) < 6:
                raise ValueError(
                    "Could not detect column headers. Use columns: ItemName, Category, SupplierName, PurchaseRate, SaleRate, Qty."
                )
            header_map = {
                "item_name": 0,
                "category": 1,
                "supplier_name": 2,
                "purchase_rate": 3,
                "sale_rate": 4,
                "qty": 5,
            }
            start_row_number = 1

            # Treat first row as data if it is not a header row.
            def _prepend_first_row():
                yield first_non_empty
                for next_row in rows_iter:
                    yield next_row

            data_rows_iter = _prepend_first_row()

        valid_items = []
        row_errors = []

        for offset, row in enumerate(data_rows_iter):
            row_index = start_row_number + offset
            row_data = {
                field: _cell_value(row[header_map[field]] if header_map[field] < len(row) else "")
                for field in required_fields
            }

            if not any(row_data.values()):
                continue

            errors = ValidationErrors()
            validated = _validate_row(row_index, row_data, errors)

            if errors.valid:
                valid_items.append(validated)
            else:
                row_errors.append(f"Row {row_index}: {errors.first()}")

        if not valid_items and row_errors:
            raise ValueError(row_errors[0])

        if not valid_items:
            raise ValueError("No valid item rows were found in the Excel file.")

        return valid_items, row_errors

    except InvalidFileException as exc:
        raise ValueError("The uploaded file is not a valid .xlsx workbook.") from exc
    finally:
        if workbook is not None:
            workbook.close()


def import_items(app, items):
    db = None
    cursor = None
    inserted = 0
    updated = 0
    row_errors = []

    try:
        from app.db import get_db_connection

        db = get_db_connection(app)
        cursor = db.cursor()

        for item in items:
            cursor.execute(
                """
                SELECT TOP 1 CategoryID, CategoryName
                FROM Category
                WHERE LOWER(LTRIM(RTRIM(CategoryName))) = LOWER(LTRIM(RTRIM(?)))
                """,
                (item["category"],),
            )
            category = cursor.fetchone()

            if not category:
                row_errors.append(f"Category not found: {item['category']}")
                continue

            cursor.execute(
                """
                SELECT TOP 1 SupplierID, SupplierName
                FROM Supplier
                WHERE LOWER(LTRIM(RTRIM(SupplierName))) = LOWER(LTRIM(RTRIM(?)))
                """,
                (item["supplier_name"],),
            )
            supplier = cursor.fetchone()

            if not supplier:
                row_errors.append(f"Supplier not found: {item['supplier_name']}")
                continue

            cursor.execute(
                """
                SELECT TOP 1 ItemID
                FROM Item
                WHERE LOWER(LTRIM(RTRIM(ItemName))) = LOWER(LTRIM(RTRIM(?)))
                  AND CategoryID = ?
                ORDER BY Qty DESC, ItemID ASC
                """,
                (item["item_name"], category.CategoryID),
            )
            existing_item = cursor.fetchone()

            if existing_item:
                item_id = existing_item.ItemID
                cursor.execute(
                    """
                    UPDATE Item
                    SET Qty = Qty + ?, PurchaseRate = ?, SaleRate = ?
                    WHERE ItemID = ?
                    """,
                    (
                        item["qty"],
                        item["purchase_rate"],
                        item["sale_rate"],
                        existing_item.ItemID,
                    ),
                )
                updated += 1
            else:
                cursor.execute("SELECT COALESCE(MAX(ItemID), 0) + 1 AS NextID FROM Item")
                next_item_id = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO Item (ItemID, ItemName, CategoryID, PurchaseRate, SaleRate, Qty)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_item_id,
                        item["item_name"],
                        category.CategoryID,
                        item["purchase_rate"],
                        item["sale_rate"],
                        item["qty"],
                    ),
                )
                item_id = next_item_id
                inserted += 1

            total = item["qty"] * item["purchase_rate"]

            cursor.execute(
                """
                INSERT INTO Purchases (PurchaseDate, SupplierID, TotalAmount)
                OUTPUT INSERTED.PurchaseID
                VALUES (?, ?, ?)
                """,
                (date.today(), supplier.SupplierID, total),
            )
            purchase_id = int(cursor.fetchone()[0])

            cursor.execute(
                """
                INSERT INTO PurchaseDetails (PurchaseID, ItemID, Particulars, Qty, PurchaseRate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (purchase_id, item_id, item["item_name"], item["qty"], item["purchase_rate"]),
            )

        db.commit()
        return inserted, updated, row_errors

    except Exception:
        if db is not None:
            db.rollback()
        raise

    finally:
        if cursor is not None:
            cursor.close()


def build_items_template_xlsx():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Items"
    sheet.append(["ItemName", "Category", "SupplierName", "PurchaseRate", "SaleRate", "Qty"])
    sheet.append(["Hammer", "Glass Hardware 12MM", "ABC Supplier", 150.00, 200.00, 25])
    sheet.append(["Screwdriver Set", "Aluminium Hardware", "ABC Supplier", 80.50, 120.00, 40])

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
