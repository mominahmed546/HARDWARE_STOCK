"""Performance helpers: indexes, pagination, cash aggregates, request timing."""

import math
import time

from flask import g, request

from app.tenancy import owner_sql

_INDEXES_READY = False
_CASH_ACCOUNT_EXISTS = {}

PERFORMANCE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_invoices_user_id ON invoices (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_user_invoice_id ON invoices (user_id, invoice_id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_customer_id ON invoices (customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_details_invoice_id ON invoice_details (invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_details_item_id ON invoice_details (item_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_payments_payment_date ON invoice_payments (payment_date)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice_date ON invoice_payments (invoice_id, payment_date)",
    "CREATE INDEX IF NOT EXISTS idx_item_user_id ON item (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_item_user_item_no ON item (user_id, item_no)",
    "CREATE INDEX IF NOT EXISTS idx_item_category_id ON item (category_id)",
    "CREATE INDEX IF NOT EXISTS idx_customers_user_id ON customers (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_customers_user_name ON customers (user_id, customer_name)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_user_id ON purchases (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_supplier_id ON purchases (supplier_id)",
    "CREATE INDEX IF NOT EXISTS idx_purchases_user_date ON purchases (user_id, purchase_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_details_purchase_id ON purchase_details (purchase_id)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_details_item_id ON purchase_details (item_id)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_payments_purchase_id ON purchase_payments (purchase_id)",
    "CREATE INDEX IF NOT EXISTS idx_purchase_payments_payment_date ON purchase_payments (payment_date)",
    "CREATE INDEX IF NOT EXISTS idx_supplier_user_id ON supplier (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_category_user_id ON category (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_quotations_user_id ON quotations (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_stock_history_user_id ON stock_history (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_stock_history_item_id ON stock_history (item_id)",
)


def ensure_performance_indexes(db, cursor):
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    for statement in PERFORMANCE_INDEXES:
        try:
            cursor.execute(statement)
        except Exception:
            pass
    db.commit()
    _INDEXES_READY = True


def paginate_request(default_per_page=50, max_per_page=200):
    page = request.args.get("page", default=1, type=int) or 1
    per_page = request.args.get("per_page", default=default_per_page, type=int) or default_per_page
    page = max(1, page)
    per_page = max(1, min(per_page, max_per_page))
    offset = (page - 1) * per_page
    return page, per_page, offset


def pagination_meta(page, per_page, total_count):
    total_pages = max(1, math.ceil(total_count / per_page)) if total_count else 1
    page = min(page, total_pages)
    return {
        "page": page,
        "per_page": per_page,
        "total": total_count,
        "pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
    }


def count_query(cursor, count_sql, params=()):
    cursor.execute(count_sql, params)
    row = cursor.fetchone()
    return int(row[0] if row else 0)


def cash_account_exists(cursor, user_id):
    if not user_id:
        return False
    cached = _CASH_ACCOUNT_EXISTS.get(user_id)
    if cached is not None:
        return cached
    cursor.execute(
        f"SELECT 1 FROM CashAccounts WHERE {owner_sql()} LIMIT 1",
    )
    exists = cursor.fetchone() is not None
    _CASH_ACCOUNT_EXISTS[user_id] = exists
    return exists


def mark_cash_account_exists(user_id, exists=True):
    if user_id:
        _CASH_ACCOUNT_EXISTS[user_id] = exists


def fetch_invoice_payment_totals(cursor, through_date_sql="CURRENT_DATE", today_only=False):
    date_filter = "CAST(p.PaymentDate AS DATE) = CURRENT_DATE" if today_only else f"CAST(p.PaymentDate AS DATE) <= {through_date_sql}"
    cursor.execute(
        f"""
        SELECT
            ISNULL(SUM(CASE WHEN COALESCE(p.PaymentMethod, 'Cash') = 'Bank' THEN p.Amount ELSE 0 END), 0) AS BankAmount,
            ISNULL(SUM(CASE WHEN COALESCE(p.PaymentMethod, 'Cash') = 'Cash' THEN p.Amount ELSE 0 END), 0) AS CashAmount
        FROM InvoicePayments p
        JOIN Invoices i ON i.InvoiceID = p.InvoiceID
        WHERE {date_filter}
          AND {owner_sql("i")}
        """,
    )
    row = cursor.fetchone()
    if not row:
        return 0.0, 0.0
    return float(row.CashAmount or 0), float(row.BankAmount or 0)


def fetch_purchase_payment_totals(cursor, through_date_sql="CURRENT_DATE", today_only=False):
    date_filter = (
        "CAST(pp.PaymentDate AS DATE) = CURRENT_DATE"
        if today_only
        else f"CAST(pp.PaymentDate AS DATE) <= {through_date_sql}"
    )
    cursor.execute(
        f"""
        SELECT
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Cash' THEN pp.Amount ELSE 0 END), 0) AS CashPaid,
            ISNULL(SUM(CASE WHEN COALESCE(pp.PaymentMethod, 'Cash') = 'Bank' THEN pp.Amount ELSE 0 END), 0) AS BankPaid
        FROM PurchasePayments pp
        JOIN Purchases p ON p.PurchaseID = pp.PurchaseID
        WHERE {date_filter}
          AND {owner_sql("p")}
        """,
    )
    row = cursor.fetchone()
    if not row:
        return 0.0, 0.0
    return float(row.CashPaid or 0), float(row.BankPaid or 0)


def fetch_drawer_balances(cursor, cash_opening, bank_opening, through_date_sql="CURRENT_DATE"):
    cash_received, bank_received = fetch_invoice_payment_totals(cursor, through_date_sql, today_only=False)
    cash_paid, bank_paid = fetch_purchase_payment_totals(cursor, through_date_sql, today_only=False)
    return {
        "cash_in_hand": cash_opening + cash_received - cash_paid,
        "bank_balance": bank_opening + bank_received - bank_paid,
        "cash_received": cash_received,
        "bank_received": bank_received,
        "cash_paid": cash_paid,
        "bank_paid": bank_paid,
    }


def register_request_timing(app, slow_threshold_seconds=0.75):
    @app.before_request
    def _perf_start_timer():
        endpoint = request.endpoint or ""
        if endpoint.startswith("static"):
            return
        g.request_start = time.perf_counter()

    @app.after_request
    def _perf_log_slow_requests(response):
        start = getattr(g, "request_start", None)
        if start is None:
            return response
        elapsed = time.perf_counter() - start
        if elapsed >= slow_threshold_seconds:
            app.logger.warning(
                "SLOW REQUEST %s %s %.3fs status=%s",
                request.method,
                request.path,
                elapsed,
                response.status_code,
            )
        return response
