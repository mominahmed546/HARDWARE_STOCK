"""Invoice payment records, status, and paid-ratio helpers.

Profit, sales, and purchase cost follow cash received on each invoice:
unpaid = 0%, partial = amount_paid / invoice_total, paid = 100%.
"""

from datetime import datetime


def ensure_invoice_payments_table(db, cursor):
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
        INSERT INTO InvoicePayments (InvoiceID, Amount, PaymentDate, Notes)
        SELECT
            i.InvoiceID,
            i.TotalAmount,
            i.[Date],
            'Backfilled from Paid status'
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
        SELECT PaymentID, InvoiceID, Amount, PaymentDate, Notes
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


def add_invoice_payment(cursor, invoice, amount, payment_date, notes=""):
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
        INSERT INTO InvoicePayments (InvoiceID, Amount, PaymentDate, Notes)
        VALUES (?, ?, ?, ?)
        """,
        (invoice_id, amount, payment_date, notes or None),
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


def pay_invoice_remaining(cursor, invoice, payment_date=None, notes="Marked paid"):
    remaining = remaining_due(invoice.TotalAmount, invoice_paid_total(cursor, int(invoice.InvoiceID)))
    if remaining <= 0:
        return refresh_invoice_settlement(cursor, int(invoice.InvoiceID))
    if payment_date is None:
        payment_date = datetime.now()
    return add_invoice_payment(cursor, invoice, remaining, payment_date, notes)


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
