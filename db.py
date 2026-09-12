"""SQLite storage: past invoices, per-item rate history, running mismatch totals."""

import sqlite3

DB_PATH = "billcheck.db"


def get_connection(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_name TEXT NOT NULL,
            invoice_number TEXT,
            invoice_date TEXT,
            grand_total REAL,
            total_loss REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS line_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            item_name TEXT NOT NULL,
            qty REAL,
            rate REAL,
            amount REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES invoices(id),
            type TEXT,
            severity TEXT,
            item TEXT,
            msg TEXT,
            loss REAL
        )
    """)
    conn.commit()


def is_duplicate(conn, supplier_name, invoice_number):
    """Return the earlier invoice id if this supplier+invoice_number was seen before, else None."""
    if not invoice_number:
        return None
    row = conn.execute(
        "SELECT id FROM invoices WHERE supplier_name = ? AND invoice_number = ?",
        (supplier_name, invoice_number),
    ).fetchone()
    return row[0] if row else None


def get_rate_history(conn, supplier_name, item_name):
    """Past rates charged by this supplier for this item, oldest first."""
    rows = conn.execute(
        """
        SELECT li.rate
        FROM line_items li
        JOIN invoices i ON i.id = li.invoice_id
        WHERE i.supplier_name = ? AND li.item_name = ?
        ORDER BY i.id ASC
        """,
        (supplier_name, item_name),
    ).fetchall()
    return [r[0] for r in rows]


def save_invoice(conn, supplier_name, invoice_number, invoice_date, grand_total, line_items, issues):
    """Persist an invoice with its line items and flagged issues. Returns the new invoice id."""
    total_loss = sum(issue["loss"] for issue in issues)
    cur = conn.execute(
        "INSERT INTO invoices (supplier_name, invoice_number, invoice_date, grand_total, total_loss) VALUES (?, ?, ?, ?, ?)",
        (supplier_name, invoice_number, invoice_date, grand_total, total_loss),
    )
    invoice_id = cur.lastrowid

    for item in line_items:
        conn.execute(
            "INSERT INTO line_items (invoice_id, item_name, qty, rate, amount) VALUES (?, ?, ?, ?, ?)",
            (invoice_id, item["name"], item.get("qty"), item.get("rate"), item.get("amount")),
        )

    for issue in issues:
        conn.execute(
            "INSERT INTO issues (invoice_id, type, severity, item, msg, loss) VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_id, issue.get("type"), issue.get("severity"), issue.get("item"), issue.get("msg"), issue.get("loss")),
        )

    conn.commit()
    return invoice_id


def get_running_total_loss(conn, supplier_name=None):
    """Total money found stuck across all saved invoices, optionally for one supplier."""
    if supplier_name:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_loss), 0) FROM invoices WHERE supplier_name = ?",
            (supplier_name,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COALESCE(SUM(total_loss), 0) FROM invoices").fetchone()
    return row[0]
