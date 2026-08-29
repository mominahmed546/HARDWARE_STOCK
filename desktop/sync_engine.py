"""
Bidirectional sync: local SQLite ↔ cloud Postgres (Supabase / Render).

Modes:
  pull  — cloud overwrites local
  push  — local overwrites cloud (same account)
  sync  — merge both; conflicts use prefer=local|remote
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# logical, sqlite_table, postgres_name_candidates, primary_key
TABLES: list[tuple[str, str, tuple[str, ...], str]] = [
    ("Users", "users", ("users",), "user_id"),
    ("Customers", "customers", ("customers",), "customer_id"),
    ("Supplier", "supplier", ("supplier",), "supplier_id"),
    ("Category", "category", ("category",), "category_id"),
    ("Item", "item", ("item",), "item_id"),
    ("Purchases", "purchases", ("purchases",), "purchase_id"),
    ("PurchaseDetails", "purchase_details", ("purchase_details", "purchasedetails"), "detail_id"),
    ("PurchasePayments", "purchase_payments", ("purchase_payments", "purchasepayments"), "payment_id"),
    ("Invoices", "invoices", ("invoices",), "invoice_id"),
    ("InvoiceDetails", "invoice_details", ("invoice_details", "invoicedetails"), "detail_id"),
    ("InvoicePayments", "invoice_payments", ("invoice_payments", "invoicepayments"), "payment_id"),
    ("Quotations", "quotations", ("quotations",), "quotation_id"),
    ("QuotationDetails", "quotation_details", ("quotation_details", "quotationdetails"), "detail_id"),
    ("CashAccounts", "cash_accounts", ("cash_accounts", "cashaccounts"), "account_id"),
    ("StockHistory", "stock_history", ("stock_history", "stockhistory"), "history_id"),
    ("LedgerEntries", "ledger_entries", ("ledger_entries", "ledgerentries"), "entry_id"),
    ("SalesReturns", "sales_returns", ("sales_returns", "salesreturns"), "sales_return_id"),
    (
        "SalesReturnDetails",
        "sales_return_details",
        ("sales_return_details", "salesreturndetails"),
        "sales_return_detail_id",
    ),
]

IMPORT_ORDER = [t[0] for t in TABLES]
CLEAR_ORDER = list(reversed(IMPORT_ORDER))
TABLE_BY_LOGICAL = {t[0]: t for t in TABLES}

SALES_RETURN_DDL = """
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


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def normalize_row(row: dict) -> dict:
    out = {_to_snake(k): _jsonable(v) for k, v in dict(row).items()}
    aliases = {
        "lineamount": "line_amount",
        "salesreturnid": "sales_return_id",
        "salesreturndetailid": "sales_return_detail_id",
        "invoicedetailid": "invoice_detail_id",
        "returndate": "return_date",
        "invoiceid": "invoice_id",
        "purchaseid": "purchase_id",
        "quotationid": "quotation_id",
        "historyid": "history_id",
        "entryid": "entry_id",
        "accountid": "account_id",
        "userid": "user_id",
    }
    for old, new in aliases.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


def _row_pk(row: dict, pk: str):
    val = row.get(pk)
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return val


def _rows_equal(a: dict, b: dict) -> bool:
    keys = set(a) | set(b)
    for key in keys:
        av, bv = a.get(key), b.get(key)
        if av is None and bv is None:
            continue
        if isinstance(av, float) or isinstance(bv, float):
            try:
                if abs(float(av or 0) - float(bv or 0)) < 1e-6:
                    continue
            except (TypeError, ValueError):
                pass
        if str(av) != str(bv):
            return False
    return True


def normalize_database_url(database_url: str) -> str:
    """Strip quotes/whitespace and normalize postgres:// → postgresql://."""
    url = (database_url or "").strip().strip("\ufeff")
    # Users often paste quoted URLs from docs / chat / Excel (ascii or “smart” quotes).
    quote_chars = {"'", '"', "`", "“", "”", "‘", "’"}
    changed = True
    while changed and len(url) >= 2:
        changed = False
        if url[0] in quote_chars and url[-1] in quote_chars:
            url = url[1:-1].strip()
            changed = True
    # Leading-only quote (common when paste drops the closing quote).
    while url and url[0] in quote_chars:
        url = url[1:].strip()
    while url and url[-1] in quote_chars:
        url = url[:-1].strip()

    # If junk remains before the scheme, keep from the first postgres URI.
    lowered = url.lower()
    for marker in ("postgresql://", "postgres://"):
        idx = lowered.find(marker)
        if idx > 0:
            url = url[idx:]
            lowered = url.lower()
            break

    url = url.strip()
    if url.lower().startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _pg_connect(database_url: str):
    """Connect via kwargs so malformed/quoted URIs don't hit libpq conninfo errors."""
    import psycopg
    from psycopg.rows import dict_row
    from urllib.parse import parse_qs, unquote, urlparse

    url = normalize_database_url(database_url)
    if not url:
        raise ValueError(
            "Cloud database URL is empty. Delete "
            r"%LOCALAPPDATA%\EuroglassHardware\sync_config.json "
            "and restart, then paste the Supabase URI without quotes."
        )

    # Prefer keyword args: avoids "missing = after connection string" when the
    # saved value has quotes, BOM, or an old libpq that mishandles URIs.
    if "://" in url:
        parsed = urlparse(url)
        if parsed.scheme not in {"postgresql", "postgres"}:
            preview = url[:60] + ("…" if len(url) > 60 else "")
            raise ValueError(
                f"Unsupported database URL scheme: {parsed.scheme!r} "
                f"(value starts with {preview!r}). "
                r"Delete %LOCALAPPDATA%\EuroglassHardware\sync_config.json "
                "and paste the URI without quotes."
            )
        host = parsed.hostname
        if not host:
            raise ValueError("Cloud database URL is missing a host.")
        user = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password is not None else None
        dbname = unquote((parsed.path or "/").lstrip("/") or "postgres")
        port = parsed.port or 5432
        query = parse_qs(parsed.query, keep_blank_values=True)
        sslmode = (query.get("sslmode") or ["require"])[0] or "require"
        kwargs: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "sslmode": sslmode,
            "row_factory": dict_row,
        }
        if user:
            kwargs["user"] = user
        if password is not None:
            kwargs["password"] = password
        # Session pooler / Supabase: keep connect timeout reasonable on Windows.
        if "connect_timeout" in query:
            try:
                kwargs["connect_timeout"] = int(query["connect_timeout"][0])
            except (TypeError, ValueError, IndexError):
                pass
        else:
            kwargs["connect_timeout"] = 15
        return psycopg.connect(**kwargs)

    # Already keyword/value conninfo (host=... dbname=...).
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=15)


def _resolve_pg_table(cur, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND lower(table_name) = lower(%s)
            """,
            (name,),
        )
        if cur.fetchone():
            return name
    return None


def _pg_columns(cur, table: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r["column_name"] for r in cur.fetchall()]


def authenticate_pg_user(database_url: str, username: str, password: str) -> dict:
    from hmac import compare_digest

    from werkzeug.security import check_password_hash

    with _pg_connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, username, password, email, phone FROM users WHERE username = %s",
                (username,),
            )
            user = cur.fetchone()
    if not user:
        raise SystemExit("Invalid username or password.")
    stored = str(user["password"] or "")
    ok = False
    try:
        ok = check_password_hash(stored, password)
    except ValueError:
        ok = False
    if not ok:
        ok = compare_digest(stored, password)
    if not ok:
        raise SystemExit("Invalid username or password.")
    return dict(user)


def _fetch_related(cur, logical: str, pg_table: str, cols: list[str], user_id: int) -> list[dict]:
    if logical == "Users" or "user_id" in cols:
        cur.execute(f'SELECT * FROM "{pg_table}" WHERE user_id = %s', (user_id,))
        return [normalize_row(dict(r)) for r in cur.fetchall()]

    if logical in {"PurchaseDetails", "PurchasePayments"}:
        cur.execute(
            f'''
            SELECT t.* FROM "{pg_table}" t
            INNER JOIN purchases p ON t.purchase_id = p.purchase_id
            WHERE p.user_id = %s
            ''',
            (user_id,),
        )
    elif logical in {"InvoiceDetails", "InvoicePayments"}:
        cur.execute(
            f'''
            SELECT t.* FROM "{pg_table}" t
            INNER JOIN invoices i ON t.invoice_id = i.invoice_id
            WHERE i.user_id = %s
            ''',
            (user_id,),
        )
    elif logical == "QuotationDetails":
        cur.execute(
            f'''
            SELECT t.* FROM "{pg_table}" t
            INNER JOIN quotations q ON t.quotation_id = q.quotation_id
            WHERE q.user_id = %s
            ''',
            (user_id,),
        )
    elif logical == "SalesReturns":
        join_col = "invoice_id" if "invoice_id" in cols else "invoiceid"
        cur.execute(
            f'''
            SELECT t.* FROM "{pg_table}" t
            INNER JOIN invoices i ON t.{join_col} = i.invoice_id
            WHERE i.user_id = %s
            ''',
            (user_id,),
        )
    elif logical == "SalesReturnDetails":
        sr = _resolve_pg_table(cur, ("sales_returns", "salesreturns"))
        if not sr:
            return []
        sr_cols = _pg_columns(cur, sr)
        sr_pk = "sales_return_id" if "sales_return_id" in sr_cols else "salesreturnid"
        inv_join = "invoice_id" if "invoice_id" in sr_cols else "invoiceid"
        join_col = "sales_return_id" if "sales_return_id" in cols else "salesreturnid"
        cur.execute(
            f'''
            SELECT t.* FROM "{pg_table}" t
            INNER JOIN "{sr}" r ON t.{join_col} = r.{sr_pk}
            INNER JOIN invoices i ON r.{inv_join} = i.invoice_id
            WHERE i.user_id = %s
            ''',
            (user_id,),
        )
    else:
        return []
    return [normalize_row(dict(r)) for r in cur.fetchall()]


def fetch_remote_tables(database_url: str, user_id: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with _pg_connect(database_url) as conn:
        with conn.cursor() as cur:
            for logical, _sqlite, candidates, _pk in TABLES:
                pg_table = _resolve_pg_table(cur, candidates)
                if not pg_table:
                    out[logical] = []
                    continue
                cols = _pg_columns(cur, pg_table)
                try:
                    out[logical] = _fetch_related(cur, logical, pg_table, cols, user_id)
                except Exception:
                    conn.rollback()
                    out[logical] = []
    return out


def _ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    schema = ROOT / "schema_sqlite.sql"
    if not schema.exists():
        raise FileNotFoundError(f"Missing schema: {schema}")
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.executescript(SALES_RETURN_DDL)
    conn.commit()


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def fetch_local_tables(db_path: Path, user_id: int) -> dict[str, list[dict]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_sqlite_schema(conn)
        out: dict[str, list[dict]] = {}
        for logical, sqlite_table, _c, _pk in TABLES:
            cols = _sqlite_columns(conn, sqlite_table)
            if not cols:
                out[logical] = []
                continue
            try:
                if logical == "Users" or "user_id" in cols:
                    rows = conn.execute(
                        f"SELECT * FROM {sqlite_table} WHERE user_id = ?",
                        (user_id,),
                    ).fetchall()
                elif logical in {"PurchaseDetails", "PurchasePayments"}:
                    rows = conn.execute(
                        f"""
                        SELECT t.* FROM {sqlite_table} t
                        INNER JOIN purchases p ON t.purchase_id = p.purchase_id
                        WHERE p.user_id = ?
                        """,
                        (user_id,),
                    ).fetchall()
                elif logical in {"InvoiceDetails", "InvoicePayments", "SalesReturns"}:
                    rows = conn.execute(
                        f"""
                        SELECT t.* FROM {sqlite_table} t
                        INNER JOIN invoices i ON t.invoice_id = i.invoice_id
                        WHERE i.user_id = ?
                        """,
                        (user_id,),
                    ).fetchall()
                elif logical == "SalesReturnDetails":
                    rows = conn.execute(
                        f"""
                        SELECT t.* FROM {sqlite_table} t
                        INNER JOIN sales_returns r ON t.sales_return_id = r.sales_return_id
                        INNER JOIN invoices i ON r.invoice_id = i.invoice_id
                        WHERE i.user_id = ?
                        """,
                        (user_id,),
                    ).fetchall()
                elif logical == "QuotationDetails":
                    rows = conn.execute(
                        f"""
                        SELECT t.* FROM {sqlite_table} t
                        INNER JOIN quotations q ON t.quotation_id = q.quotation_id
                        WHERE q.user_id = ?
                        """,
                        (user_id,),
                    ).fetchall()
                else:
                    rows = []
                out[logical] = [normalize_row(dict(r)) for r in rows]
            except sqlite3.Error:
                out[logical] = []
        return out
    finally:
        conn.close()


def merge_tables(
    local: dict[str, list[dict]],
    remote: dict[str, list[dict]],
    prefer: str = "local",
) -> tuple[dict[str, list[dict]], dict]:
    prefer = (prefer or "local").lower()
    if prefer not in {"local", "remote"}:
        prefer = "local"

    merged: dict[str, list[dict]] = {}
    stats = {
        "only_local": 0,
        "only_remote": 0,
        "same": 0,
        "conflict_local": 0,
        "conflict_remote": 0,
    }

    for logical in IMPORT_ORDER:
        pk = TABLE_BY_LOGICAL[logical][3]
        local_map = {_row_pk(r, pk): r for r in (local.get(logical) or []) if _row_pk(r, pk) is not None}
        remote_map = {_row_pk(r, pk): r for r in (remote.get(logical) or []) if _row_pk(r, pk) is not None}
        out = {}
        for key in set(local_map) | set(remote_map):
            lrow, rrow = local_map.get(key), remote_map.get(key)
            if lrow and not rrow:
                out[key] = lrow
                stats["only_local"] += 1
            elif rrow and not lrow:
                out[key] = rrow
                stats["only_remote"] += 1
            elif lrow and rrow:
                if _rows_equal(lrow, rrow):
                    out[key] = lrow
                    stats["same"] += 1
                elif prefer == "remote":
                    out[key] = rrow
                    stats["conflict_remote"] += 1
                else:
                    out[key] = lrow
                    stats["conflict_local"] += 1
        merged[logical] = list(out.values())

    merged = _dedupe_business_keys(merged, prefer=prefer)
    return merged, stats


def _dedupe_business_keys(
    tables: dict[str, list[dict]],
    prefer: str = "local",
) -> dict[str, list[dict]]:
    """
    Collapse rows that would violate secondary unique keys.
    Example: two quotation_ids both with quotation_no=1.
    """
    rules = {
        "Quotations": ("user_id", "quotation_no"),
        "Item": ("user_id", "item_no"),
        "CashAccounts": ("user_id",),
        "Users": ("username",),
    }
    out = dict(tables)
    for logical, key_fields in rules.items():
        rows = list(out.get(logical) or [])
        if not rows:
            continue
        pk = TABLE_BY_LOGICAL[logical][3]
        buckets: dict[tuple, dict] = {}
        for row in rows:
            row = normalize_row(row)
            # Skip incomplete business keys (e.g. null item_no)
            key_vals = []
            skip = False
            for field in key_fields:
                val = row.get(field)
                if val is None or val == "":
                    skip = True
                    break
                key_vals.append(val)
            if skip:
                # keep rows with null business key keyed by PK only
                buckets[("__pk__", _row_pk(row, pk))] = row
                continue
            key = tuple(key_vals)
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = row
                continue
            # Keep higher PK as tie-breaker (stable, deterministic)
            if (_row_pk(row, pk) or 0) >= (_row_pk(existing, pk) or 0):
                buckets[key] = row
        out[logical] = list(buckets.values())
    return out


def _prepare_postgres_constraints(cur) -> None:
    """Drop legacy global uniques that break per-account sync."""
    drop_statements = (
        "ALTER TABLE quotations DROP CONSTRAINT IF EXISTS quotations_quotation_no_key",
        'ALTER TABLE "Quotations" DROP CONSTRAINT IF EXISTS "Quotations_QuotationNo_key"',
        "DROP INDEX IF EXISTS quotations_quotation_no_key",
        "DROP INDEX IF EXISTS idx_quotations_quotation_no",
        # Also drop any unique index solely on quotation_no
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN
            SELECT i.relname AS index_name
            FROM pg_index x
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname = 'quotations'
              AND x.indisunique
              AND NOT x.indisprimary
              AND pg_get_indexdef(x.indexrelid) ILIKE '%(quotation_no)%'
              AND pg_get_indexdef(x.indexrelid) NOT ILIKE '%user_id%'
          LOOP
            EXECUTE format('DROP INDEX IF EXISTS %I', r.index_name);
          END LOOP;
        END $$;
        """,
    )
    for sql in drop_statements:
        try:
            cur.execute("SAVEPOINT prep_constraints")
            cur.execute(sql)
            cur.execute("RELEASE SAVEPOINT prep_constraints")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT prep_constraints")


def _ensure_per_user_indexes(cur) -> None:
    statements = (
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quotations_user_no
        ON quotations (user_id, quotation_no)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_item_user_no
        ON item (user_id, item_no)
        WHERE item_no IS NOT NULL
        """,
    )
    for sql in statements:
        try:
            cur.execute("SAVEPOINT prep_indexes")
            cur.execute(sql)
            cur.execute("RELEASE SAVEPOINT prep_indexes")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT prep_indexes")


def write_local_tables(db_path: Path, user_id: int, tables: dict[str, list[dict]]) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tables = _dedupe_business_keys(tables, prefer="local")
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_sqlite_schema(conn)
        conn.execute("PRAGMA foreign_keys = OFF")

        for logical in CLEAR_ORDER:
            sqlite_table = TABLE_BY_LOGICAL[logical][1]
            try:
                conn.execute(f"DELETE FROM {sqlite_table}")
            except sqlite3.Error:
                pass

        counts = {}
        for logical in IMPORT_ORDER:
            sqlite_table = TABLE_BY_LOGICAL[logical][1]
            cols = set(_sqlite_columns(conn, sqlite_table))
            n = 0
            for raw in tables.get(logical) or []:
                row = normalize_row(raw)
                if "user_id" in cols and logical != "Users":
                    row.setdefault("user_id", user_id)
                keys = [k for k in row.keys() if k in cols]
                if not keys:
                    continue
                conn.execute(
                    f"INSERT OR REPLACE INTO {sqlite_table} ({', '.join(keys)}) "
                    f"VALUES ({', '.join('?' for _ in keys)})",
                    [row[k] for k in keys],
                )
                n += 1
            counts[logical] = n

        for logical, sqlite_table, _c, pk in TABLES:
            try:
                max_id = conn.execute(f"SELECT MAX({pk}) FROM {sqlite_table}").fetchone()[0]
                if max_id is not None:
                    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (sqlite_table,))
                    conn.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES(?, ?)",
                        (sqlite_table, int(max_id)),
                    )
            except sqlite3.Error:
                pass

        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
        return counts
    finally:
        conn.close()


def _delete_remote_user_data(cur, user_id: int) -> None:
    for logical in CLEAR_ORDER:
        if logical == "Users":
            continue
        _sqlite, candidates, _pk = (
            TABLE_BY_LOGICAL[logical][1],
            TABLE_BY_LOGICAL[logical][2],
            TABLE_BY_LOGICAL[logical][3],
        )
        pg_table = _resolve_pg_table(cur, candidates)
        if not pg_table:
            continue
        cols = _pg_columns(cur, pg_table)
        if "user_id" in cols:
            cur.execute(f'DELETE FROM "{pg_table}" WHERE user_id = %s', (user_id,))
        elif logical in {"PurchaseDetails", "PurchasePayments"}:
            cur.execute(
                f'''
                DELETE FROM "{pg_table}" t
                USING purchases p
                WHERE t.purchase_id = p.purchase_id AND p.user_id = %s
                ''',
                (user_id,),
            )
        elif logical in {"InvoiceDetails", "InvoicePayments"}:
            cur.execute(
                f'''
                DELETE FROM "{pg_table}" t
                USING invoices i
                WHERE t.invoice_id = i.invoice_id AND i.user_id = %s
                ''',
                (user_id,),
            )
        elif logical == "QuotationDetails":
            cur.execute(
                f'''
                DELETE FROM "{pg_table}" t
                USING quotations q
                WHERE t.quotation_id = q.quotation_id AND q.user_id = %s
                ''',
                (user_id,),
            )
        elif logical == "SalesReturns":
            join_col = "invoice_id" if "invoice_id" in cols else "invoiceid"
            cur.execute(
                f'''
                DELETE FROM "{pg_table}" t
                USING invoices i
                WHERE t.{join_col} = i.invoice_id AND i.user_id = %s
                ''',
                (user_id,),
            )
        elif logical == "SalesReturnDetails":
            sr = _resolve_pg_table(cur, ("sales_returns", "salesreturns"))
            if not sr:
                continue
            sr_cols = _pg_columns(cur, sr)
            sr_pk = "sales_return_id" if "sales_return_id" in sr_cols else "salesreturnid"
            inv_join = "invoice_id" if "invoice_id" in sr_cols else "invoiceid"
            join_col = "sales_return_id" if "sales_return_id" in cols else "salesreturnid"
            cur.execute(
                f'''
                DELETE FROM "{pg_table}" t
                USING "{sr}" r, invoices i
                WHERE t.{join_col} = r.{sr_pk}
                  AND r.{inv_join} = i.invoice_id
                  AND i.user_id = %s
                ''',
                (user_id,),
            )


def write_remote_tables(database_url: str, user_id: int, tables: dict[str, list[dict]]) -> dict:
    counts: dict[str, int] = {}
    tables = _dedupe_business_keys(tables, prefer="local")
    with _pg_connect(database_url) as conn:
        with conn.cursor() as cur:
            _prepare_postgres_constraints(cur)
            _delete_remote_user_data(cur, user_id)
            _ensure_per_user_indexes(cur)

            for logical in IMPORT_ORDER:
                _sqlite, candidates, pk = (
                    TABLE_BY_LOGICAL[logical][1],
                    TABLE_BY_LOGICAL[logical][2],
                    TABLE_BY_LOGICAL[logical][3],
                )
                pg_table = _resolve_pg_table(cur, candidates)
                if not pg_table:
                    counts[logical] = 0
                    continue
                cols = set(_pg_columns(cur, pg_table))
                n = 0
                for raw in tables.get(logical) or []:
                    row = normalize_row(raw)
                    if "user_id" in cols and logical != "Users":
                        row["user_id"] = user_id
                    keys = [k for k in row.keys() if k in cols]
                    if not keys:
                        continue
                    col_sql = ", ".join(f'"{k}"' for k in keys)
                    placeholders = ", ".join("%s" for _ in keys)
                    updates = ", ".join(
                        f'"{k}" = EXCLUDED."{k}"' for k in keys if k != pk
                    )
                    sql = (
                        f'INSERT INTO "{pg_table}" ({col_sql}) VALUES ({placeholders}) '
                        f"ON CONFLICT ({pk}) DO UPDATE SET {updates}"
                        if updates
                        else f'INSERT INTO "{pg_table}" ({col_sql}) VALUES ({placeholders}) '
                        f"ON CONFLICT ({pk}) DO NOTHING"
                    )
                    cur.execute(sql, [row[k] for k in keys])
                    n += 1
                counts[logical] = n

                try:
                    cur.execute("SAVEPOINT sync_seq")
                    cur.execute(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence(%s, %s),
                            COALESCE((SELECT MAX({pk}) FROM "{pg_table}"), 1),
                            true
                        )
                        """,
                        (pg_table, pk),
                    )
                    cur.execute("RELEASE SAVEPOINT sync_seq")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sync_seq")

            conn.commit()
    return counts


def run_sync(
    *,
    database_url: str,
    db_path: Path,
    username: str,
    password: str,
    mode: str = "sync",
    prefer: str = "local",
) -> dict:
    mode = (mode or "sync").lower()
    if mode not in {"pull", "push", "sync"}:
        raise SystemExit("mode must be pull, push, or sync")

    user = authenticate_pg_user(database_url, username, password)
    user_id = int(user["user_id"])

    remote = fetch_remote_tables(database_url, user_id)
    local = fetch_local_tables(db_path, user_id)

    if not local.get("Users"):
        local["Users"] = [
            {
                "user_id": user_id,
                "username": user["username"],
                "password": user["password"],
                "email": user.get("email"),
                "phone": user.get("phone"),
            }
        ]
    if not remote.get("Users"):
        remote["Users"] = list(local["Users"])

    if mode == "pull":
        merged = remote
        stats: dict[str, Any] = {"mode": "pull"}
    elif mode == "push":
        merged = local
        stats = {"mode": "push"}
    else:
        merged, merge_stats = merge_tables(local, remote, prefer=prefer)
        stats = {"mode": "sync", "prefer": prefer, **merge_stats}

    local_counts = {}
    remote_counts = {}
    if mode in {"pull", "sync"}:
        local_counts = write_local_tables(db_path, user_id, merged)
    if mode in {"push", "sync"}:
        remote_counts = write_remote_tables(database_url, user_id, merged)
    if mode == "push":
        # Keep local identical to what was pushed.
        local_counts = write_local_tables(db_path, user_id, merged)

    return {
        "ok": True,
        "user_id": user_id,
        "username": username,
        "mode": mode,
        "prefer": prefer,
        "stats": stats,
        "local_counts": local_counts,
        "remote_counts": remote_counts,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
