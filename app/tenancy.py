"""Keep each logged-in account's customers, stock, invoices, and cash private.

Row-level security is not used: FORCE / CREATE POLICY requires table-owner
rights that managed Postgres (including Render) often does not grant, and a
failure here used to 500 every page. Isolation is applied in queries via
owner_sql() plus UserID on inserts.
"""

from flask_login import current_user

OWNER_TABLES = (
    "Customers",
    "Supplier",
    "Category",
    "Item",
    "Purchases",
    "Invoices",
    "Quotations",
    "CashAccounts",
    "StockHistory",
)

CHILD_TABLES = (
    "InvoiceDetails",
    "InvoicePayments",
    "PurchaseDetails",
    "QuotationDetails",
)

_SETTING = "NULLIF(current_setting('app.user_id', true), '')::int"
_ready = False


def owner_sql(alias=None):
    """SQL predicate that limits a query to the account bound on this connection."""
    column = f"{alias}.UserID" if alias else "UserID"
    return f"{column} = {_SETTING}"


def next_table_id(cursor, table, column):
    cursor.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 AS NextID FROM {table}")
    return int(cursor.fetchone()[0])


def next_owner_no(cursor, table, column):
    """Next 1, 2, 3... number for the current account only."""
    cursor.execute(
        f"SELECT COALESCE(MAX({column}), 0) + 1 AS NextNo FROM {table} WHERE {owner_sql()}"
    )
    return int(cursor.fetchone()[0])


def request_user_id():
    try:
        if current_user.is_authenticated:
            return int(current_user.id)
    except Exception:
        pass
    return 0


def bind_current_account(db):
    """Add owner columns if needed, then pin this connection to the current account."""
    global _ready
    cursor = db.cursor()
    try:
        if not _ready:
            try:
                _ensure_isolation(cursor)
                db.commit()
                _ready = True
            except Exception:
                db.rollback()
                _ready = False
        cursor.execute(
            "SELECT set_config('app.user_id', ?, false)",
            (str(request_user_id()),),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        cursor.close()


def _run(cursor, sql, params=None):
    cursor.execute(sql, params or ())


def _try_run(cursor, sql, params=None):
    try:
        _run(cursor, sql, params)
    except Exception:
        pass


def _ensure_isolation(cursor):
    _try_run(
        cursor,
        """
        CREATE TABLE IF NOT EXISTS CashAccounts (
            AccountID INTEGER PRIMARY KEY,
            CashOpening NUMERIC(12, 2) DEFAULT 0,
            BankOpening NUMERIC(12, 2) DEFAULT 0
        )
        """,
    )

    for table in OWNER_TABLES + CHILD_TABLES:
        _disable_row_security(cursor, table)

    for table in OWNER_TABLES:
        _try_run(cursor, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS UserID INTEGER")
        _try_run(
            cursor,
            f"ALTER TABLE {table} ALTER COLUMN UserID SET DEFAULT {_SETTING}",
        )

    owner_id = None
    try:
        cursor.execute("SELECT UserID FROM Users ORDER BY UserID LIMIT 1")
        owner = cursor.fetchone()
        owner_id = int(owner[0]) if owner else None
    except Exception:
        owner_id = None

    if owner_id:
        for table in OWNER_TABLES:
            _try_run(
                cursor,
                f"UPDATE {table} SET UserID = ? WHERE UserID IS NULL",
                (owner_id,),
            )
        _backfill_stock_history(cursor)

    _try_run(
        cursor,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_accounts_user
        ON CashAccounts (UserID)
        """,
    )
    _fix_quotation_numbers(cursor)
    _ensure_item_numbers(cursor)
    _ensure_cash_account_row(cursor, request_user_id() or owner_id)
    _ensure_performance_indexes(cursor)


def _disable_row_security(cursor, table):
    policy = f"{table.lower()}_owner"
    _try_run(cursor, f"DROP POLICY IF EXISTS {policy} ON {table}")
    _try_run(cursor, f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    _try_run(cursor, f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def _backfill_stock_history(cursor):
    _try_run(
        cursor,
        """
        UPDATE StockHistory
        SET UserID = Purchases.UserID
        FROM Purchases
        WHERE StockHistory.PurchaseID = Purchases.PurchaseID
          AND StockHistory.UserID IS NULL
        """,
    )
    _try_run(
        cursor,
        """
        UPDATE StockHistory
        SET UserID = Invoices.UserID
        FROM Invoices
        WHERE StockHistory.InvoiceID = Invoices.InvoiceID
          AND StockHistory.UserID IS NULL
        """,
    )
    _try_run(
        cursor,
        """
        UPDATE StockHistory
        SET UserID = Item.UserID
        FROM Item
        WHERE StockHistory.ItemID = Item.ItemID
          AND StockHistory.UserID IS NULL
        """,
    )


def _fix_quotation_numbers(cursor):
    try:
        cursor.execute(
            """
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'quotations'
              AND constraint_type = 'UNIQUE'
              AND constraint_name ILIKE '%quotation_no%'
            """
        )
        names = {
            "quotations_quotation_no_key",
            "quotation_quotation_no_key",
        }
        for row in cursor.fetchall() or []:
            if row and row[0]:
                names.add(str(row[0]))
        for name in names:
            _try_run(cursor, f'ALTER TABLE Quotations DROP CONSTRAINT IF EXISTS "{name}"')
        _try_run(
            cursor,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_quotations_user_no
            ON Quotations (UserID, QuotationNo)
            """,
        )
    except Exception:
        pass


def _ensure_item_numbers(cursor):
    _try_run(cursor, "ALTER TABLE Item ADD COLUMN IF NOT EXISTS ItemNo INTEGER")
    _try_run(
        cursor,
        """
        UPDATE Item
        SET ItemNo = numbered.ItemNo
        FROM (
            SELECT
                ItemID,
                ROW_NUMBER() OVER (
                    PARTITION BY COALESCE(UserID, 0)
                    ORDER BY ItemID
                ) AS ItemNo
            FROM Item
        ) numbered
        WHERE Item.ItemID = numbered.ItemID
          AND Item.ItemNo IS NULL
        """,
    )
    _try_run(
        cursor,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_item_user_no
        ON Item (UserID, ItemNo)
        """,
    )


def _ensure_performance_indexes(cursor):
    """Index every column the app filters/joins on constantly.

    owner_sql() puts "WHERE UserID = ..." on almost every query in the app,
    and list/report pages join on these same foreign keys, but the schema
    only ever had a couple of unique indexes. Without these, Postgres has to
    sequentially scan the whole table for nearly every page as data grows.
    Plain (non-CONCURRENT) CREATE INDEX is used so this can run inside the
    existing transaction like the rest of _ensure_isolation.
    """
    index_specs = (
        ("idx_item_user_id", "Item", "UserID"),
        ("idx_item_category_id", "Item", "CategoryID"),
        ("idx_purchases_user_id", "Purchases", "UserID"),
        ("idx_purchases_supplier_id", "Purchases", "SupplierID"),
        ("idx_purchase_details_purchase_id", "PurchaseDetails", "PurchaseID"),
        ("idx_purchase_details_item_id", "PurchaseDetails", "ItemID"),
        # PurchasePayments.PurchaseID already gets its index from
        # ensure_purchase_payments_table() in app/payments.py.
        ("idx_invoices_user_id", "Invoices", "UserID"),
        ("idx_invoices_customer_id", "Invoices", "CustomerID"),
        ("idx_invoice_details_invoice_id", "InvoiceDetails", "InvoiceID"),
        ("idx_invoice_details_item_id", "InvoiceDetails", "ItemID"),
        ("idx_quotations_user_id", "Quotations", "UserID"),
        ("idx_quotations_customer_id", "Quotations", "CustomerID"),
        ("idx_quotation_details_quotation_id", "QuotationDetails", "QuotationID"),
        ("idx_quotation_details_item_id", "QuotationDetails", "ItemID"),
        ("idx_customers_user_id", "Customers", "UserID"),
        ("idx_supplier_user_id", "Supplier", "UserID"),
        ("idx_category_user_id", "Category", "UserID"),
        ("idx_stock_history_user_id", "StockHistory", "UserID"),
        ("idx_stock_history_item_id", "StockHistory", "ItemID"),
        ("idx_stock_history_purchase_id", "StockHistory", "PurchaseID"),
        ("idx_stock_history_invoice_id", "StockHistory", "InvoiceID"),
    )
    for index_name, table, column in index_specs:
        _try_run(cursor, f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")


def _ensure_cash_account_row(cursor, user_id):
    if not user_id:
        return
    _try_run(
        cursor,
        """
        INSERT INTO CashAccounts (AccountID, UserID, CashOpening, BankOpening)
        SELECT ?, ?, 0, 0
        WHERE NOT EXISTS (SELECT 1 FROM CashAccounts WHERE UserID = ?)
        """,
        (user_id, user_id, user_id),
    )
