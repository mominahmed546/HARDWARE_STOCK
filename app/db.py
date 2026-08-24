import os
import re
import threading
from functools import lru_cache

from psycopg_pool import ConnectionPool
from flask import g

# =====================================================
# CONNECTION
# =====================================================

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
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        self._cursor.execute(_translate_sql(query), params or ())
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return AttrRow(row, [column.name for column in self._cursor.description])

    def fetchall(self):
        rows = self._cursor.fetchall()
        columns = [column.name for column in self._cursor.description]
        return [AttrRow(row, columns) for row in rows]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def close(self):
        self._cursor.close()


class ConnectionWrapper:
    def __init__(self, connection, pool=None):
        self._connection = connection
        self._pool = pool

    def cursor(self):
        return CursorWrapper(self._connection.cursor())

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
def _translate_sql_cached(query):
    query = _replace_identifiers(query)
    query = query.replace("?", "%s")
    query = re.sub(r"\bISNULL\s*\(", "COALESCE(", query, flags=re.IGNORECASE)
    query = re.sub(r"\bLTRIM\s*\(\s*RTRIM\s*\(([^()]+)\)\s*\)", _replace_ltrim_rtrim, query, flags=re.IGNORECASE)
    query = re.sub(r"\bYEAR\s*\(([^()]+)\)", r"EXTRACT(YEAR FROM \1)::int", query, flags=re.IGNORECASE)
    query = re.sub(r"\bMONTH\s*\(([^()]+)\)", r"EXTRACT(MONTH FROM \1)::int", query, flags=re.IGNORECASE)
    query = re.sub(
        r"CONVERT\s*\(\s*VARCHAR\s*\(\s*10\s*\)\s*,\s*([^,]+)\s*,\s*103\s*\)",
        r"TO_CHAR(\1, 'DD/MM/YYYY')",
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
    return query


def _translate_sql(query):
    return _translate_sql_cached(query)


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


_pool = None
_pool_lock = threading.Lock()


def _normalize_database_url(database_url):
    """Ensure managed Postgres URLs (Supabase/Render) include SSL when needed."""
    url = (database_url or "").strip()
    if not url:
        return url

    # Render/Heroku sometimes provide postgres://; psycopg wants postgresql://.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    lower = url.lower()
    needs_ssl = (
        "supabase.com" in lower
        or "render.com" in lower
        or "amazonaws.com" in lower
        or os.environ.get("DB_SSLMODE")
    )
    if needs_ssl and "sslmode=" not in lower:
        sep = "&" if "?" in url else "?"
        sslmode = os.environ.get("DB_SSLMODE", "require")
        url = f"{url}{sep}sslmode={sslmode}"
    return url


def _create_pool(database_url):
    max_size = int(os.environ.get("DB_POOL_MAX_SIZE", "3"))
    timeout = float(os.environ.get("DB_POOL_TIMEOUT", "20"))
    connect_timeout = int(os.environ.get("DB_CONNECT_TIMEOUT", "15"))

    return ConnectionPool(
        conninfo=_normalize_database_url(database_url),
        kwargs={"connect_timeout": connect_timeout},
        min_size=1,
        max_size=max(1, max_size),
        # Warm connections can go stale on managed Postgres (idle kill /
        # pooler recycle). Validate before handing one to a request.
        check=ConnectionPool.check_connection,
        timeout=timeout,
        max_idle=180,
        reconnect_timeout=timeout,
    )


def _reset_pool():
    """Close a broken pool so the next request builds a fresh one."""
    global _pool
    with _pool_lock:
        old = _pool
        _pool = None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def _get_pool(app):
    """Lazily create one small connection pool per process.

    Opening a brand new physical connection (TCP + TLS + Postgres auth
    handshake) on every single request - as this used to do - adds real,
    fixed latency before any query even runs, especially against a managed
    Postgres host. A pool keeps a handful of connections warm and hands
    them out per request instead, so most requests reuse an already-open
    connection. The lock only guards the one-time creation (gunicorn
    threaded workers could otherwise race and create two pools on startup).
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                database_url = app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
                if not database_url:
                    raise RuntimeError("DATABASE_URL is not configured.")
                _pool = _create_pool(database_url)
    return _pool


def _pool_timeout_message(exc):
    return (
        "Database is busy or unreachable (connection timed out). "
        "If you use Supabase, open the project dashboard and make sure it is "
        "not paused, then wait ~30 seconds and try again. "
        f"Details: {exc}"
    )


def get_db_connection(app):
    if "db" in g:
        return g.db

    from psycopg_pool import PoolTimeout

    pool = _get_pool(app)
    try:
        conn = pool.getconn()
    except PoolTimeout as exc:
        # First failure is often a cold/paused DB or a pool full of dead
        # sockets. Drop the pool and retry once with a fresh connection.
        _reset_pool()
        pool = _get_pool(app)
        try:
            conn = pool.getconn()
        except PoolTimeout as retry_exc:
            raise RuntimeError(_pool_timeout_message(retry_exc)) from retry_exc
        except Exception as retry_exc:
            raise RuntimeError(_pool_timeout_message(retry_exc)) from retry_exc
    except Exception as exc:
        # Misconfigured URL / SSL / network — surface a clear login error.
        msg = str(exc).lower()
        if "timeout" in msg or "could not connect" in msg or "connection" in msg:
            raise RuntimeError(_pool_timeout_message(exc)) from exc
        raise

    g.db = ConnectionWrapper(conn, pool=pool)
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