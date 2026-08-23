import os
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from flask import g

# =====================================================
# CONNECTION
# =====================================================

_bound_user = threading.local()
_sqlite_schema_ready = False
_sqlite_schema_lock = threading.Lock()


def set_bound_user_id(user_id):
    """Pin the current request's account id (used by SQLite owner_sql)."""
    _bound_user.value = int(user_id or 0)


def bound_user_id():
    return int(getattr(_bound_user, "value", 0) or 0)


def _database_url(app=None):
    if app is not None:
        url = app.config.get("DATABASE_URL")
        if url:
            return url
    return os.environ.get("DATABASE_URL") or ""


def using_sqlite(app=None):
    url = (_database_url(app) or "").strip().lower()
    if url.startswith("sqlite:"):
        return True
    flag = (os.environ.get("DESKTOP_MODE") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if app is not None and app.config.get("DESKTOP_MODE"):
        return True
    return False


def _sqlite_path_from_url(url):
    """Parse sqlite:///path or sqlite:////abs/path into a filesystem path."""
    raw = (url or "").strip()
    if not raw:
        raise RuntimeError("SQLite DATABASE_URL is empty.")
    if raw.startswith("sqlite:////"):
        return unquote(raw[len("sqlite:///"):])
    if raw.startswith("sqlite:///"):
        rest = unquote(raw[len("sqlite:///"):])
        if len(rest) >= 2 and rest[1] == ":":
            # Windows drive letter: C:/Users/...
            return rest
        return rest
    parsed = urlparse(raw)
    if parsed.scheme != "sqlite":
        raise RuntimeError(f"Not a sqlite URL: {raw}")
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {".", "localhost"}:
        path = f"/{parsed.netloc}{path}"
    return path or ":memory:"


_COLUMN_REPLACEMENTS = {
    "Users": "users",
    "Customers": "customers",
    "Supplier": "supplier",
    "Category": "category",
    "Item": "item",
    "Purchases": "purchases",
    "PurchaseDetails": "purchase_details",
    "Invoices": "invoices",
    "InvoiceDetails": "invoice_details",
    "UserID": "user_id",
    "UserName": "username",
    "Username": "username",
    "Email": "email",
    "Phone": "phone",
    "CustomerID": "customer_id",
    "CustomerName": "customer_name",
    "ContactNo": "contact_no",
    "SupplierID": "supplier_id",
    "SupplierName": "supplier_name",
    "CategoryID": "category_id",
    "CategoryName": "category_name",
    "ItemID": "item_id",
    "ItemNo": "item_no",
    "ItemName": "item_name",
    "Brand": "brand",
    "PurchaseID": "purchase_id",
    "PurchaseDate": "purchase_date",
    "PurchaseRate": "purchase_rate",
    "SaleRate": "sale_rate",
    "InvoiceID": "invoice_id",
    "PaymentStatus": "payment_status",
    "TotalAmount": "total_amount",
    "PreviousBalance": "previous_balance",
    "CashReceived": "cash_received",
    "NetBalance": "net_balance",
    "Quotations": "quotations",
    "QuotationDetails": "quotation_details",
    "QuotationID": "quotation_id",
    "QuotationNo": "quotation_no",
    "QuotationDate": "quotation_date",
    "Address": "address",
    "Project": "project",
    "WorkType": "work_type",
    "Engineer": "engineer",
    "Advance": "advance",
    "Description": "description",
    "Width": "width",
    "Height": "height",
    "SqFt": "sqft",
    "InvoicePayments": "invoice_payments",
    "PaymentID": "payment_id",
    "PaymentDate": "payment_date",
    "PaidAmount": "paid_amount",
    "Amount": "amount",
    "Notes": "notes",
    "PaymentMethod": "payment_method",
    "LedgerEntries": "ledger_entries",
    "EntryID": "entry_id",
    "EntryDate": "entry_date",
    "EntryType": "entry_type",
    "VchType": "vch_type",
    "ManualDebit": "manual_debit",
    "ManualCredit": "manual_credit",
    "ManualDebitTotal": "manual_debit_total",
    "ManualCreditTotal": "manual_credit_total",
    "CashAccounts": "cash_accounts",
    "AccountID": "account_id",
    "CashOpening": "cash_opening",
    "BankOpening": "bank_opening",
    "CashAmount": "cash_amount",
    "BankAmount": "bank_amount",
    "SaleDate": "sale_date",
    "DetailID": "detail_id",
    "StockHistory": "stock_history",
    "HistoryID": "history_id",
    "Qty": "qty",
    "Rate": "rate",
    "Particulars": "particulars",
    "Password": "password",
    "LineTotal": "line_total",
    "CurrentQty": "current_qty",
    "FirstPurchaseDate": "first_purchase_date",
    "LastPurchaseDate": "last_purchase_date",
    "PurchaseCount": "purchase_count",
    "ItemLineCount": "item_line_count",
    "TotalQty": "total_qty",
    "InvoiceCount": "invoice_count",
    "PurchaseCount": "purchase_count",
    "SalesYear": "sales_year",
    "SalesMonth": "sales_month",
    "TotalSales": "total_sales",
    "ActionType": "action_type",
    "CreatedAt": "created_at",
}


class AttrRow(tuple):
    def __new__(cls, values, columns):
        row = super().__new__(cls, values)
        row._columns = columns
        row._index = {_normalize_key(column): index for index, column in enumerate(columns)}
        return row

    def __getattr__(self, name):
        key = _normalize_key(name)
        if key in self._index:
            return self[self._index[key]]
        raise AttributeError(name)

    def __getitem__(self, key):
        if isinstance(key, str):
            return getattr(self, key)
        return super().__getitem__(key)


class CursorWrapper:
    def __init__(self, cursor, sqlite=False):
        self._cursor = cursor
        self._sqlite = sqlite

    def execute(self, query, params=None):
        translated = _translate_sql(query, sqlite=self._sqlite)
        try:
            self._cursor.execute(translated, params or ())
        except Exception as exc:
            if self._sqlite and _ignore_sqlite_schema_error(translated, exc):
                return self
            raise
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        columns = _cursor_column_names(self._cursor)
        return AttrRow(row, columns)

    def fetchall(self):
        rows = self._cursor.fetchall()
        columns = _cursor_column_names(self._cursor)
        return [AttrRow(row, columns) for row in rows]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()


def _cursor_column_names(cursor):
    if not cursor.description:
        return []
    names = []
    for column in cursor.description:
        # psycopg: column.name; sqlite3: column[0]
        name = getattr(column, "name", None)
        if name is None:
            name = column[0]
        names.append(name)
    return names


def _ignore_sqlite_schema_error(sql, exc):
    """Self-healing ALTER/CREATE that already applied should not fail pages."""
    msg = str(exc).lower()
    if "duplicate column" in msg:
        return True
    if "already exists" in msg:
        return True
    return False


class ConnectionWrapper:
    def __init__(self, connection, pool=None, sqlite=False):
        self._connection = connection
        self._pool = pool
        self._sqlite = sqlite

    def cursor(self):
        return CursorWrapper(self._connection.cursor(), sqlite=self._sqlite)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        if self._pool is None:
            self._connection.close()
            return

        # Pooled connections are handed to a *different*, unrelated request
        # (possibly a different logged-in account) the next time they're
        # checked out. Any transaction left open here must not carry over,
        # so always roll back before returning it - routes that already
        # commit/rollback on their own paths make this a no-op; it only
        # does real work for the read-only paths (execute_query etc.) and
        # any error path that forgot to roll back explicitly.
        try:
            self._connection.rollback()
        except Exception:
            pass
        try:
            self._pool.putconn(self._connection)
        except Exception:
            try:
                self._connection.close()
            except Exception:
                pass


def _normalize_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _replace_ltrim_rtrim(match):
    return f"BTRIM({match.group(1)})"


def _translate_top(match):
    limit = match.group(1)
    rest = match.group(2).rstrip()
    return f"SELECT {rest} LIMIT {limit}"


def _translate_insert_returning(match):
    table = match.group("table")
    columns = match.group("columns")
    returning = _replace_identifiers(match.group("returning"))
    values = match.group("values")
    return f"INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING {returning}"


# Sorted once at import: longest identifiers first so "InvoiceDetails"
# replaces before "Invoice" / "Details" fragments never fight each other.
_COLUMN_REPLACEMENTS_SORTED = sorted(
    _COLUMN_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True
)


def _replace_identifiers(query):
    query = query.replace("[Date]", "date")

    for old, new in _COLUMN_REPLACEMENTS_SORTED:
        query = re.sub(rf"\b{old}\b", new, query, flags=re.IGNORECASE)

    return query


@lru_cache(maxsize=512)
def _translate_sql_cached(query, sqlite=False):
    query = _replace_identifiers(query)
    query = re.sub(r"\bISNULL\s*\(", "COALESCE(", query, flags=re.IGNORECASE)
    query = re.sub(
        r"\bLTRIM\s*\(\s*RTRIM\s*\(([^()]+)\)\s*\)",
        _replace_ltrim_rtrim if not sqlite else (lambda m: f"TRIM({m.group(1)})"),
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"INSERT\s+INTO\s+(?P<table>\w+)\s*\((?P<columns>.*?)\)\s*OUTPUT\s+INSERTED\.(?P<returning>\w+)\s*VALUES\s*\((?P<values>.*?)\)",
        _translate_insert_returning,
        query,
        flags=re.IGNORECASE | re.DOTALL,
    )
    query = re.sub(r"SELECT\s+TOP\s+(\d+)\s+(.*)", _translate_top, query, flags=re.IGNORECASE | re.DOTALL)

    if sqlite:
        # Keep "?" placeholders for sqlite3.
        query = re.sub(
            r"\bADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\b",
            "ADD COLUMN",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"\bYEAR\s*\(([^()]+)\)",
            r"CAST(strftime('%Y', \1) AS INTEGER)",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"\bMONTH\s*\(([^()]+)\)",
            r"CAST(strftime('%m', \1) AS INTEGER)",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"CONVERT\s*\(\s*VARCHAR\s*\(\s*10\s*\)\s*,\s*([^,]+)\s*,\s*103\s*\)",
            r"strftime('%d/%m/%Y', \1)",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(r"\bILIKE\b", "LIKE", query, flags=re.IGNORECASE)
        query = re.sub(r"\bBTRIM\s*\(", "TRIM(", query, flags=re.IGNORECASE)
        # Some SQLite builds omit GREATEST/LEAST; MAX/MIN are the multi-arg equivalents.
        query = re.sub(r"\bGREATEST\s*\(", "MAX(", query, flags=re.IGNORECASE)
        query = re.sub(r"\bLEAST\s*\(", "MIN(", query, flags=re.IGNORECASE)
        query = re.sub(
            r"\bTIMESTAMP\s+WITHOUT\s+TIME\s+ZONE\b",
            "TIMESTAMP",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(
            r"\bSERIAL\s+PRIMARY\s+KEY\b",
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            query,
            flags=re.IGNORECASE,
        )
        query = re.sub(r"\bSERIAL\b", "INTEGER", query, flags=re.IGNORECASE)
        # Strip Postgres casts like COUNT(*)::int
        query = re.sub(r"::[a-zA-Z_]\w*", "", query)
        return query

    query = query.replace("?", "%s")
    query = re.sub(r"\bYEAR\s*\(([^()]+)\)", r"EXTRACT(YEAR FROM \1)::int", query, flags=re.IGNORECASE)
    query = re.sub(r"\bMONTH\s*\(([^()]+)\)", r"EXTRACT(MONTH FROM \1)::int", query, flags=re.IGNORECASE)
    query = re.sub(
        r"CONVERT\s*\(\s*VARCHAR\s*\(\s*10\s*\)\s*,\s*([^,]+)\s*,\s*103\s*\)",
        r"TO_CHAR(\1, 'DD/MM/YYYY')",
        query,
        flags=re.IGNORECASE,
    )
    return query


def _translate_sql(query, sqlite=False):
    return _translate_sql_cached(query, bool(sqlite))


_user_columns_ready = False


def _ensure_user_contact_columns(db):
    """Add the Email/Phone columns used by password reset if they're missing.

    Mirrors the self-healing migration pattern in app/tenancy.py so a fresh
    Render Postgres database (or one deployed before this feature existed)
    picks up the new columns automatically, without a manual migration step.
    """
    global _user_columns_ready
    if _user_columns_ready:
        return

    try:
        cursor = db.cursor()
        try:
            cursor.execute("ALTER TABLE Users ADD COLUMN IF NOT EXISTS Email VARCHAR(120)")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Users ADD COLUMN IF NOT EXISTS Phone VARCHAR(20)")
        except Exception:
            pass
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON Users (Email)")
        except Exception:
            pass
        db.commit()
        cursor.close()
        _user_columns_ready = True
    except Exception:
        db.rollback()


def _schema_sqlite_path():
    try:
        from desktop.paths import bundle_dir

        candidate = bundle_dir() / "schema_sqlite.sql"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "schema_sqlite.sql"


def _ensure_sqlite_schema(connection):
    """Create core tables on first desktop launch."""
    global _sqlite_schema_ready
    if _sqlite_schema_ready:
        return
    with _sqlite_schema_lock:
        if _sqlite_schema_ready:
            return
        cur = connection.cursor()
        try:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            )
            if cur.fetchone():
                _sqlite_schema_ready = True
                return
            schema_file = _schema_sqlite_path()
            if not schema_file.exists():
                raise RuntimeError(f"Missing SQLite schema file: {schema_file}")
            connection.executescript(schema_file.read_text(encoding="utf-8"))
            connection.commit()
            _sqlite_schema_ready = True
        finally:
            cur.close()


def _connect_sqlite(app):
    database_url = _database_url(app)
    if not database_url or not str(database_url).lower().startswith("sqlite:"):
        try:
            from desktop.paths import default_sqlite_url

            database_url = default_sqlite_url()
        except Exception:
            database_url = "sqlite:///euroglass_stock.db"
        if app is not None:
            app.config["DATABASE_URL"] = database_url

    path = _sqlite_path_from_url(database_url)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
    connection.row_factory = None
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    _ensure_sqlite_schema(connection)
    return ConnectionWrapper(connection, pool=None, sqlite=True)


_pool = None
_pool_lock = threading.Lock()


def _get_pool(app):
    """Lazily create one small connection pool per process (Postgres only)."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool

                database_url = app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
                if not database_url:
                    raise RuntimeError("DATABASE_URL is not configured.")

                max_size = int(os.environ.get("DB_POOL_MAX_SIZE", 5))
                _pool = ConnectionPool(
                    conninfo=database_url,
                    kwargs={"connect_timeout": 10},
                    min_size=1,
                    max_size=max_size,
                    # Keep this short: if the database is genuinely down,
                    # requests should fail fast (like the old direct-connect
                    # code did) instead of every request hanging for a long
                    # wait before the page's own try/except can show an
                    # error. Healthy pools hand out a connection almost
                    # instantly, so this never matters in normal operation.
                    timeout=10,
                    max_idle=300,
                )
    return _pool


def get_db_connection(app):
    if "db" not in g:
        if using_sqlite(app):
            g.db = _connect_sqlite(app)
        else:
            pool = _get_pool(app)
            g.db = ConnectionWrapper(pool.getconn(), pool=pool, sqlite=False)
        _ensure_user_contact_columns(g.db)

    return g.db


def close_db_connection(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# =====================================================
# QUERY HELPERS
# =====================================================

def execute_query(app, query, params=None):
    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        cursor.execute(query, params or ())
        return cursor.fetchall()
    finally:
        cursor.close()


def execute_query_one(app, query, params=None):
    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        cursor.execute(query, params or ())
        return cursor.fetchone()
    finally:
        cursor.close()


def execute_update(app, query, params=None):
    db = get_db_connection(app)
    cursor = db.cursor()
    try:
        cursor.execute(query, params or ())
        db.commit()
        return cursor.rowcount
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()