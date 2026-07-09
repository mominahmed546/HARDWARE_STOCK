CREATE TABLE IF NOT EXISTS users (
    user_id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id SERIAL PRIMARY KEY,
    customer_name VARCHAR(100) NOT NULL,
    contact_no VARCHAR(20),
    previous_balance NUMERIC(12, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier (
    supplier_id SERIAL PRIMARY KEY,
    supplier_name VARCHAR(100) NOT NULL,
    contact_no VARCHAR(20)
);

CREATE TABLE IF NOT EXISTS category (
    category_id SERIAL PRIMARY KEY,
    category_name VARCHAR(100) NOT NULL
);

CREATE TABLE IF NOT EXISTS item (
    item_id SERIAL PRIMARY KEY,
    item_name VARCHAR(100) NOT NULL,
    category_id INTEGER REFERENCES category(category_id),
    purchase_rate NUMERIC(10, 2) DEFAULT 0,
    sale_rate NUMERIC(10, 2) DEFAULT 0,
    qty INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchases (
    purchase_id SERIAL PRIMARY KEY,
    purchase_date DATE NOT NULL,
    supplier_id INTEGER REFERENCES supplier(supplier_id),
    total_amount NUMERIC(12, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_details (
    detail_id SERIAL PRIMARY KEY,
    purchase_id INTEGER NOT NULL REFERENCES purchases(purchase_id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES item(item_id),
    particulars VARCHAR(255),
    qty INTEGER NOT NULL,
    purchase_rate NUMERIC(10, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    date DATE NOT NULL,
    total_amount NUMERIC(12, 2) DEFAULT 0,
    payment_status VARCHAR(20) DEFAULT 'Unpaid'
);

CREATE TABLE IF NOT EXISTS invoice_details (
    detail_id SERIAL PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(invoice_id) ON DELETE CASCADE,
    item_id INTEGER REFERENCES item(item_id),
    particulars VARCHAR(255),
    qty INTEGER NOT NULL,
    rate NUMERIC(10, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_history (
    history_id SERIAL PRIMARY KEY,
    item_id INTEGER REFERENCES item(item_id),
    purchase_id INTEGER REFERENCES purchases(purchase_id),
    invoice_id INTEGER REFERENCES invoices(invoice_id),
    qty INTEGER NOT NULL,
    action_type VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
