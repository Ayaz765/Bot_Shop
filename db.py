"""SQLite storage: past invoices, per-item rate history, running mismatch totals."""

import difflib
import os
import sqlite3

# Overridable so a host with an ephemeral filesystem (e.g. Railway) can point this
# at a mounted persistent volume instead of losing the db on every redeploy.
DB_PATH = os.environ.get("DB_PATH", "billcheck.db")
NAME_MATCH_CUTOFF = 0.8  # how close an item name must be to count as "the same item"


def _normalize(name):
    return " ".join(name.lower().split())


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            vendor_name TEXT NOT NULL,
            item_name TEXT NOT NULL,
            unit TEXT,
            qty REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            vendor_name TEXT NOT NULL,
            item_name TEXT NOT NULL,
            unit TEXT,
            change REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def find_supplier(conn, name):
    """Fuzzy-match a typed supplier name against ones already seen. None if no match."""
    known = [row[0] for row in conn.execute("SELECT DISTINCT supplier_name FROM invoices").fetchall()]
    normalized_to_actual = {_normalize(n): n for n in known}
    match = difflib.get_close_matches(_normalize(name), normalized_to_actual.keys(), n=1, cutoff=NAME_MATCH_CUTOFF)
    return normalized_to_actual[match[0]] if match else None


def get_invoices_for_supplier(conn, supplier_name, limit=10):
    """Most recent invoices for one supplier, newest first."""
    rows = conn.execute(
        "SELECT invoice_number, invoice_date, grand_total, total_loss FROM invoices "
        "WHERE supplier_name = ? ORDER BY id DESC LIMIT ?",
        (supplier_name, limit),
    ).fetchall()
    return [
        {"invoice_number": r[0], "invoice_date": r[1], "grand_total": r[2], "total_loss": r[3]}
        for r in rows
    ]


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
    """Past rates charged by this supplier for this item, oldest first.

    Matches item names fuzzily (case/spacing-insensitive) since the same
    product can come out of extraction slightly differently each time
    ("Tata Salt 1Kg" vs "TATA SALT 1 KG").
    """
    known_names = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT li.item_name FROM line_items li "
            "JOIN invoices i ON i.id = li.invoice_id WHERE i.supplier_name = ?",
            (supplier_name,),
        ).fetchall()
    ]
    normalized_to_actual = {_normalize(name): name for name in known_names}
    match = difflib.get_close_matches(
        _normalize(item_name), normalized_to_actual.keys(), n=1, cutoff=NAME_MATCH_CUTOFF
    )
    if not match:
        return []
    matched_name = normalized_to_actual[match[0]]

    rows = conn.execute(
        """
        SELECT li.rate
        FROM line_items li
        JOIN invoices i ON i.id = li.invoice_id
        WHERE i.supplier_name = ? AND li.item_name = ?
        ORDER BY i.id ASC
        """,
        (supplier_name, matched_name),
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


def _fuzzy_match(query, known_names):
    """Best match for `query` among `known_names`. Checks substring containment first
    (so a nickname like "Ramesh" finds "Ramesh Traders" — difflib's ratio alone penalizes
    that length gap too heavily), then falls back to difflib for typos/case/spacing."""
    if not known_names:
        return None
    normalized_to_actual = {_normalize(n): n for n in known_names}
    target = _normalize(query)

    if target in normalized_to_actual:
        return normalized_to_actual[target]

    substring_matches = [n for n in normalized_to_actual if target in n or n in target]
    if substring_matches:
        best = min(substring_matches, key=lambda n: abs(len(n) - len(target)))
        return normalized_to_actual[best]

    close = difflib.get_close_matches(target, normalized_to_actual.keys(), n=1, cutoff=NAME_MATCH_CUTOFF)
    return normalized_to_actual[close[0]] if close else None


def _find_vendor_in_stock(conn, owner_id, vendor_name):
    """Fuzzy-match a vendor name against ones this owner already has in stock. None if no match."""
    known = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT vendor_name FROM stock WHERE owner_id = ?", (owner_id,)
        ).fetchall()
    ]
    return _fuzzy_match(vendor_name, known)


def _find_item_for_vendor(conn, owner_id, vendor_name, item_name):
    """Fuzzy-match an item name against one vendor's existing stock rows. None if no match."""
    known = [
        row[0] for row in conn.execute(
            "SELECT item_name FROM stock WHERE owner_id = ? AND vendor_name = ?", (owner_id, vendor_name)
        ).fetchall()
    ]
    return _fuzzy_match(item_name, known)


def add_stock(conn, owner_id, vendor_name, item_name, qty, unit=None):
    """Delivery: add qty to a vendor's stock of an item, creating the vendor/item if new.
    Scoped to owner_id so different people's vendor lists never mix.

    Returns the canonical (vendor_name, item_name, unit, new_qty) — canonical meaning
    whatever spelling was already on record, so repeat deliveries with slightly
    different extraction wording accumulate onto the same row. A newly-given unit
    overwrites the stored one (assumes the latest reading is right); passing none
    leaves whatever was already on record.
    """
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name) or vendor_name
    item = _find_item_for_vendor(conn, owner_id, vendor, item_name)

    if item:
        if unit:
            conn.execute(
                "UPDATE stock SET qty = qty + ?, unit = ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
                (qty, unit, owner_id, vendor, item),
            )
        else:
            conn.execute(
                "UPDATE stock SET qty = qty + ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
                (qty, owner_id, vendor, item),
            )
    else:
        item = item_name
        conn.execute(
            "INSERT INTO stock (owner_id, vendor_name, item_name, unit, qty) VALUES (?, ?, ?, ?, ?)",
            (owner_id, vendor, item, unit, qty),
        )

    conn.execute(
        "INSERT INTO stock_movements (owner_id, vendor_name, item_name, unit, change, reason) VALUES (?, ?, ?, ?, ?, 'delivery')",
        (owner_id, vendor, item, unit, qty),
    )
    conn.commit()

    row = conn.execute(
        "SELECT unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?", (owner_id, vendor, item)
    ).fetchone()
    return vendor, item, row[0], row[1]


def find_item_across_vendors(conn, owner_id, item_name):
    """Fuzzy-matches item_name against this owner's vendors' stock. Used to resolve a
    sale when the vendor wasn't mentioned: 0 matches = unknown item, 1 = unambiguous,
    >1 = ask the user which vendor's stock to sell from."""
    rows = conn.execute(
        "SELECT vendor_name, item_name, unit, qty FROM stock WHERE owner_id = ?", (owner_id,)
    ).fetchall()
    target = _normalize(item_name)
    matches = []
    for vendor, item, unit, qty in rows:
        normalized_item = _normalize(item)
        is_match = (
            target == normalized_item
            or target in normalized_item
            or normalized_item in target
            or difflib.SequenceMatcher(None, target, normalized_item).ratio() >= NAME_MATCH_CUTOFF
        )
        if is_match:
            matches.append({"vendor_name": vendor, "item_name": item, "unit": unit, "qty": qty})
    return matches


def record_sale(conn, owner_id, vendor_name, item_name, qty, unit=None):
    """Sale: subtract qty from a vendor's stock of an item. Not clamped at 0 —
    a negative number is an honest signal something's off, not hidden."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name) or vendor_name
    item = _find_item_for_vendor(conn, owner_id, vendor, item_name) or item_name

    conn.execute(
        "UPDATE stock SET qty = qty - ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (qty, owner_id, vendor, item),
    )
    conn.execute(
        "INSERT INTO stock_movements (owner_id, vendor_name, item_name, unit, change, reason) VALUES (?, ?, ?, ?, ?, 'sale')",
        (owner_id, vendor, item, unit, -qty),
    )
    conn.commit()

    row = conn.execute(
        "SELECT unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?", (owner_id, vendor, item)
    ).fetchone()
    return (vendor, item, row[0], row[1]) if row else (vendor, item, unit, -qty)


def get_stock_for_vendor(conn, owner_id, vendor_name):
    """All items and quantities on hand for one (fuzzy-resolved) vendor of this owner's."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return []
    rows = conn.execute(
        "SELECT item_name, unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? ORDER BY item_name",
        (owner_id, vendor),
    ).fetchall()
    return [{"item_name": r[0], "unit": r[1], "qty": r[2]} for r in rows]
