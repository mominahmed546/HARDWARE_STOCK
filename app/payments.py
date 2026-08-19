"""Invoice payment records, status, and paid-ratio helpers.

Profit, sales, and purchase cost follow cash received on each invoice:
unpaid = 0%, partial = amount_paid / invoice_total, paid = 100%.
"""

from datetime import datetime

_SCHEMA_READY = {
    "invoice_payments": False,
    "cash_accounts_schema": False,
    "purchase_payments": False,
}


def ensure_invoice_payments_table(db, cursor):
    if _SCHEMA_READY["invoice_payments"]:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS InvoicePayments (
            PaymentID SERIAL PRIMARY KEY,
            InvoiceID INTEGER NOT NULL REFERENCES Invoices(InvoiceID) ON DELETE CASCADE,
            Amount NUMERIC(12, 2) NOT NULL,
            PaymentDate TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            Notes VARCHAR(255)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice_id
        ON InvoicePayments (InvoiceID)
        """
    )
    cursor.execute(
        """
        ALTER TABLE InvoicePayments
        ADD COLUMN IF NOT EXISTS PaymentMethod VARCHAR(20) DEFAULT 'Cash'
        """
    )
    cursor.execute(
        """
        UPDATE InvoicePayments
        SET PaymentMethod = 'Cash'
        WHERE PaymentMethod IS NULL OR BTRIM(PaymentMethod) = ''
        """
    )
    cursor.execute(
        """
        INSERT INTO InvoicePayments (InvoiceID, Amount, PaymentDate, Notes, PaymentMethod)
        SELECT
            i.InvoiceID,
            i.TotalAmount,
            i.[Date],
            'Backfilled from Paid status',
            'Cash'
        FROM Invoices i
        WHERE COALESCE(i.PaymentStatus, 'Unpaid') = 'Paid'
          AND COALESCE(i.TotalAmount, 0) > 0
          AND NOT EXISTS (
              SELECT 1 FROM InvoicePayments p WHERE p.InvoiceID = i.InvoiceID
          )
        """
    )
    backfilled = int(cursor.rowcount or 0)
    if backfilled:
        _sync_invoices_with_payments(cursor)
    db.commit()
    _SCHEMA_READY["invoice_payments"] = True


def payments_join_sql(invoice_alias="i"):
    return f"""
        LEFT JOIN (
            SELECT InvoiceID, SUM(Amount) AS PaidAmount
            FROM InvoicePayments
            GROUP BY InvoiceID
        ) pay ON pay.InvoiceID = {invoice_alias}.InvoiceID
    """


def paid_ratio_sql(invoice_alias="i", paid_alias="pay"):
    """Share of the invoice that has been paid, capped at 100%."""
    return f"""
        CASE
            WHEN COALESCE({invoice_alias}.TotalAmount, 0) <= 0 THEN 0
            ELSE LEAST(
                1.0,
                COALESCE({paid_alias}.PaidAmount, 0) / {invoice_alias}.TotalAmount
            )
        END
    """


def payment_status(total_amount, paid_amount, epsilon=0.005):
    total_amount = float(total_amount or 0)
    paid_amount = float(paid_amount or 0)
    if paid_amount <= epsilon:
        return "Unpaid"
    if total_amount <= epsilon or paid_amount + epsilon >= total_amount:
        return "Paid"
    return "Partial"


def normalize_payment_method(value):
    method = str(value or "").strip().lower()
    if method == "bank":
        return "Bank"
    if method in {"in-kind", "inkind", "items", "item"}:
        return "In-Kind"
    return "Cash"


def ensure_cash_accounts(db, cursor):
    schema_changed = False
    if not _SCHEMA_READY["cash_accounts_schema"]:
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
            "ALTER TABLE CashAccounts ADD COLUMN IF NOT EXISTS UserID INTEGER"
        )
        schema_changed = True
        _SCHEMA_READY["cash_accounts_schema"] = True
    from app.tenancy import request_user_id

    user_id = request_user_id()
    inserted = 0
    if user_id:
        cursor.execute(
            """
            INSERT INTO CashAccounts (AccountID, UserID, CashOpening, BankOpening)
            SELECT ?, ?, 0, 0
            WHERE NOT EXISTS (SELECT 1 FROM CashAccounts WHERE UserID = ?)
            """,
            (user_id, user_id, user_id),
        )
        inserted = int(cursor.rowcount or 0)
    if schema_changed or inserted:
        db.commit()


def ensure_purchase_payments_table(db, cursor):
    if _SCHEMA_READY["purchase_payments"]:
        return
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS PurchasePayments (
            PaymentID SERIAL PRIMARY KEY,
            PurchaseID INTEGER NOT NULL REFERENCES Purchases(PurchaseID) ON DELETE CASCADE,
            Amount NUMERIC(12, 2) NOT NULL,
            PaymentDate TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            Notes VARCHAR(255),
            PaymentMethod VARCHAR(20) DEFAULT 'Cash'
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_purchase_payments_purchase_id
        ON PurchasePayments (PurchaseID)
        """
    )
    cursor.execute(
        """
        ALTER TABLE Purchases
        ADD COLUMN IF NOT EXISTS PaymentStatus VARCHAR(20) DEFAULT 'Unpaid'
        """
    )
    cursor.execute(
        """
        UPDATE Purchases
        SET PaymentStatus = 'Unpaid'
        WHERE PaymentStatus IS NULL OR BTRIM(PaymentStatus) = ''
        """
    )
    cursor.execute(
        """
        INSERT INTO PurchasePayments (PurchaseID, Amount, PaymentDate, Notes, PaymentMethod)
        SELECT
            p.PurchaseID,
            p.TotalAmount,
            p.PurchaseDate,
            'Backfilled from purchase payment mode',
            CASE WHEN COALESCE(p.PaymentMethod, 'Cash') = 'Bank' THEN 'Bank' ELSE 'Cash' END
        FROM Purchases p
        WHERE COALESCE(p.TotalAmount, 0) > 0
          AND COALESCE(p.PaymentMethod, 'Cash') IN ('Cash', 'Bank')
          AND NOT EXISTS (
              SELECT 1 FROM PurchasePayments pp WHERE pp.PurchaseID = p.PurchaseID
          )
        """
    )
    _sync_purchases_with_payments(cursor)
    db.commit()
    _SCHEMA_READY["purchase_payments"] = True


def get_cash_openings(cursor):
    from app.tenancy import owner_sql

    cursor.execute(
        f"""
        SELECT COALESCE(CashOpening, 0) AS CashOpening, COALESCE(BankOpening, 0) AS BankOpening
        FROM CashAccounts
        WHERE {owner_sql()}
        ORDER BY AccountID
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    if not row:
        return 0.0, 0.0
    return float(row.CashOpening or 0), float(row.BankOpening or 0)


def save_cash_openings(cursor, cash_opening, bank_opening):
    from app.tenancy import owner_sql

    cursor.execute(
        f"""
        UPDATE CashAccounts
        SET CashOpening = ?, BankOpening = ?
        WHERE {owner_sql()}
        """,
        (float(cash_opening or 0), float(bank_opening or 0)),
    )


def remaining_due(total_amount, paid_amount, epsilon=0.005):
    remaining = float(total_amount or 0) - float(paid_amount or 0)
    return remaining if remaining > epsilon else 0.0


def invoice_paid_total(cursor, invoice_id):
    cursor.execute(
        """
        SELECT COALESCE(SUM(Amount), 0) AS PaidAmount
        FROM InvoicePayments
        WHERE InvoiceID = ?
        """,
        (invoice_id,),
    )
    row = cursor.fetchone()
    return float(row.PaidAmount or 0) if row else 0.0


def list_invoice_payments(cursor, invoice_id):
    cursor.execute(
        """
        SELECT PaymentID, InvoiceID, Amount, PaymentDate, Notes,
               COALESCE(PaymentMethod, 'Cash') AS PaymentMethod
        FROM InvoicePayments
        WHERE InvoiceID = ?
        ORDER BY PaymentDate ASC, PaymentID ASC
        """,
        (invoice_id,),
    )
    return cursor.fetchall()


def refresh_invoice_settlement(cursor, invoice_id):
    cursor.execute(
        """
        SELECT
            COALESCE(TotalAmount, 0) AS TotalAmount,
            COALESCE(PreviousBalance, 0) AS PreviousBalance
        FROM Invoices
        WHERE InvoiceID = ?
        """,
        (invoice_id,),
    )
    invoice = cursor.fetchone()
    if not invoice:
        return None

    paid_amount = invoice_paid_total(cursor, invoice_id)
    status = payment_status(invoice.TotalAmount, paid_amount)
    cash_received = paid_amount
    net_balance = float(invoice.PreviousBalance or 0) + float(invoice.TotalAmount or 0) - cash_received
    if net_balance < 0:
        net_balance = 0.0

    cursor.execute(
        """
        UPDATE Invoices
        SET PaymentStatus = ?, CashReceived = ?, NetBalance = ?
        WHERE InvoiceID = ?
        """,
        (status, cash_received, net_balance, invoice_id),
    )
    return {
        "status": status,
        "paid_amount": paid_amount,
        "cash_received": cash_received,
        "net_balance": net_balance,
        "remaining": remaining_due(invoice.TotalAmount, paid_amount),
    }


def adjust_customer_previous_balance(cursor, customer_id, delta):
    delta = float(delta or 0)
    if abs(delta) < 0.005:
        return
    if delta < 0:
        cursor.execute(
            """
            UPDATE Customers
            SET PreviousBalance = CASE
                WHEN COALESCE(PreviousBalance, 0) + ? < 0 THEN 0
                ELSE COALESCE(PreviousBalance, 0) + ?
            END
            WHERE CustomerID = ?
            """,
            (delta, delta, customer_id),
        )
        return
    cursor.execute(
        """
        UPDATE Customers
        SET PreviousBalance = COALESCE(PreviousBalance, 0) + ?
        WHERE CustomerID = ?
        """,
        (delta, customer_id),
    )


def add_invoice_payment(cursor, invoice, amount, payment_date, notes="", payment_method="Cash"):
    invoice_id = int(invoice.InvoiceID)
    customer_id = int(invoice.CustomerID)
    total_amount = float(invoice.TotalAmount or 0)
    paid_amount = invoice_paid_total(cursor, invoice_id)
    remaining = remaining_due(total_amount, paid_amount)

    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if amount > remaining + 0.005:
        raise ValueError(f"Payment cannot exceed the remaining due of Rs {remaining:,.2f}.")

    cursor.execute(
        """
        INSERT INTO InvoicePayments (InvoiceID, Amount, PaymentDate, Notes, PaymentMethod)
        VALUES (?, ?, ?, ?, ?)
        """,
        (invoice_id, amount, payment_date, notes or None, normalize_payment_method(payment_method)),
    )
    adjust_customer_previous_balance(cursor, customer_id, -amount)
    return refresh_invoice_settlement(cursor, invoice_id)


def delete_invoice_payment(cursor, invoice, payment_id):
    invoice_id = int(invoice.InvoiceID)
    cursor.execute(
        """
        SELECT PaymentID, Amount
        FROM InvoicePayments
        WHERE PaymentID = ? AND InvoiceID = ?
        """,
        (payment_id, invoice_id),
    )
    payment = cursor.fetchone()
    if not payment:
        raise ValueError("Payment not found.")

    amount = float(payment.Amount or 0)
    cursor.execute(
        "DELETE FROM InvoicePayments WHERE PaymentID = ? AND InvoiceID = ?",
        (payment_id, invoice_id),
    )
    adjust_customer_previous_balance(cursor, int(invoice.CustomerID), amount)
    return refresh_invoice_settlement(cursor, invoice_id)


def clear_invoice_payments(cursor, invoice):
    invoice_id = int(invoice.InvoiceID)
    paid_amount = invoice_paid_total(cursor, invoice_id)
    cursor.execute("DELETE FROM InvoicePayments WHERE InvoiceID = ?", (invoice_id,))
    adjust_customer_previous_balance(cursor, int(invoice.CustomerID), paid_amount)
    return refresh_invoice_settlement(cursor, invoice_id)


def pay_invoice_remaining(cursor, invoice, payment_date=None, notes="Marked paid", payment_method="Cash"):
    remaining = remaining_due(invoice.TotalAmount, invoice_paid_total(cursor, int(invoice.InvoiceID)))
    if remaining <= 0:
        return refresh_invoice_settlement(cursor, int(invoice.InvoiceID))
    if payment_date is None:
        payment_date = datetime.now()
    return add_invoice_payment(cursor, invoice, remaining, payment_date, notes, payment_method)


def _sync_invoices_with_payments(cursor):
    cursor.execute(
        """
        UPDATE Invoices
        SET
            CashReceived = COALESCE(pay.PaidAmount, 0),
            PaymentStatus = CASE
                WHEN COALESCE(pay.PaidAmount, 0) <= 0 THEN 'Unpaid'
                WHEN COALESCE(TotalAmount, 0) <= 0 THEN 'Paid'
                WHEN COALESCE(pay.PaidAmount, 0) >= COALESCE(TotalAmount, 0) THEN 'Paid'
                ELSE 'Partial'
            END,
            NetBalance = COALESCE(PreviousBalance, 0)
                + COALESCE(TotalAmount, 0)
                - COALESCE(pay.PaidAmount, 0)
        FROM (
            SELECT InvoiceID, SUM(Amount) AS PaidAmount
            FROM InvoicePayments
            GROUP BY InvoiceID
        ) pay
        WHERE Invoices.InvoiceID = pay.InvoiceID
        """
    )


def purchase_payment_status(total_amount, paid_amount, epsilon=0.005):
    total_amount = float(total_amount or 0)
    paid_amount = float(paid_amount or 0)
    if paid_amount <= epsilon:
        return "Unpaid"
    if total_amount <= epsilon or paid_amount + epsilon >= total_amount:
        return "Paid"
    return "Partial"


def purchase_paid_total(cursor, purchase_id):
    cursor.execute(
        """
        SELECT COALESCE(SUM(Amount), 0) AS PaidAmount
        FROM PurchasePayments
        WHERE PurchaseID = ?
        """,
        (purchase_id,),
    )
    row = cursor.fetchone()
    return float(row.PaidAmount or 0) if row else 0.0


def purchase_remaining_due(total_amount, paid_amount, epsilon=0.005):
    remaining = float(total_amount or 0) - float(paid_amount or 0)
    return remaining if remaining > epsilon else 0.0


def list_purchase_payments(cursor, purchase_id):
    cursor.execute(
        """
        SELECT PaymentID, PurchaseID, Amount, PaymentDate, Notes,
               COALESCE(PaymentMethod, 'Cash') AS PaymentMethod
        FROM PurchasePayments
        WHERE PurchaseID = ?
        ORDER BY PaymentDate ASC, PaymentID ASC
        """,
        (purchase_id,),
    )
    return cursor.fetchall()


def refresh_purchase_settlement(cursor, purchase_id):
    cursor.execute(
        """
        SELECT COALESCE(TotalAmount, 0) AS TotalAmount
        FROM Purchases
        WHERE PurchaseID = ?
        """,
        (purchase_id,),
    )
    purchase = cursor.fetchone()
    if not purchase:
        return None
    paid_amount = purchase_paid_total(cursor, purchase_id)
    status = purchase_payment_status(purchase.TotalAmount, paid_amount)
    cursor.execute(
        """
        UPDATE Purchases
        SET PaymentStatus = ?
        WHERE PurchaseID = ?
        """,
        (status, purchase_id),
    )
    return {
        "status": status,
        "paid_amount": paid_amount,
        "remaining": purchase_remaining_due(purchase.TotalAmount, paid_amount),
    }


def add_purchase_payment(cursor, purchase, amount, payment_date, notes="", payment_method="Cash"):
    purchase_id = int(purchase.PurchaseID)
    total_amount = float(purchase.TotalAmount or 0)
    paid_amount = purchase_paid_total(cursor, purchase_id)
    remaining = purchase_remaining_due(total_amount, paid_amount)
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if amount > remaining + 0.005:
        raise ValueError(f"Payment cannot exceed the remaining due of Rs {remaining:,.2f}.")
    method = "Bank" if str(payment_method or "").strip().lower() == "bank" else "Cash"
    cursor.execute(
        """
        INSERT INTO PurchasePayments (PurchaseID, Amount, PaymentDate, Notes, PaymentMethod)
        VALUES (?, ?, ?, ?, ?)
        """,
        (purchase_id, amount, payment_date, notes or None, method),
    )
    return refresh_purchase_settlement(cursor, purchase_id)


def delete_purchase_payment(cursor, purchase, payment_id):
    purchase_id = int(purchase.PurchaseID)
    cursor.execute(
        """
        DELETE FROM PurchasePayments
        WHERE PaymentID = ? AND PurchaseID = ?
        """,
        (payment_id, purchase_id),
    )
    if int(cursor.rowcount or 0) <= 0:
        raise ValueError("Payment not found.")
    return refresh_purchase_settlement(cursor, purchase_id)


def clear_purchase_payments(cursor, purchase):
    purchase_id = int(purchase.PurchaseID)
    cursor.execute("DELETE FROM PurchasePayments WHERE PurchaseID = ?", (purchase_id,))
    return refresh_purchase_settlement(cursor, purchase_id)


def pay_purchase_remaining(cursor, purchase, payment_date=None, notes="Marked paid", payment_method="Cash"):
    remaining = purchase_remaining_due(
        purchase.TotalAmount,
        purchase_paid_total(cursor, int(purchase.PurchaseID)),
    )
    if remaining <= 0:
        return refresh_purchase_settlement(cursor, int(purchase.PurchaseID))
    if payment_date is None:
        payment_date = datetime.now()
    return add_purchase_payment(cursor, purchase, remaining, payment_date, notes, payment_method)


def _sync_purchases_with_payments(cursor):
    cursor.execute(
        """
        UPDATE Purchases
        SET PaymentStatus = CASE
                WHEN COALESCE(pay.PaidAmount, 0) <= 0 THEN 'Unpaid'
                WHEN COALESCE(Purchases.TotalAmount, 0) <= 0 THEN 'Paid'
                WHEN COALESCE(pay.PaidAmount, 0) >= COALESCE(Purchases.TotalAmount, 0) THEN 'Paid'
                ELSE 'Partial'
            END
        FROM (
            SELECT PurchaseID, SUM(Amount) AS PaidAmount
            FROM PurchasePayments
            GROUP BY PurchaseID
        ) pay
        WHERE Purchases.PurchaseID = pay.PurchaseID
        """
    )
    cursor.execute(
        """
        UPDATE Purchases
        SET PaymentStatus = 'Unpaid'
        WHERE PurchaseID NOT IN (SELECT DISTINCT PurchaseID FROM PurchasePayments)
        """
    )
