CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    customer_name VARCHAR(100) NOT NULL,
    contact_no VARCHAR(20),
    previous_balance NUMERIC(12, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier (
    supplier_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    supplier_name VARCHAR(100) NOT NULL,
    contact_no VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS category (
    category_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    category_name VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS item (
    item_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    item_name VARCHAR(100) NOT NULL,
    category_id INTEGER REFERENCES category(category_id),
    purchase_rate NUMERIC(10, 2) DEFAULT 0,
    sale_rate NUMERIC(10, 2) DEFAULT 0,
    qty INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    purchase_date DATE NOT NULL,
    supplier_id INTEGER REFERENCES supplier(supplier_id),
    total_amount NUMERIC(12, 2) DEFAULT 0,
    payment_method VARCHAR(20) DEFAULT 'Cash',
    payment_status VARCHAR(20) DEFAULT 'Unpaid'
);

CREATE TABLE IF NOT EXISTS purchase_details (
    detail_id SERIAL PRIMARY KEY,
    purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES item(item_id),
    particulars VARCHAR(255),
    qty INTEGER NOT NULL,
    purchase_rate NUMERIC(10, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_payments (
    payment_id SERIAL PRIMARY KEY,
    purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL,
    payment_date TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    notes VARCHAR(255),
    payment_method VARCHAR(20) DEFAULT 'Cash'
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    date DATE NOT NULL,
    total_amount NUMERIC(12, 2) DEFAULT 0,
    payment_status VARCHAR(20) DEFAULT 'Unpaid',
    previous_balance NUMERIC(12, 2) DEFAULT 0,
    cash_received NUMERIC(12, 2) DEFAULT 0,
    net_balance NUMERIC(12, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoice_details (
    detail_id SERIAL PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES item(item_id),
    particulars VARCHAR(255),
    qty INTEGER NOT NULL,
    rate NUMERIC(10, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoice_payments (
    payment_id SERIAL PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    amount NUMERIC(12, 2) NOT NULL,
    payment_date TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    notes VARCHAR(255),
    payment_method VARCHAR(20) DEFAULT 'Cash'
);

CREATE TABLE IF NOT EXISTS cash_accounts (
    account_id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    cash_opening NUMERIC(12, 2) DEFAULT 0,
    bank_opening NUMERIC(12, 2) DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice_id
    ON invoice_payments (invoice_id);

CREATE INDEX IF NOT EXISTS idx_invoice_payments_payment_date
    ON invoice_payments (payment_date);

CREATE INDEX IF NOT EXISTS idx_invoice_payments_invoice_date
    ON invoice_payments (invoice_id, payment_date);

CREATE INDEX IF NOT EXISTS idx_invoices_user_id
    ON invoices (user_id);

CREATE INDEX IF NOT EXISTS idx_invoices_user_invoice_id
    ON invoices (user_id, invoice_id DESC);

CREATE INDEX IF NOT EXISTS idx_invoices_customer_id
    ON invoices (customer_id);

CREATE INDEX IF NOT EXISTS idx_invoice_details_invoice_id
    ON invoice_details (invoice_id);

CREATE INDEX IF NOT EXISTS idx_invoice_details_item_id
    ON invoice_details (item_id);

CREATE INDEX IF NOT EXISTS idx_item_user_id
    ON item (user_id);

CREATE INDEX IF NOT EXISTS idx_item_user_item_no
    ON item (user_id, item_no);

CREATE INDEX IF NOT EXISTS idx_item_category_id
    ON item (category_id);

CREATE INDEX IF NOT EXISTS idx_customers_user_id
    ON customers (user_id);

CREATE INDEX IF NOT EXISTS idx_customers_user_name
    ON customers (user_id, customer_name);

CREATE INDEX IF NOT EXISTS idx_purchases_user_id
    ON purchases (user_id);

CREATE INDEX IF NOT EXISTS idx_purchases_supplier_id
    ON purchases (supplier_id);

CREATE INDEX IF NOT EXISTS idx_purchases_user_date
    ON purchases (user_id, purchase_date DESC);

CREATE INDEX IF NOT EXISTS idx_purchase_details_purchase_id
    ON purchase_details (purchase_id);

CREATE INDEX IF NOT EXISTS idx_purchase_details_item_id
    ON purchase_details (item_id);

CREATE INDEX IF NOT EXISTS idx_purchase_payments_purchase_id
    ON purchase_payments (purchase_id);

CREATE INDEX IF NOT EXISTS idx_purchase_payments_payment_date
    ON purchase_payments (payment_date);

CREATE INDEX IF NOT EXISTS idx_supplier_user_id
    ON supplier (user_id);

CREATE INDEX IF NOT EXISTS idx_category_user_id
    ON category (user_id);

CREATE INDEX IF NOT EXISTS idx_quotations_user_id
    ON quotations (user_id);

CREATE INDEX IF NOT EXISTS idx_stock_history_user_id
    ON stock_history (user_id);

CREATE INDEX IF NOT EXISTS idx_stock_history_item_id
    ON stock_history (item_id);

CREATE TABLE IF NOT EXISTS quotations (
    quotation_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    quotation_no INTEGER,
    quotation_date DATE NOT NULL,
    customer_id INTEGER REFERENCES customers(customer_id),
    customer_name VARCHAR(100) NOT NULL,
    address VARCHAR(255),
    project VARCHAR(255),
    work_type VARCHAR(100),
    engineer VARCHAR(100),
    contact_no VARCHAR(50),
    notes VARCHAR(500),
    advance NUMERIC(12, 2) DEFAULT 0,
    total_amount NUMERIC(12, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quotation_details (
    detail_id SERIAL PRIMARY KEY,
    quotation_id INTEGER NOT NULL REFERENCES quotations(quotation_id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES item(item_id),
    description VARCHAR(255),
    width NUMERIC(12, 2) DEFAULT 0,
    height NUMERIC(12, 2) DEFAULT 0,
    qty INTEGER NOT NULL,
    rate NUMERIC(10, 2) DEFAULT 0,
    sqft NUMERIC(12, 4) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_history (
    history_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id),
    item_id INTEGER REFERENCES item(item_id),
    purchase_id INTEGER REFERENCES purchases(purchase_id),
    invoice_id INTEGER REFERENCES invoices(invoice_id),
    qty INTEGER NOT NULL,
    action_type VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_quotations_user_no
    ON quotations (user_id, quotation_no);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_accounts_user
    ON cash_accounts (user_id);
