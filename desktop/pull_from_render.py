"""
Pull your cloud (Supabase/Render Postgres) account into local offline SQLite.

No Google login in this app — only username/password.
If you only have the database URL, use --no-password to pick a user from the DB
and set a new offline password.

  python desktop/pull_from_render.py --database-url "postgresql://..." --no-password

WARNING: Replaces local offline data with the cloud copy for that account.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from desktop.paths import database_path
from desktop.sync import load_sync_state, save_sync_state

_TABLE_MAP = {
    "Users": "users",
    "Customers": "customers",
    "Supplier": "supplier",
    "Category": "category",
    "Item": "item",
    "Purchases": "purchases",
    "PurchaseDetails": "purchase_details",
    "PurchasePayments": "purchase_payments",
    "Invoices": "invoices",
    "InvoiceDetails": "invoice_details",
    "InvoicePayments": "invoice_payments",
    "Quotations": "quotations",
    "QuotationDetails": "quotation_details",
    "CashAccounts": "cash_accounts",
    "StockHistory": "stock_history",
    "LedgerEntries": "ledger_entries",
    "SalesReturns": "sales_returns",
    "SalesReturnDetails": "sales_return_details",
}

_IMPORT_ORDER = tuple(_TABLE_MAP.keys())
_CLEAR_ORDER = tuple(reversed(_IMPORT_ORDER))

_SALES_RETURN_DDL = """
CREATE TABLE IF NOT EXISTS sales_returns (
    sales_return_id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    return_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount NUMERIC(12, 2) NOT NULL,
    notes VARCHAR(255)
);
CREATE TABLE IF NOT EXISTS sales_return_details (
    sales_return_detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sales_return_id INTEGER NOT NULL REFERENCES sales_returns(sales_return_id) ON DELETE CASCADE,
    invoice_detail_id INTEGER,
    item_id INTEGER NOT NULL REFERENCES item(item_id),
    particulars VARCHAR(255),
    qty INTEGER NOT NULL,
    rate NUMERIC(12, 2) NOT NULL,
    line_amount NUMERIC(12, 2) NOT NULL
);
"""


def _to_snake(name: str) -> str:
    name = str(name)
    if "_" in name and name == name.lower():
        return name
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("__", "_").lower()


def _normalize_row(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        out[_to_snake(key)] = value
    if "lineamount" in out and "line_amount" not in out:
        out["line_amount"] = out.pop("lineamount")
    if "salesreturnid" in out and "sales_return_id" not in out:
        out["sales_return_id"] = out.pop("salesreturnid")
    if "salesreturndetailid" in out and "sales_return_detail_id" not in out:
        out["sales_return_detail_id"] = out.pop("salesreturndetailid")
    if "invoicedetailid" in out and "invoice_detail_id" not in out:
        out["invoice_detail_id"] = out.pop("invoicedetailid")
    if "returndate" in out and "return_date" not in out:
        out["return_date"] = out.pop("returndate")
    return out


def _ensure_local_schema(conn: sqlite3.Connection) -> None:
    schema = _ROOT / "schema_sqlite.sql"
    if not schema.exists():
        raise FileNotFoundError(f"Missing schema file: {schema}")
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.executescript(_SALES_RETURN_DDL)
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _clear_local(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for logical in _CLEAR_ORDER:
        table = _TABLE_MAP[logical]
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.Error:
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def _insert_rows(conn: sqlite3.Connection, logical: str, rows: list) -> int:
    if not rows:
        return 0
    table = _TABLE_MAP[logical]
    columns = set(_table_columns(conn, table))
    if not columns:
        return 0

    count = 0
    for raw in rows:
        row = _normalize_row(dict(raw))
        keys = [k for k in row.keys() if k in columns]
        if not keys:
            continue
        placeholders = ", ".join("?" for _ in keys)
        col_sql = ", ".join(keys)
        values = [row[k] for k in keys]
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
            values,
        )
        count += 1
    return count


def _fix_sequences(conn: sqlite3.Connection) -> None:
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in tables:
        cols = _table_columns(conn, name)
        if not cols:
            continue
        pk = cols[0]
        try:
            max_id = conn.execute(f"SELECT MAX({pk}) FROM {name}").fetchone()[0]
        except sqlite3.Error:
            continue
        if max_id is None:
            continue
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (name,))
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
            (name, int(max_id)),
        )


def apply_export_to_local(export: dict, db_file: Path | None = None) -> dict:
    path = db_file or database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        _ensure_local_schema(conn)
        _clear_local(conn)
        conn.execute("PRAGMA foreign_keys = OFF")
        counts = {}
        tables = export.get("tables") or {}
        for logical in _IMPORT_ORDER:
            rows = tables.get(logical) or []
            if not isinstance(rows, list):
                continue
            counts[logical] = _insert_rows(conn, logical, rows)
        _fix_sequences(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        return {"database": str(path), "counts": counts}
    finally:
        conn.close()


def export_via_http(base_url: str, username: str, password: str) -> dict:
    url = base_url.rstrip("/") + "/api/sync/export"
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except Exception:
            pass
        raise SystemExit(f"Export failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise SystemExit(
            f"Could not reach {url}\n"
            f"  {exc}\n"
            "Use --database-url with your Supabase/Postgres URI instead."
        ) from exc

    if not payload.get("ok"):
        raise SystemExit(payload.get("error") or "Export failed")
    return payload


def _convert_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if type(value).__name__ == "Decimal":
        return float(value)
    return value


def _convert_rows(rows) -> list[dict]:
    return [{k: _convert_value(v) for k, v in dict(row).items()} for row in rows]


def export_via_database_url(
    database_url: str,
    username: str | None = None,
    password: str | None = None,
    *,
    skip_password: bool = False,
    new_local_password: str | None = None,
) -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise SystemExit(
            'psycopg is required for --database-url. Run:\n'
            '  python -m pip install "psycopg[binary]"'
        ) from exc

    from hmac import compare_digest

    from werkzeug.security import check_password_hash, generate_password_hash

    url = database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    def verify(stored, plain) -> bool:
        if not stored:
            return False
        stored = str(stored)
        try:
            if check_password_hash(stored, plain):
                return True
        except ValueError:
            pass
        return compare_digest(stored, plain)

    with psycopg.connect(url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT user_id, username, password, email, phone FROM users "
                "ORDER BY user_id"
            )
            all_users = list(cur.fetchall())
            if not all_users:
                raise SystemExit("No users found in the database.")

            user = None
            if username:
                for row in all_users:
                    if str(row["username"]).lower() == username.lower():
                        user = row
                        break
                if user is None:
                    names = ", ".join(str(u["username"]) for u in all_users)
                    raise SystemExit(f"Username '{username}' not found. Users: {names}")
                if not skip_password:
                    if not password or not verify(user["password"], password):
                        raise SystemExit("Invalid username or password.")
            else:
                print("\nAccounts found in the database:")
                for i, row in enumerate(all_users, start=1):
                    email = row.get("email") or "-"
                    print(f"  {i}. {row['username']}  (email: {email})")
                choice = input("\nEnter number to import: ").strip()
                try:
                    user = all_users[int(choice) - 1]
                except (ValueError, IndexError) as exc:
                    raise SystemExit("Invalid selection.") from exc

            username = str(user["username"])
            user_id = int(user["user_id"])
            stored_password = user["password"]
            if new_local_password:
                stored_password = generate_password_hash(new_local_password)

            tables: dict[str, list] = {
                "Users": [
                    {
                        "user_id": user_id,
                        "username": username,
                        "password": stored_password,
                        "email": user.get("email"),
                        "phone": user.get("phone"),
                    }
                ]
            }

            owner_sql = {
                "Customers": "SELECT * FROM customers WHERE user_id = %s",
                "Supplier": "SELECT * FROM supplier WHERE user_id = %s",
                "Category": "SELECT * FROM category WHERE user_id = %s",
                "Item": "SELECT * FROM item WHERE user_id = %s",
                "Purchases": "SELECT * FROM purchases WHERE user_id = %s",
                "Invoices": "SELECT * FROM invoices WHERE user_id = %s",
                "Quotations": "SELECT * FROM quotations WHERE user_id = %s",
                "CashAccounts": "SELECT * FROM cash_accounts WHERE user_id = %s",
                "StockHistory": "SELECT * FROM stock_history WHERE user_id = %s",
                "LedgerEntries": "SELECT * FROM ledger_entries WHERE user_id = %s",
            }
            for logical, sql in owner_sql.items():
                try:
                    cur.execute(sql, (user_id,))
                    tables[logical] = _convert_rows(cur.fetchall())
                except Exception:
                    conn.rollback()
                    tables[logical] = []

            child_sql = {
                "PurchaseDetails": """
                    SELECT d.* FROM purchase_details d
                    INNER JOIN purchases p ON d.purchase_id = p.purchase_id
                    WHERE p.user_id = %s
                """,
                "PurchasePayments": """
                    SELECT pay.* FROM purchase_payments pay
                    INNER JOIN purchases p ON pay.purchase_id = p.purchase_id
                    WHERE p.user_id = %s
                """,
                "InvoiceDetails": """
                    SELECT d.* FROM invoice_details d
                    INNER JOIN invoices i ON d.invoice_id = i.invoice_id
                    WHERE i.user_id = %s
                """,
                "InvoicePayments": """
                    SELECT pay.* FROM invoice_payments pay
                    INNER JOIN invoices i ON pay.invoice_id = i.invoice_id
                    WHERE i.user_id = %s
                """,
                "QuotationDetails": """
                    SELECT d.* FROM quotation_details d
                    INNER JOIN quotations q ON d.quotation_id = q.quotation_id
                    WHERE q.user_id = %s
                """,
            }
            for logical, sql in child_sql.items():
                try:
                    cur.execute(sql, (user_id,))
                    tables[logical] = _convert_rows(cur.fetchall())
                except Exception:
                    conn.rollback()
                    tables[logical] = []

            sales_queries = [
                (
                    "SalesReturns",
                    """
                    SELECT r.* FROM sales_returns r
                    INNER JOIN invoices i ON r.invoice_id = i.invoice_id
                    WHERE i.user_id = %s
                    """,
                ),
                (
                    "SalesReturns",
                    """
                    SELECT r.* FROM salesreturns r
                    INNER JOIN invoices i ON r.invoiceid = i.invoice_id
                    WHERE i.user_id = %s
                    """,
                ),
                (
                    "SalesReturnDetails",
                    """
                    SELECT d.* FROM sales_return_details d
                    INNER JOIN sales_returns r ON d.sales_return_id = r.sales_return_id
                    INNER JOIN invoices i ON r.invoice_id = i.invoice_id
                    WHERE i.user_id = %s
                    """,
                ),
                (
                    "SalesReturnDetails",
                    """
                    SELECT d.* FROM salesreturndetails d
                    INNER JOIN salesreturns r ON d.salesreturnid = r.salesreturnid
                    INNER JOIN invoices i ON r.invoiceid = i.invoice_id
                    WHERE i.user_id = %s
                    """,
                ),
            ]
            for logical, sql in sales_queries:
                if tables.get(logical):
                    continue
                try:
                    cur.execute(sql, (user_id,))
                    tables[logical] = _convert_rows(cur.fetchall())
                except Exception:
                    conn.rollback()
                    tables.setdefault(logical, [])

            return {
                "ok": True,
                "format": "euroglass-export-v1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "username": username,
                "tables": tables,
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download cloud account data into the offline SQLite database."
    )
    parser.add_argument("--url", default=os.environ.get("SYNC_REMOTE_URL") or "")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RENDER_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "",
    )
    parser.add_argument("--username", default=os.environ.get("SYNC_USERNAME") or "")
    parser.add_argument("--password", default=os.environ.get("SYNC_PASSWORD") or "")
    parser.add_argument(
        "--no-password",
        action="store_true",
        help="With --database-url: pick a user from the DB; no app password needed.",
    )
    parser.add_argument(
        "--set-password",
        default="",
        help="New offline login password for the imported account.",
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    db_url = (args.database_url or "").strip()
    use_db = bool(db_url) and not db_url.lower().startswith("sqlite:")

    username = (args.username or "").strip() or None
    password = args.password or None
    new_local_password = (args.set_password or "").strip() or None
    skip_password = bool(args.no_password) or (use_db and not password and not username)

    if use_db and skip_password:
        print(
            "This stock app has no Google login — only username/password.\n"
            "Because you have the database URL, we can import without the old password.\n"
        )
        if not new_local_password:
            new_local_password = getpass.getpass(
                "Choose a NEW password for offline login: "
            ).strip()
            confirm_pw = getpass.getpass("Confirm new password: ").strip()
            if not new_local_password or new_local_password != confirm_pw:
                print("Passwords missing or did not match.", file=sys.stderr)
                return 1
    else:
        if not username:
            username = input("App username: ").strip() or None
        if not skip_password and not password:
            password = getpass.getpass("App password: ")
        if not username:
            print("Username is required.", file=sys.stderr)
            return 1
        if not skip_password and not password:
            print(
                "Password required, or use --no-password with --database-url.",
                file=sys.stderr,
            )
            return 1

    db_file = database_path()
    print(f"Local database: {db_file}")
    print("This will REPLACE all local offline data with your cloud account.")
    if not args.yes:
        confirm = input("Type YES to continue: ").strip()
        if confirm != "YES":
            print("Cancelled.")
            return 1

    if use_db:
        print("Pulling from Postgres (Supabase/Render)...")
        export = export_via_database_url(
            db_url,
            username,
            password,
            skip_password=skip_password,
            new_local_password=new_local_password,
        )
        method = "database_url"
        username = export.get("username") or username
    elif args.url:
        if not username or not password:
            print("Username and password are required for --url.", file=sys.stderr)
            return 1
        print(f"Pulling via {args.url.rstrip('/')}/api/sync/export ...")
        export = export_via_http(args.url, username, password)
        method = "http"
    else:
        print(
            "Provide --database-url \"postgresql://...\" (from Supabase Connect)\n"
            "or --url https://YOUR-APP.onrender.com",
            file=sys.stderr,
        )
        return 1

    result = apply_export_to_local(export, db_file)
    state = load_sync_state()
    state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    state["last_status"] = "pulled_from_cloud"
    state["last_message"] = f"Imported via {method}"
    state["remote_url"] = args.url or state.get("remote_url") or ""
    state["counts"] = result["counts"]
    save_sync_state(state)

    print("Import complete.")
    for name, count in result["counts"].items():
        if count:
            print(f"  {name}: {count}")
    print()
    print("Next:  python desktop\\launcher.py")
    print(f"Username: {username}")
    if new_local_password:
        print("Password: (the NEW offline password you chose)")
    else:
        print("Password: your usual app password")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
