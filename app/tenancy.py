"""Keep each logged-in account's customers, stock, invoices, and cash private."""

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

CHILD_POLICIES = (
    ("InvoiceDetails", "Invoices", "InvoiceID"),
    ("InvoicePayments", "Invoices", "InvoiceID"),
    ("PurchaseDetails", "Purchases", "PurchaseID"),
    ("QuotationDetails", "Quotations", "QuotationID"),
)

_SETTING = "NULLIF(current_setting('app.user_id', true), '')::int"
_ready = False


def next_table_id(cursor, table, column):
    """Next primary key across all accounts, avoiding ID collisions under RLS."""
    cursor.execute("SELECT set_config('app.migrate', '1', true)")
    try:
        cursor.execute(
            f"SELECT COALESCE(MAX({column}), 0) + 1 AS NextID FROM {table}"
        )
        return int(cursor.fetchone()[0])
    finally:
        cursor.execute("SELECT set_config('app.migrate', '0', true)")


def request_user_id():
    try:
        if current_user.is_authenticated:
            return int(current_user.id)
    except Exception:
        pass
    return 0


def bind_current_account(db):
    """Apply isolation schema once, then pin this connection to the current account."""
    global _ready
    cursor = db.cursor()
    try:
        if not _ready:
            cursor.execute("SELECT set_config('app.migrate', '1', true)")
            _ensure_isolation(cursor)
            _ready = True
        cursor.execute(
            "SELECT set_config('app.user_id', ?, false)",
            (str(request_user_id()),),
        )
        db.commit()
    except Exception:
        db.rollback()
        _ready = False
        raise
    finally:
        cursor.close()


def _ensure_isolation(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS CashAccounts (
            AccountID INTEGER PRIMARY KEY,
            CashOpening NUMERIC(12, 2) DEFAULT 0,
            BankOpening NUMERIC(12, 2) DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS InvoicePayments (
            PaymentID SERIAL PRIMARY KEY,
            InvoiceID INTEGER NOT NULL REFERENCES Invoices(InvoiceID) ON DELETE CASCADE,
            Amount NUMERIC(12, 2) NOT NULL,
            PaymentDate TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            Notes VARCHAR(255),
            PaymentMethod VARCHAR(20) DEFAULT 'Cash'
        )
        """
    )
    for table in OWNER_TABLES:
        cursor.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS UserID INTEGER"
        )
        cursor.execute(
            f"""
            ALTER TABLE {table}
            ALTER COLUMN UserID SET DEFAULT {_SETTING}
            """
        )

    cursor.execute(
        """
        SELECT UserID FROM Users
        ORDER BY UserID
        LIMIT 1
        """
    )
    owner = cursor.fetchone()
    owner_id = int(owner[0]) if owner else None

    if owner_id:
        for table in OWNER_TABLES:
            cursor.execute(
                f"UPDATE {table} SET UserID = ? WHERE UserID IS NULL",
                (owner_id,),
            )
        _backfill_stock_history(cursor)

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_accounts_user
        ON CashAccounts (UserID)
        """
    )

    _fix_quotation_numbers(cursor)
    _ensure_cash_account_row(cursor, request_user_id() or owner_id)

    for table in OWNER_TABLES:
        _apply_owner_policy(cursor, table)

    for child, parent, key in CHILD_POLICIES:
        _apply_child_policy(cursor, child, parent, key)


def _backfill_stock_history(cursor):
    cursor.execute(
        """
        UPDATE StockHistory
        SET UserID = Purchases.UserID
        FROM Purchases
        WHERE StockHistory.PurchaseID = Purchases.PurchaseID
          AND StockHistory.UserID IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE StockHistory
        SET UserID = Invoices.UserID
        FROM Invoices
        WHERE StockHistory.InvoiceID = Invoices.InvoiceID
          AND StockHistory.UserID IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE StockHistory
        SET UserID = Item.UserID
        FROM Item
        WHERE StockHistory.ItemID = Item.ItemID
          AND StockHistory.UserID IS NULL
        """
    )


def _fix_quotation_numbers(cursor):
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
        cursor.execute(f'ALTER TABLE Quotations DROP CONSTRAINT IF EXISTS "{name}"')
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_quotations_user_no
        ON Quotations (UserID, QuotationNo)
        """
    )


def _ensure_cash_account_row(cursor, user_id):
    if not user_id:
        return
    cursor.execute(
        """
        INSERT INTO CashAccounts (AccountID, UserID, CashOpening, BankOpening)
        SELECT ?, ?, 0, 0
        WHERE NOT EXISTS (SELECT 1 FROM CashAccounts WHERE UserID = ?)
        """,
        (user_id, user_id, user_id),
    )


def _apply_owner_policy(cursor, table):
    policy = f"{table.lower()}_owner"
    cursor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    cursor.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    cursor.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    cursor.execute(
        f"""
        CREATE POLICY {policy} ON {table}
        USING (
            current_setting('app.migrate', true) = '1'
            OR UserID = {_SETTING}
        )
        WITH CHECK (
            current_setting('app.migrate', true) = '1'
            OR UserID = {_SETTING}
        )
        """
    )


def _apply_child_policy(cursor, child, parent, key):
    policy = f"{child.lower()}_owner"
    cursor.execute(f"ALTER TABLE {child} ENABLE ROW LEVEL SECURITY")
    cursor.execute(f"ALTER TABLE {child} FORCE ROW LEVEL SECURITY")
    cursor.execute(f"DROP POLICY IF EXISTS {policy} ON {child}")
    cursor.execute(
        f"""
        CREATE POLICY {policy} ON {child}
        USING (
            current_setting('app.migrate', true) = '1'
            OR {key} IN (
                SELECT {key} FROM {parent}
                WHERE UserID = {_SETTING}
            )
        )
        WITH CHECK (
            current_setting('app.migrate', true) = '1'
            OR {key} IN (
                SELECT {key} FROM {parent}
                WHERE UserID = {_SETTING}
            )
        )
        """
    )
