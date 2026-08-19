from datetime import date, datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import login_required

from app import app
from app.db import get_db_connection
from app.quotations.excel import MAX_LINE_ROWS, build_quotation_xlsx, line_amount, sqft_for_line
from app.tenancy import owner_sql, request_user_id
from app.wa_api import is_configured as wa_api_configured
from app.wa_api import send_file_bytes as wa_send_file
from app.whatsapp import public_file_url, whatsapp_url
from app.validators import (
    ValidationErrors,
    clean_date,
    clean_optional_select_id,
    clean_optional_string,
    clean_phone,
    clean_positive_decimal,
    clean_positive_int,
    clean_string,
)

quotations_bp = Blueprint("quotations", __name__, url_prefix="/quotations")
_QUOTATIONS_SCHEMA_READY = False


def ensure_quotations_schema(db, cursor):
    global _QUOTATIONS_SCHEMA_READY
    if _QUOTATIONS_SCHEMA_READY:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS Quotations (
            QuotationID SERIAL PRIMARY KEY,
            QuotationNo INTEGER UNIQUE,
            QuotationDate DATE NOT NULL,
            CustomerID INTEGER REFERENCES Customers(CustomerID),
            CustomerName VARCHAR(100) NOT NULL,
            Address VARCHAR(255),
            Project VARCHAR(255),
            WorkType VARCHAR(100),
            Engineer VARCHAR(100),
            ContactNo VARCHAR(50),
            Notes VARCHAR(500),
            Advance NUMERIC(12, 2) DEFAULT 0,
            TotalAmount NUMERIC(12, 2) DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS QuotationDetails (
            DetailID SERIAL PRIMARY KEY,
            QuotationID INTEGER NOT NULL REFERENCES Quotations(QuotationID) ON DELETE CASCADE,
            ItemID INTEGER REFERENCES Item(ItemID),
            Description VARCHAR(255),
            Width NUMERIC(12, 2) DEFAULT 0,
            Height NUMERIC(12, 2) DEFAULT 0,
            Qty INTEGER NOT NULL,
            Rate NUMERIC(10, 2) DEFAULT 0,
            SqFt NUMERIC(12, 4) DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        ALTER TABLE QuotationDetails
        ADD COLUMN IF NOT EXISTS SqFt NUMERIC(12, 4) DEFAULT 0
        """
    )
    db.commit()
    _QUOTATIONS_SCHEMA_READY = True


def _default_quotation_lines():
    return [
        {
            "description": "",
            "width": "",
            "height": "",
            "quantity": "1",
            "rate": "0",
            "sqft": "",
        }
    ]


def _quotation_lines_from_form(form):
    descriptions = form.getlist("description[]")
    widths = form.getlist("width[]")
    heights = form.getlist("height[]")
    quantities = form.getlist("quantity[]")
    rates = form.getlist("rate[]")
    sqfts = form.getlist("sqft[]")

    line_count = max(len(descriptions), len(widths), len(heights), len(quantities), len(rates), len(sqfts), 1)
    lines = []
    for index in range(line_count):
        lines.append(
            {
                "description": descriptions[index] if index < len(descriptions) else "",
                "width": widths[index] if index < len(widths) else "",
                "height": heights[index] if index < len(heights) else "",
                "quantity": quantities[index] if index < len(quantities) else "",
                "rate": rates[index] if index < len(rates) else "",
                "sqft": sqfts[index] if index < len(sqfts) else "",
            }
        )
    return lines


def _validate_quotation_header(form, errors):
    customer_id = clean_optional_select_id(form.get("customer_id"), "customer_id", errors, label="Customer")
    return {
        "quotation_date": clean_date(form.get("quotation_date"), "quotation_date", errors, label="Quotation date"),
        "customer_id": customer_id,
        "customer_name": clean_string(
            form.get("customer_name"), "customer_name", errors, max_len=100, label="Name"
        ),
        "address": clean_optional_string(form.get("address"), "address", errors, max_len=255, label="Address"),
        "project": clean_optional_string(form.get("project"), "project", errors, max_len=255, label="Project"),
        "work_type": clean_optional_string(form.get("work_type"), "work_type", errors, max_len=100, label="Work type"),
        "engineer": clean_optional_string(form.get("engineer"), "engineer", errors, max_len=100, label="Engineer"),
        "contact_no": clean_optional_string(form.get("contact_no"), "contact_no", errors, max_len=50, label="Contact"),
        "notes": clean_optional_string(form.get("notes"), "notes", errors, max_len=500, label="Note"),
        "advance": clean_positive_decimal(
            form.get("advance") or "0",
            "advance",
            errors,
            min_val=0,
            label="Advance",
        ),
    }


def _validate_quotation_lines(form, errors):
    lines = _quotation_lines_from_form(form)
    valid_lines = []

    if not any(
        line["description"] or line["quantity"] or line["rate"] or line["width"] or line["height"] or line["sqft"]
        for line in lines
    ):
        errors.add("description[]", "At least one quotation item is required.")
        return lines, valid_lines

    if len(lines) > MAX_LINE_ROWS:
        errors.add("description[]", f"A quotation can include at most {MAX_LINE_ROWS} items.")
        return lines, valid_lines

    for line in lines:
        has_content = any(str(line.get(key) or "").strip() for key in ("description", "width", "height", "sqft"))
        qty_filled = str(line.get("quantity") or "").strip() not in {"", "1"}
        rate_filled = str(line.get("rate") or "").strip() not in {"", "0", "0.0", "0.00"}
        if not has_content and not qty_filled and not rate_filled:
            continue

        description = clean_string(
            line.get("description"), "description[]", errors, max_len=255, label="Description"
        )

        quantity = clean_positive_int(line.get("quantity"), "quantity[]", errors, min_val=1, label="Quantity")
        rate = clean_positive_decimal(line.get("rate"), "rate[]", errors, min_val=0, label="Rate")
        width = clean_positive_decimal(
            line.get("width") or "0", "width[]", errors, min_val=0, label="Width"
        )
        height = clean_positive_decimal(
            line.get("height") or "0", "height[]", errors, min_val=0, label="Height"
        )
        sqft = clean_positive_decimal(
            line.get("sqft") or "0", "sqft[]", errors, min_val=0, label="SQ/FT"
        )

        if not errors.valid:
            break

        sqft_value = sqft_for_line(width or 0, height or 0, quantity, sqft)
        valid_lines.append(
            {
                "item_id": None,
                "description": description,
                "width": width or 0,
                "height": height or 0,
                "quantity": quantity,
                "rate": rate,
                "sqft": sqft_value,
                "total": line_amount(width or 0, height or 0, quantity, rate, sqft_value),
            }
        )

    if errors.valid and not valid_lines:
        errors.add("description[]", "At least one valid quotation item is required.")

    return lines, valid_lines


def _load_form_lookups(cursor):
    cursor.execute(
        f"""
        SELECT CustomerID, CustomerName, ContactNo
        FROM Customers
        WHERE {owner_sql()}
        ORDER BY CustomerName
        """
    )
    customers = cursor.fetchall()
    return customers


def _quotation_header_payload(quotation, valid_lines):
    return {
        "quotation_no": quotation.QuotationNo,
        "quotation_date": quotation.QuotationDate,
        "customer_name": quotation.CustomerName,
        "address": quotation.Address,
        "project": quotation.Project,
        "work_type": quotation.WorkType,
        "engineer": quotation.Engineer,
        "contact_no": quotation.ContactNo,
        "notes": quotation.Notes,
        "advance": float(quotation.Advance or 0),
        "total_amount": float(quotation.TotalAmount or 0),
    }, [
        {
            "description": row.Description,
            "width": row.Width,
            "height": row.Height,
            "quantity": row.Qty,
            "rate": row.Rate,
            "sqft": getattr(row, "SqFt", None) or getattr(row, "sqft", None),
        }
        for row in valid_lines
    ]


def _load_quotation(cursor, quotation_id):
    cursor.execute(
        f"""
        SELECT
            QuotationID, QuotationNo, QuotationDate, CustomerID, CustomerName,
            Address, Project, WorkType, Engineer, ContactNo, Notes, Advance, TotalAmount
        FROM Quotations
        WHERE QuotationID = ? AND {owner_sql()}
        """,
        (quotation_id,),
    )
    return cursor.fetchone()


def _load_quotation_details(cursor, quotation_id):
    cursor.execute(
        """
        SELECT DetailID, ItemID, Description, Width, Height, Qty, Rate, SqFt
        FROM QuotationDetails
        WHERE QuotationID = ?
        ORDER BY DetailID
        """,
        (quotation_id,),
    )
    return cursor.fetchall()


def _send_quotation_excel(quotation, details):
    header, lines = _quotation_header_payload(quotation, details)
    workbook = build_quotation_xlsx(header, lines)
    return send_file(
        workbook,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="QUOTATION.xlsx",
    )


@quotations_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_quotation():
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {
        "quotation_date": date.today().isoformat(),
        "advance": "0",
    }
    quotation_lines = _default_quotation_lines()

    try:
        ensure_quotations_schema(db, cursor)
        customers = _load_form_lookups(cursor)

        if request.method == "POST":
            form_data = request.form.to_dict()
            quotation_lines = _quotation_lines_from_form(request.form)
            header = _validate_quotation_header(request.form, errors)
            quotation_lines, valid_lines = _validate_quotation_lines(request.form, errors)

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "quotations/form.html",
                    customers=customers,
                    errors=errors.errors,
                    form_data=form_data,
                    quotation_lines=quotation_lines,
                    max_lines=MAX_LINE_ROWS,
                )

            total = sum(line["total"] for line in valid_lines)
            quotation_date = datetime.strptime(header["quotation_date"], "%Y-%m-%d").date()

            cursor.execute(f"SELECT COALESCE(MAX(QuotationNo), 0) + 1 AS NextNo FROM Quotations WHERE {owner_sql()}")
            quotation_no = int(cursor.fetchone()[0])

            cursor.execute(
                """
                INSERT INTO Quotations (
                    QuotationNo, QuotationDate, CustomerID, CustomerName, Address,
                    Project, WorkType, Engineer, ContactNo, Notes, Advance, TotalAmount, UserID
                )
                OUTPUT INSERTED.QuotationID
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quotation_no,
                    quotation_date,
                    header["customer_id"],
                    header["customer_name"],
                    header["address"],
                    header["project"],
                    header["work_type"],
                    header["engineer"],
                    header["contact_no"],
                    header["notes"],
                    header["advance"] or 0,
                    total,
                    request_user_id(),
                ),
            )
            quotation_id = int(cursor.fetchone()[0])

            for line in valid_lines:
                cursor.execute(
                    """
                    INSERT INTO QuotationDetails (
                        QuotationID, ItemID, Description, Width, Height, Qty, Rate, SqFt
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        quotation_id,
                        line["item_id"],
                        line["description"],
                        line["width"],
                        line["height"],
                        line["quantity"],
                        line["rate"],
                        line["sqft"],
                    ),
                )

            db.commit()
            quotation = _load_quotation(cursor, quotation_id)
            details = _load_quotation_details(cursor, quotation_id)
            return _send_quotation_excel(quotation, details)

        return render_template(
            "quotations/form.html",
            customers=customers,
            errors=errors.errors,
            form_data=form_data,
            quotation_lines=quotation_lines,
            max_lines=MAX_LINE_ROWS,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error creating quotation: {str(e)}", "danger")
        return render_template(
            "quotations/form.html",
            customers=[],
            errors=errors.errors,
            form_data=form_data,
            quotation_lines=quotation_lines,
            max_lines=MAX_LINE_ROWS,
        )

    finally:
        cursor.close()


@quotations_bp.route("/<int:id>/edit", methods=["GET", "POST"])
@login_required
def edit_quotation(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()
    form_data = {}
    quotation_lines = _default_quotation_lines()

    try:
        ensure_quotations_schema(db, cursor)
        customers = _load_form_lookups(cursor)

        quotation = _load_quotation(cursor, id)
        if not quotation:
            flash("Quotation not found.", "danger")
            return redirect(url_for("quotations.list_quotations"))

        if request.method == "GET":
            details = _load_quotation_details(cursor, id)
            form_data = {
                "quotation_date": quotation.QuotationDate.isoformat()
                    if hasattr(quotation.QuotationDate, "isoformat")
                    else str(quotation.QuotationDate)[:10],
                "customer_id": str(quotation.CustomerID or ""),
                "customer_name": quotation.CustomerName or "",
                "address": quotation.Address or "",
                "project": quotation.Project or "",
                "work_type": quotation.WorkType or "",
                "engineer": quotation.Engineer or "",
                "contact_no": quotation.ContactNo or "",
                "notes": quotation.Notes or "",
                "advance": str(float(quotation.Advance or 0)),
            }
            quotation_lines = [
                {
                    "description": row.Description or "",
                    "width": str(float(row.Width or 0)) if float(row.Width or 0) else "",
                    "height": str(float(row.Height or 0)) if float(row.Height or 0) else "",
                    "quantity": str(row.Qty or 1),
                    "rate": str(float(row.Rate or 0)),
                    "sqft": str(float(getattr(row, "SqFt", None) or 0)) if float(getattr(row, "SqFt", None) or 0) else "",
                }
                for row in details
            ] or _default_quotation_lines()

        if request.method == "POST":
            form_data = request.form.to_dict()
            quotation_lines = _quotation_lines_from_form(request.form)
            header = _validate_quotation_header(request.form, errors)
            quotation_lines, valid_lines = _validate_quotation_lines(request.form, errors)

            if not errors.valid:
                flash(errors.first(), "danger")
                return render_template(
                    "quotations/form.html",
                    customers=customers,
                    errors=errors.errors,
                    form_data=form_data,
                    quotation_lines=quotation_lines,
                    max_lines=MAX_LINE_ROWS,
                    is_edit=True,
                    quotation_id=id,
                )

            total = sum(line["total"] for line in valid_lines)
            quotation_date = datetime.strptime(header["quotation_date"], "%Y-%m-%d").date()

            cursor.execute(
                f"""
                UPDATE Quotations SET
                    QuotationDate = ?,
                    CustomerID = ?,
                    CustomerName = ?,
                    Address = ?,
                    Project = ?,
                    WorkType = ?,
                    Engineer = ?,
                    ContactNo = ?,
                    Notes = ?,
                    Advance = ?,
                    TotalAmount = ?
                WHERE QuotationID = ? AND {owner_sql()}
                """,
                (
                    quotation_date,
                    header["customer_id"],
                    header["customer_name"],
                    header["address"],
                    header["project"],
                    header["work_type"],
                    header["engineer"],
                    header["contact_no"],
                    header["notes"],
                    header["advance"] or 0,
                    total,
                    id,
                ),
            )

            cursor.execute("DELETE FROM QuotationDetails WHERE QuotationID = ?", (id,))
            for line in valid_lines:
                cursor.execute(
                    """
                    INSERT INTO QuotationDetails (
                        QuotationID, ItemID, Description, Width, Height, Qty, Rate, SqFt
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        id,
                        line["item_id"],
                        line["description"],
                        line["width"],
                        line["height"],
                        line["quantity"],
                        line["rate"],
                        line["sqft"],
                    ),
                )

            db.commit()
            flash("Quotation updated.", "success")
            return redirect(url_for("quotations.list_quotations"))

        return render_template(
            "quotations/form.html",
            customers=customers,
            errors=errors.errors,
            form_data=form_data,
            quotation_lines=quotation_lines,
            max_lines=MAX_LINE_ROWS,
            is_edit=True,
            quotation_id=id,
            quotation_no=quotation.QuotationNo,
        )

    except Exception as e:
        db.rollback()
        flash(f"Error updating quotation: {str(e)}", "danger")
        return render_template(
            "quotations/form.html",
            customers=[],
            errors=errors.errors,
            form_data=form_data,
            quotation_lines=quotation_lines,
            max_lines=MAX_LINE_ROWS,
            is_edit=True,
            quotation_id=id,
        )

    finally:
        cursor.close()


@quotations_bp.route("/list")
@login_required
def list_quotations():
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_quotations_schema(db, cursor)
        from app.perf import count_query, paginate_request, pagination_meta

        search = request.args.get("search", "")
        page, per_page, offset = paginate_request(default_per_page=50)
        base_where = f"WHERE {owner_sql()}"
        params = []
        if search:
            base_where += """
                AND (
                    CAST(QuotationNo AS VARCHAR(20)) LIKE ?
                    OR CustomerName LIKE ?
                    OR COALESCE(Project, '') LIKE ?
                )
            """
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

        total_count = count_query(
            cursor,
            f"SELECT COUNT(*) FROM Quotations {base_where}",
            params or (),
        )
        pagination = pagination_meta(page, per_page, total_count)
        page = pagination["page"]
        offset = (page - 1) * per_page

        query = f"""
            SELECT
                QuotationID, QuotationNo, QuotationDate, CustomerName, Project,
                WorkType, Advance, TotalAmount
            FROM Quotations
            {base_where}
            ORDER BY QuotationNo DESC
            LIMIT {per_page} OFFSET {offset}
        """
        cursor.execute(query, params or ())
        quotations = cursor.fetchall()
        return render_template(
            "quotations/list.html",
            quotations=quotations,
            search=search,
            pagination=pagination,
        )

    except Exception as e:
        flash(f"Error loading quotations: {str(e)}", "danger")
        return redirect(url_for("dashboard.dashboard"))

    finally:
        cursor.close()


@quotations_bp.route("/<int:id>/excel/share")
def quotation_excel_share(id):
    """Login-free Excel download — served to anyone with the direct URL."""
    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        ensure_quotations_schema(db, cursor)
        quotation = _load_quotation(cursor, id)
        if not quotation:
            abort(404)
        details = _load_quotation_details(cursor, id)
        return _send_quotation_excel(quotation, details)
    finally:
        cursor.close()


@quotations_bp.route("/<int:id>/excel")
@login_required
def quotation_excel(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_quotations_schema(db, cursor)
        quotation = _load_quotation(cursor, id)
        if not quotation:
            flash("Quotation not found.", "danger")
            return redirect(url_for("quotations.list_quotations"))
        details = _load_quotation_details(cursor, id)
        return _send_quotation_excel(quotation, details)

    except Exception as e:
        flash(f"Error generating quotation Excel: {str(e)}", "danger")
        return redirect(url_for("quotations.list_quotations"))

    finally:
        cursor.close()


@quotations_bp.route("/<int:id>/whatsapp", methods=["GET", "POST"])
@login_required
def quotation_whatsapp(id):
    db = get_db_connection(app)
    cursor = db.cursor()
    errors = ValidationErrors()

    try:
        ensure_quotations_schema(db, cursor)
        quotation = _load_quotation(cursor, id)
        if not quotation:
            flash("Quotation not found.", "danger")
            return redirect(url_for("quotations.list_quotations"))

        entered_number = (
            request.form.get("whatsapp_number") if request.method == "POST" else (quotation.ContactNo or "")
        )
        share_path = url_for("quotations.quotation_excel_share", id=id)

        if request.method == "POST":
            phone = clean_phone(entered_number, "whatsapp_number", errors, required=True, max_len=20)
            if errors.valid and not whatsapp_url(phone):
                errors.add("whatsapp_number", "Enter a valid WhatsApp mobile number.")
            if errors.valid:
                if wa_api_configured():
                    # Send actual Excel file via WhatsApp Business API
                    details = _load_quotation_details(cursor, id)
                    header, lines = _quotation_header_payload(quotation, details)
                    xl_buf = build_quotation_xlsx(header, lines)
                    xl_bytes = xl_buf.read()
                    caption = (
                        f"Quotation #{quotation.QuotationNo} — {quotation.CustomerName}\n"
                        f"Amount: Rs {float(quotation.TotalAmount or 0):,.2f}"
                    )
                    _result, err = wa_send_file(
                        phone, xl_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "QUOTATION.xlsx", caption,
                    )
                    if err:
                        flash(f"WhatsApp send failed: {err}", "danger")
                    else:
                        flash("Quotation Excel sent to WhatsApp.", "success")
                        return redirect(url_for("quotations.list_quotations"))
                else:
                    # Fallback: open wa.me with a download link in the text
                    xl_link = public_file_url(share_path)
                    message = (
                        f"Quotation #{quotation.QuotationNo} — {quotation.CustomerName}\n"
                        f"Amount: Rs {float(quotation.TotalAmount or 0):,.2f}\n"
                        f"Download Excel: {xl_link}"
                    )
                    return redirect(whatsapp_url(phone, message))
            else:
                flash(errors.first(), "danger")

        return render_template(
            "quotations/whatsapp.html",
            quotation=quotation,
            share_path=share_path,
            wa_api=wa_api_configured(),
            form_data={"whatsapp_number": entered_number or ""},
            errors=errors.errors,
        )

    except Exception as e:
        flash(f"Error opening WhatsApp quotation: {str(e)}", "danger")
        return redirect(url_for("quotations.list_quotations"))

    finally:
        cursor.close()


@quotations_bp.route("/delete/<int:id>", methods=["POST"])
@login_required
def delete_quotation(id):
    db = get_db_connection(app)
    cursor = db.cursor()

    try:
        ensure_quotations_schema(db, cursor)
        cursor.execute(f"SELECT QuotationID FROM Quotations WHERE QuotationID = ? AND {owner_sql()}", (id,))
        if not cursor.fetchone():
            flash("Quotation not found.", "danger")
            return redirect(url_for("quotations.list_quotations"))

        cursor.execute("DELETE FROM QuotationDetails WHERE QuotationID = ?", (id,))
        cursor.execute(f"DELETE FROM Quotations WHERE QuotationID = ? AND {owner_sql()}", (id,))
        db.commit()
        flash("Quotation deleted.", "success")

    except Exception as e:
        db.rollback()
        flash(f"Error deleting quotation: {str(e)}", "danger")

    finally:
        cursor.close()

    return redirect(url_for("quotations.list_quotations"))
