"""Export account data for offline desktop sync (Render → local SQLite)."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from hmac import compare_digest

from flask import Blueprint, jsonify, request
from flask_login import login_required
from werkzeug.security import check_password_hash

from app import app
from app.db import get_db_connection

sync_api_bp = Blueprint("sync_api", __name__, url_prefix="/api/sync")

# Owner-scoped tables first, then children (import order for the desktop client).
_OWNER_TABLES = (
    "Customers",
    "Supplier",
    "Category",
    "Item",
    "Purchases",
    "Invoices",
    "Quotations",
    "CashAccounts",
    "StockHistory",
    "LedgerEntries",
)

_CHILD_QUERIES = (
    (
        "PurchaseDetails",
        """
        SELECT d.* FROM PurchaseDetails d
        INNER JOIN Purchases p ON d.PurchaseID = p.PurchaseID
        WHERE p.UserID = ?
        """,
    ),
    (
        "PurchasePayments",
        """
        SELECT pay.* FROM PurchasePayments pay
        INNER JOIN Purchases p ON pay.PurchaseID = p.PurchaseID
        WHERE p.UserID = ?
        """,
    ),
    (
        "InvoiceDetails",
        """
        SELECT d.* FROM InvoiceDetails d
        INNER JOIN Invoices i ON d.InvoiceID = i.InvoiceID
        WHERE i.UserID = ?
        """,
    ),
    (
        "InvoicePayments",
        """
        SELECT pay.* FROM InvoicePayments pay
        INNER JOIN Invoices i ON pay.InvoiceID = i.InvoiceID
        WHERE i.UserID = ?
        """,
    ),
    (
        "QuotationDetails",
        """
        SELECT d.* FROM QuotationDetails d
        INNER JOIN Quotations q ON d.QuotationID = q.QuotationID
        WHERE q.UserID = ?
        """,
    ),
    (
        "SalesReturns",
        """
        SELECT r.* FROM SalesReturns r
        INNER JOIN Invoices i ON r.InvoiceID = i.InvoiceID
        WHERE i.UserID = ?
        """,
    ),
    (
        "SalesReturnDetails",
        """
        SELECT d.* FROM SalesReturnDetails d
        INNER JOIN SalesReturns r ON d.SalesReturnID = r.SalesReturnID
        INNER JOIN Invoices i ON r.InvoiceID = i.InvoiceID
        WHERE i.UserID = ?
        """,
    ),
)


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def _row_to_dict(row):
    if row is None:
        return None
    columns = getattr(row, "_columns", None)
    if columns:
        return {str(columns[i]): _json_safe(row[i]) for i in range(len(columns))}
    # Fallback if AttrRow shape differs
    return {str(k): _json_safe(getattr(row, k)) for k in dir(row) if not k.startswith("_")}


def _verify_password(stored, password):
    if not stored:
        return False
    stored = str(stored)
    try:
        if check_password_hash(stored, password):
            return True
    except ValueError:
        pass
    return compare_digest(stored, password)


def _fetch_table(cursor, sql, params=()):
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall() or []
        return [_row_to_dict(row) for row in rows]
    except Exception:
        return None


@sync_api_bp.route("/export", methods=["POST"])
def export_account():
    """
    POST JSON: {"username": "...", "password": "..."}
    Returns all rows for that account so the desktop app can replace local SQLite.
    """
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "username and password are required"}), 400

    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        cursor.execute(
            "SELECT UserID, Username, Password, Email, Phone FROM Users WHERE Username = ?",
            (username,),
        )
        user = cursor.fetchone()
        if not user or not _verify_password(user[2], password):
            return jsonify({"ok": False, "error": "Invalid username or password"}), 401

        user_id = int(user[0])
        tables = {
            "Users": [
                {
                    "user_id": user_id,
                    "username": user[1],
                    "password": user[2],
                    "email": user[3],
                    "phone": user[4],
                }
            ]
        }

        for table in _OWNER_TABLES:
            rows = _fetch_table(
                cursor,
                f"SELECT * FROM {table} WHERE UserID = ?",
                (user_id,),
            )
            if rows is not None:
                tables[table] = rows

        for table, sql in _CHILD_QUERIES:
            rows = _fetch_table(cursor, sql, (user_id,))
            if rows is not None:
                tables[table] = rows

        return jsonify(
            {
                "ok": True,
                "format": "euroglass-export-v1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "username": username,
                "tables": tables,
            }
        )
    finally:
        cursor.close()


def _desktop_mode() -> bool:
    flag = (os.environ.get("DESKTOP_MODE") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    try:
        return bool(app.config.get("DESKTOP_MODE"))
    except Exception:
        return False


@sync_api_bp.route("/run", methods=["POST"])
@login_required
def run_sync_now():
    """Manual local ↔ cloud sync from the desktop Sync button."""
    if not _desktop_mode():
        return jsonify({"ok": False, "error": "Sync button is only available in the desktop app."}), 403

    try:
        from desktop.auto_sync import sync_now
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Desktop sync is unavailable: {exc}"}), 500

    result = sync_now()
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@sync_api_bp.route("/status", methods=["GET"])
@login_required
def sync_status():
    if not _desktop_mode():
        return jsonify({"ok": True, "desktop": False})

    try:
        from desktop.sync import load_sync_state
        from desktop.sync_config import config_ready, load_sync_config
    except Exception as exc:
        return jsonify({"ok": False, "desktop": True, "error": str(exc)}), 500

    cfg = load_sync_config() or {}
    state = load_sync_state() or {}
    return jsonify(
        {
            "ok": True,
            "desktop": True,
            "configured": config_ready(cfg),
            "username": cfg.get("username") or "",
            "last_sync_at": state.get("last_sync_at"),
            "last_status": state.get("last_status"),
            "last_message": state.get("last_message"),
        }
    )
