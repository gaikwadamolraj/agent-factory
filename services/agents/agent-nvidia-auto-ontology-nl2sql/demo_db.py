import sqlite3
from pathlib import Path


def ensure_demo_database(db_path: str = "demo_enterprise.db") -> str:
    """Creates a sample enterprise database with ambiguous columns to demonstrate Auto-Ontology."""
    path = Path(db_path)
    if path.exists():
        return f"sqlite:///{path.resolve()}"

    conn = sqlite3.connect(str(path))
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE customers (
        cust_id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL,
        segment TEXT NOT NULL,
        account_status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE orders (
        order_id INTEGER PRIMARY KEY,
        cust_id INTEGER NOT NULL,
        gross_amt REAL NOT NULL,
        discount_amt REAL NOT NULL,
        tax_amt REAL NOT NULL,
        order_status TEXT NOT NULL,
        order_date TEXT NOT NULL,
        FOREIGN KEY (cust_id) REFERENCES customers(cust_id)
    );

    INSERT INTO customers VALUES
        (1, 'Acme Corp', 'Enterprise', 'ACTIVE', '2025-01-10'),
        (2, 'Globex Ltd', 'Mid-Market', 'ACTIVE', '2025-03-15'),
        (3, 'Initech', 'SMB', 'CHURNED', '2024-11-01');

    INSERT INTO orders VALUES
        (101, 1, 10000.0, 1000.0, 1800.0, 'COMPLETED', '2026-10-05'),
        (102, 1, 5000.0, 0.0, 900.0, 'CANCELLED', '2026-10-12'),
        (103, 2, 7500.0, 500.0, 1350.0, 'COMPLETED', '2026-10-20');
    """)
    conn.commit()
    conn.close()
    return f"sqlite:///{path.resolve()}"
