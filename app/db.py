import os
import re

import psycopg
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
    "CustomerID": "customer_id",
    "CustomerName": "customer_name",
    "ContactNo": "contact_no",
    "SupplierID": "supplier_id",
    "SupplierName": "supplier_name",
    "CategoryID": "category_id",
    "CategoryName": "category_name",
    "ItemID": "item_id",
    "ItemName": "item_name",
    "PurchaseID": "purchase_id",
    "PurchaseDate": "purchase_date",
    "PurchaseRate": "purchase_rate",
    "SaleRate": "sale_rate",
    "InvoiceID": "invoice_id",
    "PaymentStatus": "payment_status",
    "TotalAmount": "total_amount",
    "PreviousBalance": "previous_balance",
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
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return CursorWrapper(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


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


def _replace_identifiers(query):
    query = query.replace("[Date]", "date")

    for old, new in sorted(_COLUMN_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        query = re.sub(rf"\b{old}\b", new, query, flags=re.IGNORECASE)

    return query


def _translate_sql(query):
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


def get_db_connection(app):
    if 'db' not in g:
        database_url = app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured.")

        g.db = ConnectionWrapper(psycopg.connect(database_url))

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