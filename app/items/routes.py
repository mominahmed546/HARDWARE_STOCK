from datetime import date, datetime
from io import BytesIO

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file

from flask_login import login_required

from app import app
from app.db import execute_query, execute_query_one, execute_update, get_db_connection
from app.perf import pagination_meta, parse_page, parse_page_size
from app.payments import (
    add_purchase_payment,
    ensure_purchase_payments_table,
    refresh_purchase_settlement,
)
from app.purchases.routes import _ensure_purchase_payment_method_column, normalize_purchase_payment_method
from app.tenancy import next_owner_no, next_table_id, owner_sql, request_user_id
from app.items.import_utils import (
    build_items_template_xlsx,
    import_items,
    parse_items_xlsx,
)
from app.validators import (
    ValidationErrors,
    clean_date,
    clean_select_id,
    clean_string,
    clean_optional_select_id,
    clean_optional_string,
    clean_positive_decimal,
    clean_positive_int,
)

items_bp = Blueprint("items", __name__, url_prefix="/items")

_BRAND_COLUMN_READY = False


def _ensure_item_brand_column(db, cursor):
    global _BRAND_COLUMN_READY
    if _BRAND_COLUMN_READY:
        return
    cursor.execute(
        """
        ALTER TABLE Item
        ADD COLUMN IF NOT EXISTS Brand VARCHAR(100)
        """
    )
    db.commit()
    _BRAND_COLUMN_READY = True

QTY_FILTERS = {
    "lt10": ("Qty less than 10", " AND COALESCE(i.Qty, 0) < 10"),
    "lt5": ("Qty less than 5", " AND COALESCE(i.Qty, 0) < 5"),
    "eq0": ("Qty equal to 0", " AND COALESCE(i.Qty, 0) = 0"),
    "gt10": ("Qty greater than 10", " AND COALESCE(i.Qty, 0) > 10"),
}


def _validate_item_form(form, errors, *, as_purchase=False):
    data = {
        "item_name": clean_string(form.get("item_name"), "item_name", errors, max_len=100, label="Item name"),
        "brand": clean_optional_string(form.get("brand"), "brand", errors, max_len=100, label="Brand"),
        "category_id": clean_select_id(form.get("category_id"), "category_id", errors, label="Category")
        if as_purchase
        else clean_optional_select_id(form.get("category_id"), "category_id", errors, label="Category"),
        "purchase_rate": clean_positive_decimal(form.get("purchase_rate"), "purchase_rate", errors, label="Purchase rate"),
        "sale_rate": clean_positive_decimal(form.get("sale_rate"), "sale_rate", errors, label="Sale rate"),
        "qty": clean_positive_int(
            form.get("qty"),
            "qty",
            errors,
            min_val=1 if as_purchase else 0,
            label="Quantity",
        ),
    }
    if as_purchase:
        data["supplier_id"] = clean_select_id(form.get("supplier_id"), "supplier_id", errors, label="Supplier")
        data["purchase_date"] = clean_date(form.get("purchase_date"), "purchase_date", errors, label="Purchase date")
        data["payment_method"] = normalize_purchase_payment_method(form.get("payment_method"))
    return data


def _resolve_or_create_item(cursor, data):
    """Find an existing item row or insert a new one; return (item_id, item_name)."""
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
            SET Qty = Qty + ?, PurchaseRate = ?, SaleRate = ?,
                Brand = COALESCE(?, Brand)
            WHERE ItemID = ? AND {owner_sql()}
            """,
            (data["qty"], data["purchase_rate"], data["sale_rate"], data.get("brand"), item_id),
        )
        return item_id, item_name

    next_item_id = next_table_id(cursor, "Item", "ItemID")
    next_item_no = next_owner_no(cursor, "Item", "ItemNo")
    cursor.execute(
        """
        INSERT INTO Item (ItemID, ItemNo, ItemName, Brand, CategoryID, PurchaseRate, SaleRate, Qty, UserID)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            next_item_id,
            next_item_no,
            item_name,
            data.get("brand"),
            data["category_id"],
            data["purchase_rate"],
            data["sale_rate"],
            data["qty"],
            request_user_id(),
        ),
    )
    return next_item_id, item_name


def _record_item_purchase(cursor, data):
    item_id, item_name = _resolve_or_create_item(cursor, data)
    total = float(data["qty"] or 0) * float(data["purchase_rate"] or 0)
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
        INSERT INTO PurchaseDetails (PurchaseID, ItemID, Particulars, Qty, PurchaseRate)
        VALUES (?, ?, ?, ?, ?)
        """,
        (purchase_id, item_id, item_name, data["qty"], data["purchase_rate"]),
    )
    if data["payment_method"] in {"Cash", "Bank"} and total > 0:
        add_purchase_payment(
            cursor,
            type("PurchaseObj", (), {"PurchaseID": purchase_id, "TotalAmount": total})(),
            total,
            data["purchase_date"],
            notes="Paid on item purchase entry",
            payment_method=data["payment_method"],
        )
    else:
        refresh_purchase_settlement(cursor, purchase_id)
    return item_id, purchase_id


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


def _count_duplicate_item_groups(app):
    row = execute_query_one(
        app,
        f"""
        SELECT COUNT(*) AS GroupCount
        FROM (
            SELECT 1
            FROM Item
            WHERE {owner_sql()}
            GROUP BY LOWER(LTRIM(RTRIM(ItemName)))
            HAVING COUNT(*) > 1
        ) dupes
        """,
    )
    return int(row.GroupCount or 0) if row else 0


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


def _supplier_names_for_items(app, item_ids):
    """Batch-load supplier names for a page of items (avoids per-row subqueries)."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    rows = execute_query(
        app,
        f"""
        SELECT
            pd.ItemID,
            STRING_AGG(sname, ', ' ORDER BY sname) AS SupplierName
        FROM (
            SELECT DISTINCT pd.ItemID, s.SupplierName AS sname
            FROM PurchaseDetails pd
            INNER JOIN Purchases p ON p.PurchaseID = pd.PurchaseID
            LEFT JOIN Supplier s ON s.SupplierID = p.SupplierID
            WHERE pd.ItemID IN ({placeholders})
              AND {owner_sql("p")}
              AND {owner_sql("s")}
              AND s.SupplierName IS NOT NULL
              AND BTRIM(s.SupplierName) <> ''
        ) pd
        GROUP BY pd.ItemID
        """,
        tuple(item_ids),
    )
    return {int(row.ItemID): row.SupplierName for row in rows}


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


def _pdf_escape(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_pdf_text(text_str, max_chars):
    words = str(text_str or "").split()
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else [""]


def _items_list_filters():
    search = request.args.get("search", "", type=str)
    category_id = request.args.get("category_id", "", type=str)
    qty_filter = request.args.get("qty_filter", "", type=str)
    if qty_filter not in QTY_FILTERS:
        qty_filter = ""
    return search, category_id, qty_filter


def _items_list_where_sql(search, category_id, qty_filter):
    where_sql = f"WHERE {owner_sql('i')}"
    params = []
    if search:
        where_sql += " AND LOWER(i.ItemName) LIKE LOWER(?)"
        params.append(f"%{search}%")
    if category_id:
        where_sql += " AND i.CategoryID = ?"
        params.append(int(category_id))
    if qty_filter:
        where_sql += QTY_FILTERS[qty_filter][1]
    return where_sql, params


def _fetch_items_for_brand_set(search, category_id, qty_filter):
    where_sql, params = _items_list_where_sql(search, category_id, qty_filter)
    return execute_query(
        app,
        f"""
        SELECT
            i.ItemID,
            COALESCE(i.ItemNo, i.ItemID) AS ItemNo,
            i.ItemName,
            i.Brand,
            c.CategoryName
        FROM Item i
        LEFT JOIN Category c ON i.CategoryID = c.CategoryID
        {where_sql}
        ORDER BY COALESCE(i.ItemNo, i.ItemID), i.ItemID
        """,
        tuple(params) if params else None,
    )


def _fetch_items_for_pdf(search, category_id, qty_filter):
    where_sql, params = _items_list_where_sql(search, category_id, qty_filter)
    return execute_query(
        app,
        f"""
        SELECT
            i.ItemID,
            COALESCE(i.ItemNo, i.ItemID) AS ItemNo,
            i.ItemName,
            i.Brand,
            i.PurchaseRate,
            i.SaleRate,
            i.Qty,
            c.CategoryName,
            (
                SELECT STRING_AGG(sname, ', ' ORDER BY sname)
                FROM (
                    SELECT DISTINCT s.SupplierName AS sname
                    FROM PurchaseDetails pd
                    INNER JOIN Purchases p ON p.PurchaseID = pd.PurchaseID
                    LEFT JOIN Supplier s ON s.SupplierID = p.SupplierID
                    WHERE pd.ItemID = i.ItemID
                      AND {owner_sql("p")}
                      AND {owner_sql("s")}
                      AND s.SupplierName IS NOT NULL
                      AND BTRIM(s.SupplierName) <> ''
                ) suppliers
            ) AS SupplierName
        FROM Item i
        LEFT JOIN Category c ON i.CategoryID = c.CategoryID
        {where_sql}
        ORDER BY COALESCE(i.ItemNo, i.ItemID), i.ItemID
        """,
        tuple(params) if params else None,
    )


def _assemble_multipage_pdf(page_streams, page_width=595, page_height=842):
    n = len(page_streams)
    font1_idx = 3 + 2 * n
    font2_idx = 4 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("ascii"),
    ]

    for i, stream in enumerate(page_streams):
        content = stream.encode("latin-1", errors="replace")
        page_idx = 3 + 2 * i
        content_idx = 4 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 {font1_idx} 0 R /F2 {font2_idx} 0 R >> >> "
                f"/Contents {content_idx} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"
        )

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    pdf = BytesIO()
    pdf.write(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(pdf.tell())
        pdf.write(f"{index} 0 obj\n".encode("ascii"))
        pdf.write(obj)
        pdf.write(b"\nendobj\n")

    xref_offset = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.write(b"0000000000 65535 f \n")
    for offset in offsets:
        pdf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii")
    )
    pdf.seek(0)
    return pdf


def _build_items_list_pdf(items, subtitle_lines=None):
    page_width = 595
    page_height = 842
    x_left = 14
    x_right = page_width - 14
    table_w = x_right - x_left
    line_h = 10
    max_name_chars = 28
    bottom_margin = 52

    col_sr_w = 22
    table_x = x_left
    col_sr_right = table_x + col_sr_w
    col_id_right = col_sr_right + 34
    col_name_right = col_id_right + 138
    col_brand_right = col_name_right + 52
    col_cat_right = col_brand_right + 58
    col_supplier_right = col_cat_right + 72
    col_purchase_right = col_supplier_right + 58
    col_sale_right = col_purchase_right + 58
    col_qty_right = table_x + table_w

    page_streams = []
    commands = []
    y = page_height - 26
    page_num = 1

    def money(value):
        return f"{float(value or 0):,.2f}"

    def text(x, y_pos, value, size=8, font="F1"):
        commands.append(f"BT /{font} {size} Tf {x} {y_pos} Td ({_pdf_escape(value)}) Tj ET")

    def text_right(x, y_pos, value, size=8, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x - approx_width), y_pos, value, size, font)

    def text_center(x_center, y_pos, value, size=8, font="F1"):
        value = str(value)
        approx_width = len(value) * (size * 0.5)
        text(max(2, x_center - (approx_width / 2)), y_pos, value, size, font)

    def line(x1, y1, x2, y2):
        commands.append(f"0.6 w {x1} {y1} m {x2} {y2} l S")

    def rect(x, y_pos, width, height):
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def filled_rect(x, y_pos, width, height, r, g, b):
        commands.append(
            f"{r:.3f} {g:.3f} {b:.3f} rg "
            f"{x} {y_pos} {width} {height} re f "
            f"0 0 0 rg"
        )
        commands.append(f"0.6 w {x} {y_pos} {width} {height} re S")

    def start_page():
        nonlocal y, page_num, commands
        commands = []
        y = page_height - 26
        text_center(page_width / 2, y, "EUROGLASS HARDWARE", 14, "F2")
        y -= 12
        text_center(page_width / 2, y, "Ph: 0300-5411417", 8, "F1")
        y -= 10
        line(x_left, y, x_right, y)
        y -= 12
        text(x_left, y, "ITEMS LIST", 10, "F2")
        text(x_left + 88, y, datetime.now().strftime("%d/%m/%Y %I:%M:%S %p"), 8, "F1")
        y -= 11
        if subtitle_lines:
            for subtitle in subtitle_lines:
                text(x_left, y, subtitle, 8, "F1")
                y -= 10
        text(x_left, y, f"Page {page_num}", 8, "F1")
        y -= 10
        line(x_left, y, x_right, y)
        y -= 12
        draw_table_header()

    def draw_table_header():
        nonlocal y
        header_y = y
        row_h = 18
        filled_rect(table_x, header_y - row_h + 4, table_w, row_h, 0.88, 0.88, 0.88)
        for x in (
            col_sr_right,
            col_id_right,
            col_name_right,
            col_brand_right,
            col_cat_right,
            col_supplier_right,
            col_purchase_right,
            col_sale_right,
        ):
            line(x, header_y - row_h + 4, x, header_y + 4)
        text_center(table_x + col_sr_w / 2, header_y - 8, "#", 8, "F2")
        text_center(col_sr_right + (col_id_right - col_sr_right) / 2, header_y - 8, "ID", 8, "F2")
        text_center(col_id_right + (col_name_right - col_id_right) / 2, header_y - 8, "ITEM NAME", 8, "F2")
        text_center(col_name_right + (col_brand_right - col_name_right) / 2, header_y - 8, "BRAND", 8, "F2")
        text_center(col_brand_right + (col_cat_right - col_brand_right) / 2, header_y - 8, "CATEGORY", 8, "F2")
        text_center(col_cat_right + (col_supplier_right - col_cat_right) / 2, header_y - 8, "SUPPLIER", 8, "F2")
        text_center(col_supplier_right + (col_purchase_right - col_supplier_right) / 2, header_y - 8, "PUR RATE", 8, "F2")
        text_center(col_purchase_right + (col_sale_right - col_purchase_right) / 2, header_y - 8, "SALE RATE", 8, "F2")
        text_center(col_sale_right + (col_qty_right - col_sale_right) / 2, header_y - 8, "QTY", 8, "F2")
        y = header_y - row_h - 2

    def new_page():
        nonlocal page_num
        page_streams.append("\n".join(commands))
        page_num += 1
        start_page()

    start_page()

    total_qty = 0
    for index, item in enumerate(items, start=1):
        name_lines = _wrap_pdf_text(item.ItemName, max_name_chars)
        brand_lines = _wrap_pdf_text(item.Brand or "—", 10)
        cat_lines = _wrap_pdf_text(item.CategoryName or "N/A", 12)
        supplier_lines = _wrap_pdf_text(item.SupplierName or "—", 14)
        row_lines = max(len(name_lines), len(brand_lines), len(cat_lines), len(supplier_lines), 1)
        dyn_row_h = max(18, 6 + row_lines * line_h)

        if y - dyn_row_h < bottom_margin:
            new_page()

        rect(table_x, y - dyn_row_h + 4, table_w, dyn_row_h)
        for x in (
            col_sr_right,
            col_id_right,
            col_name_right,
            col_brand_right,
            col_cat_right,
            col_supplier_right,
            col_purchase_right,
            col_sale_right,
        ):
            line(x, y - dyn_row_h + 4, x, y + 4)

        mid_y = y - (dyn_row_h / 2) + 2
        text_center(table_x + col_sr_w / 2, mid_y, str(index), 8, "F1")
        text_center(col_sr_right + (col_id_right - col_sr_right) / 2, mid_y, str(item.ItemNo or item.ItemID), 8, "F1")

        text_y = y - 8
        for name_line in name_lines:
            text(col_id_right + 3, text_y, name_line, 8, "F1")
            text_y -= line_h
        text_y = y - 8
        for brand_line in brand_lines:
            text(col_name_right + 3, text_y, brand_line, 8, "F1")
            text_y -= line_h
        text_y = y - 8
        for cat_line in cat_lines:
            text(col_brand_right + 3, text_y, cat_line, 8, "F1")
            text_y -= line_h
        text_y = y - 8
        for supplier_line in supplier_lines:
            text(col_cat_right + 3, text_y, supplier_line, 8, "F1")
            text_y -= line_h

        text_right(col_purchase_right - 3, mid_y, money(item.PurchaseRate), 8, "F1")
        text_right(col_sale_right - 3, mid_y, money(item.SaleRate), 8, "F1")
        text_right(col_qty_right - 3, mid_y, str(int(item.Qty or 0)), 8, "F1")
        total_qty += int(item.Qty or 0)
        y -= dyn_row_h

    # Summary footer rows (invoice-style, full table width)
    row_h_footer = 18
    if y - row_h_footer * 2 < bottom_margin:
        new_page()
    y -= 8
    for label, value in (("TOTAL ITEMS", str(len(items))), ("TOTAL QTY", str(total_qty))):
        filled_rect(table_x, y - row_h_footer + 4, table_w, row_h_footer, 0.88, 0.88, 0.88)
        line(col_sale_right, y - row_h_footer + 4, col_sale_right, y + 4)
        mid_y = y - (row_h_footer / 2) + 2
        text(table_x + 4, mid_y - 3, label, 10, "F2")
        text_right(col_qty_right - 4, mid_y - 3, value, 10, "F2")
        y -= row_h_footer

    page_streams.append("\n".join(commands))
    return _assemble_multipage_pdf(page_streams, page_width, page_height)


@items_bp.route("/list")
@login_required
def list_items():
    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        try:
            _ensure_item_brand_column(db, cursor)
        finally:
            cursor.close()

        search, category_id, qty_filter = _items_list_filters()

        where_sql, params = _items_list_where_sql(search, category_id, qty_filter)

        total_row = execute_query_one(
            app,
            f"SELECT COUNT(*) AS TotalCount FROM Item i {where_sql}",
            tuple(params) if params else None,
        )
        pagination = pagination_meta(int(total_row.TotalCount or 0), parse_page(request.args.get("page")), parse_page_size(request.args.get("page_size")))

        query = f"""
            SELECT
                i.ItemID,
                COALESCE(i.ItemNo, i.ItemID) AS ItemNo,
                i.ItemName,
                i.Brand,
                i.CategoryID,
                i.PurchaseRate,
                i.SaleRate,
                i.Qty,
                c.CategoryName
            FROM Item i
            LEFT JOIN Category c ON i.CategoryID = c.CategoryID
            {where_sql}
            ORDER BY COALESCE(i.ItemNo, i.ItemID), i.ItemID
            LIMIT ? OFFSET ?
        """
        item_rows = execute_query(
            app,
            query,
            tuple(params + [pagination["page_size"], pagination["offset"]]),
        )
        suppliers_by_id = _supplier_names_for_items(
            app,
            [int(row.ItemID) for row in item_rows],
        )
        items = []
        for row in item_rows:
            items.append(
                {
                    "ItemID": row.ItemID,
                    "ItemNo": row.ItemNo,
                    "ItemName": row.ItemName,
                    "Brand": row.Brand,
                    "CategoryID": row.CategoryID,
                    "PurchaseRate": row.PurchaseRate,
                    "SaleRate": row.SaleRate,
                    "Qty": row.Qty,
                    "CategoryName": row.CategoryName,
                    "SupplierName": suppliers_by_id.get(int(row.ItemID)),
                }
            )
        categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")
        duplicate_groups_count = _count_duplicate_item_groups(app)

        return render_template(
            "items/list.html",
            items=items,
            categories=categories,
            search=search,
            category_id=category_id,
            qty_filter=qty_filter,
            qty_filters=QTY_FILTERS,
            duplicate_groups_count=duplicate_groups_count,
            pagination=pagination,
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
            pagination=pagination_meta(0, 1, 50),
        )


@items_bp.route("/list/pdf")
@login_required
def list_items_pdf():
    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        try:
            _ensure_item_brand_column(db, cursor)
        finally:
            cursor.close()

        search, category_id, qty_filter = _items_list_filters()
        items = _fetch_items_for_pdf(search, category_id, qty_filter)

        subtitle_lines = [f"Total items: {len(items)}"]
        if search:
            subtitle_lines.append(f"Search: {search}")
        if category_id:
            category = execute_query_one(
                app,
                f"SELECT CategoryName FROM Category WHERE CategoryID = ? AND {owner_sql()}",
                (int(category_id),),
            )
            if category:
                subtitle_lines.append(f"Category: {category.CategoryName}")
        if qty_filter:
            subtitle_lines.append(f"Filter: {QTY_FILTERS[qty_filter][0]}")

        pdf = _build_items_list_pdf(items, subtitle_lines)
        filename = f"items_list_{date.today().strftime('%Y%m%d')}.pdf"
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        app.logger.exception("Error generating items PDF")
        flash(f"Error generating items PDF: {str(e)}", "danger")
        return redirect(url_for("items.list_items", search=request.args.get("search", "")))


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


@items_bp.route("/set-brand", methods=["GET", "POST"])
@login_required
def set_brand_manually():
    search, category_id, qty_filter = _items_list_filters()
    categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")

    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        try:
            _ensure_item_brand_column(db, cursor)
        finally:
            cursor.close()
    except Exception as e:
        app.logger.exception("Error preparing brand column")
        flash(f"Error loading items: {str(e)}", "danger")
        return redirect(url_for("items.list_items"))

    if request.method == "POST":
        errors = ValidationErrors()
        brand = clean_string(request.form.get("brand"), "brand", errors, max_len=100, label="Brand")
        item_ids_raw = request.form.getlist("item_ids")
        item_ids = []
        for raw in item_ids_raw:
            try:
                item_ids.append(int(raw))
            except (TypeError, ValueError):
                errors.add("item_ids", "Invalid item selection.")
                break

        if not item_ids and errors.valid:
            errors.add("item_ids", "Select at least one item.")

        if not errors.valid:
            flash(errors.first(), "danger")
            items = _fetch_items_for_brand_set(search, category_id, qty_filter)
            return render_template(
                "items/set_brand.html",
                items=items,
                categories=categories,
                search=search,
                category_id=category_id,
                qty_filter=qty_filter,
                qty_filters=QTY_FILTERS,
                brand=brand if isinstance(brand, str) else request.form.get("brand", ""),
                selected_ids=set(item_ids),
            )

        try:
            placeholders = ", ".join("?" * len(item_ids))
            updated = execute_update(
                app,
                f"""
                UPDATE Item
                SET Brand = ?
                WHERE ItemID IN ({placeholders})
                  AND {owner_sql()}
                """,
                tuple([brand] + item_ids),
            )
            flash(f'Brand "{brand}" set on {updated} item(s).', "success")
            return redirect(url_for("items.list_items"))
        except Exception as e:
            app.logger.exception("Manual brand update failed")
            flash(f"Brand update failed: {str(e)}", "danger")

    items = _fetch_items_for_brand_set(search, category_id, qty_filter)
    return render_template(
        "items/set_brand.html",
        items=items,
        categories=categories,
        search=search,
        category_id=category_id,
        qty_filter=qty_filter,
        qty_filters=QTY_FILTERS,
        brand="",
        selected_ids=set(),
    )


@items_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_item():
    errors = ValidationErrors()
    form_data = {}
    categories = execute_query(app, f"SELECT CategoryID, CategoryName FROM Category WHERE {owner_sql()} ORDER BY CategoryName")
    suppliers = execute_query(app, f"SELECT SupplierID, SupplierName FROM Supplier WHERE {owner_sql()} ORDER BY SupplierName")

    if request.method == "POST":
        form_data = request.form.to_dict()
        data = _validate_item_form(request.form, errors, as_purchase=True)

        if not errors.valid:
            flash(errors.first(), "danger")
            return render_template(
                "items/form.html",
                item=None,
                categories=categories,
                suppliers=suppliers,
                errors=errors.errors,
                form_data=form_data,
            )

        try:
            db = get_db_connection(app)
            cursor = db.cursor()
            _ensure_item_brand_column(db, cursor)
            _ensure_purchase_payment_method_column(db, cursor)
            ensure_purchase_payments_table(db, cursor)
            cursor.execute(
                f"SELECT SupplierID FROM Supplier WHERE SupplierID = ? AND {owner_sql()}",
                (data["supplier_id"],),
            )
            if not cursor.fetchone():
                flash("Supplier not found.", "danger")
                cursor.close()
                return render_template(
                    "items/form.html",
                    item=None,
                    categories=categories,
                    suppliers=suppliers,
                    errors=errors.errors,
                    form_data=form_data,
                )

            _record_item_purchase(cursor, data)
            db.commit()
            cursor.close()

            flash("Purchase recorded and item added to stock.", "success")
            return redirect(url_for("items.list_items"))

        except Exception as e:
            app.logger.exception("Error creating item purchase")
            flash(f"Error creating item: {str(e)}", "danger")

    if not form_data:
        form_data = {
            "purchase_date": date.today().isoformat(),
            "payment_method": "Cash",
        }

    return render_template(
        "items/form.html",
        item=None,
        categories=categories,
        suppliers=suppliers,
        errors=errors.errors,
        form_data=form_data,
    )


@items_bp.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_item(id):
    errors = ValidationErrors()
    form_data = {}

    try:
        db = get_db_connection(app)
        cursor = db.cursor()
        try:
            _ensure_item_brand_column(db, cursor)
        finally:
            cursor.close()

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
                SET ItemName = ?, Brand = ?, CategoryID = ?, PurchaseRate = ?,
                    SaleRate = ?, Qty = ?
                WHERE ItemID = ? AND {owner_sql()}
                """,
                (
                    data["item_name"],
                    data.get("brand"),
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


def _items_list_redirect():
    """Keep search/filters after delete so the list view does not reset."""
    return redirect(
        url_for(
            "items.list_items",
            search=request.form.get("search") or request.args.get("search") or "",
            category_id=request.form.get("category_id") or request.args.get("category_id") or "",
            qty_filter=request.form.get("qty_filter") or request.args.get("qty_filter") or "",
            page=request.form.get("page") or request.args.get("page") or None,
        )
    )


@items_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_item(id):
    try:
        item = execute_query_one(
            app,
            f"SELECT ItemID, ItemName FROM Item WHERE ItemID = ? AND {owner_sql()}",
            (id,),
        )
        if not item:
            flash("Item not found.", "danger")
            return _items_list_redirect()

        # Drop orphaned detail rows (parent purchase/invoice already gone) so
        # they cannot block delete forever after a bad sync or manual cleanup.
        execute_update(
            app,
            """
            DELETE FROM PurchaseDetails
            WHERE ItemID = ?
              AND PurchaseID NOT IN (SELECT PurchaseID FROM Purchases)
            """,
            (id,),
        )
        execute_update(
            app,
            """
            DELETE FROM InvoiceDetails
            WHERE ItemID = ?
              AND InvoiceID NOT IN (SELECT InvoiceID FROM Invoices)
            """,
            (id,),
        )

        purchase_rows = execute_query(
            app,
            """
            SELECT DISTINCT pd.PurchaseID
            FROM PurchaseDetails pd
            INNER JOIN Purchases p ON p.PurchaseID = pd.PurchaseID
            WHERE pd.ItemID = ?
            ORDER BY pd.PurchaseID
            """,
            (id,),
        )
        invoice_rows = execute_query(
            app,
            """
            SELECT DISTINCT id.InvoiceID
            FROM InvoiceDetails id
            INNER JOIN Invoices i ON i.InvoiceID = id.InvoiceID
            WHERE id.ItemID = ?
            ORDER BY id.InvoiceID
            """,
            (id,),
        )

        purchase_ids = [int(r.PurchaseID) for r in (purchase_rows or [])]
        invoice_ids = [int(r.InvoiceID) for r in (invoice_rows or [])]

        if invoice_ids or purchase_ids:
            parts = []
            if purchase_ids:
                ids = ", ".join(f"#{pid}" for pid in purchase_ids[:8])
                extra = f" (+{len(purchase_ids) - 8} more)" if len(purchase_ids) > 8 else ""
                parts.append(f"purchase(s) {ids}{extra}")
            if invoice_ids:
                ids = ", ".join(f"#{iid}" for iid in invoice_ids[:8])
                extra = f" (+{len(invoice_ids) - 8} more)" if len(invoice_ids) > 8 else ""
                parts.append(f"invoice(s) {ids}{extra}")
            flash(
                f"Cannot delete “{item.ItemName}”: it is used on {', and '.join(parts)}. "
                "Open that purchase/invoice, remove the line (or delete the document), "
                "then delete this item.",
                "danger",
            )
            return _items_list_redirect()

        execute_update(app, f"DELETE FROM Item WHERE ItemID = ? AND {owner_sql()}", (id,))
        flash("Item deleted successfully", "success")

    except Exception as e:
        flash(f"Error deleting item: {str(e)}", "danger")

    return _items_list_redirect()


def _repoint_and_delete_items(cursor, keep_id, other_ids):
    """Move purchase/invoice/quotation/stock history from `other_ids` onto
    `keep_id`, then delete the now-empty duplicate rows. Caller owns the
    transaction (commit/rollback) and the owner_sql() check on Item.
    """
    for other_id in other_ids:
        cursor.execute("UPDATE PurchaseDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
        cursor.execute("UPDATE InvoiceDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
        cursor.execute("UPDATE QuotationDetails SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
        cursor.execute("UPDATE StockHistory SET ItemID = ? WHERE ItemID = ?", (keep_id, other_id))
        cursor.execute(f"DELETE FROM Item WHERE ItemID = ? AND {owner_sql()}", (other_id,))


@items_bp.route("/duplicates")
@login_required
def duplicate_items():
    try:
        groups = _duplicate_item_groups(app)
        return render_template("items/duplicates.html", groups=groups)
    except Exception as e:
        # Render the page inside the try too: any bad row/field should
        # flash instead of showing a raw 500.
        app.logger.exception("Error loading duplicate items")
        flash(f"Error loading duplicate items: {str(e)}", "danger")
        return redirect(url_for("items.list_items"))


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

        _repoint_and_delete_items(cursor, keep_id, merge_ids)
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


@items_bp.route("/duplicates/merge-all", methods=["POST"])
@login_required
def merge_all_duplicate_items():
    try:
        groups = _duplicate_item_groups(app)

        if not groups:
            flash("No duplicate items to merge.", "info")
            return redirect(url_for("items.duplicate_items"))

        db = get_db_connection(app)
        cursor = db.cursor()
        merged_rows = 0

        for group in groups:
            keep_id = group["suggested_keep_id"]
            other_ids = [int(row.ItemID) for row in group["rows"] if int(row.ItemID) != keep_id]
            if not other_ids:
                continue

            total_qty = sum(int(row.Qty or 0) for row in group["rows"])
            _repoint_and_delete_items(cursor, keep_id, other_ids)
            cursor.execute(f"UPDATE Item SET Qty = ? WHERE ItemID = ? AND {owner_sql()}", (total_qty, keep_id))
            merged_rows += len(other_ids)

        db.commit()
        cursor.close()

        flash(
            f"Merged {merged_rows} duplicate row(s) across {len(groups)} item name(s), "
            "using the suggested pick (most usage history, then highest quantity) for each.",
            "success",
        )

    except Exception as e:
        app.logger.exception("Error merging all duplicate items")
        flash(f"Error merging items: {str(e)}", "danger")

    return redirect(url_for("items.duplicate_items"))
