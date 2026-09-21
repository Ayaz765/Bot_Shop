"""SQLite storage: past invoices, per-item rate history, running mismatch totals."""

import difflib
import os
import re
import sqlite3
import uuid
from datetime import datetime

# Overridable so a host with an ephemeral filesystem (e.g. Railway) can point this
# at a mounted persistent volume instead of losing the db on every redeploy.
DB_PATH = os.environ.get("DB_PATH", "billcheck.db")
NAME_MATCH_CUTOFF = 0.8  # how close an item name must be to count as "the same item"


def _normalize(name):
    return " ".join(name.lower().split())


def get_connection(db_path=DB_PATH):
    dirname = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(dirname, exist_ok=True)
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
            batch_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            owner_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS digest_log (
            owner_id INTEGER PRIMARY KEY,
            last_sent_date TEXT NOT NULL
        )
    """)
    # Migration for dbs created before batch_id existed — CREATE TABLE IF NOT
    # EXISTS above is a no-op on an already-existing table, so old installs need
    # this column added by hand. Old rows keep batch_id NULL, which undo_last_
    # movement treats as "a batch of one" (its original single-row behavior).
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(stock_movements)").fetchall()]
    if "batch_id" not in existing_cols:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN batch_id TEXT")
    conn.commit()


def new_batch_id():
    """One id shared by every stock_movements row that came from the same
    user message — "Maggi, Egg, Pizza aaya" in one go groups all three so
    undo_last_movement can reverse the whole thing, not just the last item."""
    return uuid.uuid4().hex[:12]


def get_user_name(conn, owner_id):
    """A person's saved name, or None if they've never told us — survives bot restarts."""
    row = conn.execute("SELECT name FROM users WHERE owner_id = ?", (owner_id,)).fetchone()
    return row[0] if row else None


def set_user_name(conn, owner_id, name):
    conn.execute(
        "INSERT INTO users (owner_id, name) VALUES (?, ?) "
        "ON CONFLICT(owner_id) DO UPDATE SET name = excluded.name",
        (owner_id, name),
    )
    conn.commit()


def find_supplier(conn, name):
    """Fuzzy-match a typed supplier name against ones already seen. None if no match."""
    known = [row[0] for row in conn.execute("SELECT DISTINCT supplier_name FROM invoices").fetchall()]
    normalized_to_actual = {_normalize(n): n for n in known}
    target = _normalize(name)
    candidates = [k for k in normalized_to_actual if not _numbers_conflict(target, k)]
    match = difflib.get_close_matches(target, candidates, n=1, cutoff=NAME_MATCH_CUTOFF)
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
    target = _normalize(item_name)
    candidates = [k for k in normalized_to_actual if not _numbers_conflict(target, k)]
    match = difflib.get_close_matches(target, candidates, n=1, cutoff=NAME_MATCH_CUTOFF)
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


def _numbers_conflict(a, b):
    """True if both names contain digits and those digit sequences differ —
    e.g. "maggi 100g" vs "maggi 200g" scores 0.9 on difflib's ratio (one
    character out of many), easily clearing NAME_MATCH_CUTOFF, but they're two
    different pack sizes, not a typo/OCR variant of the same item. Numbers in
    a product/vendor name are almost always meaningful, unlike letter case or
    spacing, so a plain similarity score can't be trusted to tell these apart
    — silently merging two real items is worse than missing a real typo match
    (same reasoning _find_vendor_exact uses for vendors)."""
    nums_a = re.findall(r"\d+", a)
    nums_b = re.findall(r"\d+", b)
    return bool(nums_a) and bool(nums_b) and nums_a != nums_b


def _fuzzy_match(query, known_names):
    """Best match for `query` among `known_names`. Checks substring containment first
    (so a nickname like "Ramesh" finds "Ramesh Traders" — difflib's ratio alone penalizes
    that length gap too heavily), then falls back to difflib for typos/case/spacing.
    Candidates whose numbers conflict with the query's (see _numbers_conflict) are
    excluded before either check, so different pack sizes never merge."""
    if not known_names:
        return None
    normalized_to_actual = {_normalize(n): n for n in known_names}
    target = _normalize(query)

    if target in normalized_to_actual:
        return normalized_to_actual[target]

    candidates = {k: v for k, v in normalized_to_actual.items() if not _numbers_conflict(target, k)}
    if not candidates:
        return None

    substring_matches = [n for n in candidates if target in n or n in target]
    if substring_matches:
        best = min(substring_matches, key=lambda n: abs(len(n) - len(target)))
        return candidates[best]

    close = difflib.get_close_matches(target, candidates.keys(), n=1, cutoff=NAME_MATCH_CUTOFF)
    return candidates[close[0]] if close else None


def _find_vendor_in_stock(conn, owner_id, vendor_name):
    """Fuzzy-match a vendor name against ones this owner already has in stock. None if no match."""
    known = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT vendor_name FROM stock WHERE owner_id = ?", (owner_id,)
        ).fetchall()
    ]
    return _fuzzy_match(vendor_name, known)


def _find_vendor_exact(conn, owner_id, vendor_name):
    """Case/whitespace-insensitive EXACT match only — no substring or typo
    fuzziness. Used by add_stock when deciding whether new stock belongs to an
    existing vendor or a brand-new one: _find_vendor_in_stock's substring rule
    (needed so "Ramesh" resolves to "Ramesh Traders" when selling/querying) also
    silently folded a genuinely new "Karan Traders" into an existing "Karan" —
    two different vendors merged into one with no way to tell they'd split.
    Wrongly creating a near-duplicate vendor (visible, fixable via rename) is a
    smaller problem than wrongly merging two real ones (invisible, not)."""
    known = [
        row[0] for row in conn.execute(
            "SELECT DISTINCT vendor_name FROM stock WHERE owner_id = ?", (owner_id,)
        ).fetchall()
    ]
    target = _normalize(vendor_name)
    for name in known:
        if _normalize(name) == target:
            return name
    return None


def _find_item_for_vendor(conn, owner_id, vendor_name, item_name):
    """Fuzzy-match an item name against one vendor's existing stock rows. None if no match."""
    known = [
        row[0] for row in conn.execute(
            "SELECT item_name FROM stock WHERE owner_id = ? AND vendor_name = ?", (owner_id, vendor_name)
        ).fetchall()
    ]
    return _fuzzy_match(item_name, known)


def add_stock(conn, owner_id, vendor_name, item_name, qty, unit=None, batch_id=None):
    """Delivery: add qty to a vendor's stock of an item, creating the vendor/item if new.
    Scoped to owner_id so different people's vendor lists never mix.

    Returns the canonical (vendor_name, item_name, unit, new_qty) — canonical meaning
    whatever spelling was already on record, so repeat deliveries with slightly
    different extraction wording accumulate onto the same row. A newly-given unit
    overwrites the stored one (assumes the latest reading is right); passing none
    leaves whatever was already on record.

    batch_id (optional): tag applied to the stock_movements row so multiple items
    from one message ("Maggi, Egg aaya") can be undone together as a unit.
    """
    vendor = _find_vendor_exact(conn, owner_id, vendor_name) or vendor_name
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
        "INSERT INTO stock_movements (owner_id, vendor_name, item_name, unit, change, reason, batch_id) "
        "VALUES (?, ?, ?, ?, ?, 'delivery', ?)",
        (owner_id, vendor, item, unit, qty, batch_id),
    )
    conn.commit()

    row = conn.execute(
        "SELECT unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?", (owner_id, vendor, item)
    ).fetchone()
    return vendor, item, row[0], row[1]


def rename_item(conn, owner_id, vendor_name, old_name, new_name):
    """Corrects a misspelled/mis-transcribed item name for one vendor ("Maggie"
    typed by mistake for "Maggi"). If new_name fuzzy-matches an item that
    already exists under that vendor, the two are merged (quantities added,
    old row dropped) instead of creating a duplicate — same fuzzy-identity rule
    the rest of stock uses. Past stock_movements rows are relabeled too, so a
    later "undo" still finds the item under its current name.

    Returns (vendor_name, old_canonical_name, new_canonical_name, unit, qty),
    or None if old_name doesn't match anything on record for this vendor."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return None
    old_item = _find_item_for_vendor(conn, owner_id, vendor, old_name)
    if old_item is None:
        return None

    existing = conn.execute(
        "SELECT qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (owner_id, vendor, old_item),
    ).fetchone()
    old_qty = existing[0]

    target_item = _find_item_for_vendor(conn, owner_id, vendor, new_name)
    if target_item and target_item != old_item:
        conn.execute(
            "UPDATE stock SET qty = qty + ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
            (old_qty, owner_id, vendor, target_item),
        )
        conn.execute(
            "DELETE FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
            (owner_id, vendor, old_item),
        )
        final_name = target_item
    else:
        conn.execute(
            "UPDATE stock SET item_name = ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
            (new_name, owner_id, vendor, old_item),
        )
        final_name = new_name

    conn.execute(
        "UPDATE stock_movements SET item_name = ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (final_name, owner_id, vendor, old_item),
    )
    conn.commit()

    row = conn.execute(
        "SELECT unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (owner_id, vendor, final_name),
    ).fetchone()
    return vendor, old_item, final_name, row[0], row[1]


def rename_vendor(conn, owner_id, old_name, new_name):
    """Corrects a misspelled/mis-transcribed vendor name across all of that
    vendor's stock (every item, and its stock_movements history) — the vendor-
    level counterpart to rename_item. If new_name matches a vendor that already
    exists, each of the old vendor's items merges into it (quantities added
    onto a matching item there, moved over as a new item otherwise) instead of
    leaving the same vendor split under two names.

    Returns (old_canonical_name, new_canonical_name), or None if old_name
    doesn't match any vendor on record for this owner."""
    vendor = _find_vendor_in_stock(conn, owner_id, old_name)
    if not vendor:
        return None

    target_vendor = _find_vendor_exact(conn, owner_id, new_name)
    if target_vendor and target_vendor != vendor:
        rows = conn.execute(
            "SELECT item_name, qty FROM stock WHERE owner_id = ? AND vendor_name = ?", (owner_id, vendor)
        ).fetchall()
        for item_name, qty in rows:
            existing_item = _find_item_for_vendor(conn, owner_id, target_vendor, item_name)
            if existing_item:
                conn.execute(
                    "UPDATE stock SET qty = qty + ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
                    (qty, owner_id, target_vendor, existing_item),
                )
            else:
                conn.execute(
                    "UPDATE stock SET vendor_name = ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
                    (target_vendor, owner_id, vendor, item_name),
                )
        # whatever's left under the old name is the merged-away items (moved rows
        # above already carry target_vendor, so this only catches those)
        conn.execute("DELETE FROM stock WHERE owner_id = ? AND vendor_name = ?", (owner_id, vendor))
        conn.execute(
            "UPDATE stock_movements SET vendor_name = ? WHERE owner_id = ? AND vendor_name = ?",
            (target_vendor, owner_id, vendor),
        )
        conn.commit()
        return vendor, target_vendor

    conn.execute(
        "UPDATE stock SET vendor_name = ? WHERE owner_id = ? AND vendor_name = ?", (new_name, owner_id, vendor)
    )
    conn.execute(
        "UPDATE stock_movements SET vendor_name = ? WHERE owner_id = ? AND vendor_name = ?",
        (new_name, owner_id, vendor),
    )
    conn.commit()
    return vendor, new_name


def delete_item(conn, owner_id, vendor_name, item_name):
    """Permanently drops one item from a vendor's stock list — unlike record_sale
    (which only lowers qty and keeps the row/history), this removes the row
    entirely. stock_movements rows are left as-is; they're history of what
    happened, not current stock. Returns (vendor_name, item_name) as matched,
    or None if either the vendor or the item doesn't exist on record."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return None
    item = _find_item_for_vendor(conn, owner_id, vendor, item_name)
    if item is None:
        return None
    conn.execute(
        "DELETE FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (owner_id, vendor, item),
    )
    conn.commit()
    return vendor, item


def delete_vendor(conn, owner_id, vendor_name):
    """Permanently drops a vendor and every item in its stock list. Returns the
    canonical vendor_name as matched, or None if this owner has no such vendor."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return None
    conn.execute(
        "DELETE FROM stock WHERE owner_id = ? AND vendor_name = ?",
        (owner_id, vendor),
    )
    conn.commit()
    return vendor


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
        if _numbers_conflict(target, normalized_item):
            continue  # e.g. "maggi 100g" must never match a stocked "maggi 200g"
        is_match = (
            target == normalized_item
            or target in normalized_item
            or normalized_item in target
            or difflib.SequenceMatcher(None, target, normalized_item).ratio() >= NAME_MATCH_CUTOFF
        )
        if is_match:
            matches.append({"vendor_name": vendor, "item_name": item, "unit": unit, "qty": qty})
    return matches


def get_item_sales_totals(conn, owner_id, vendor_name=None):
    """Total units sold (reason='sale' stock_movements) per (vendor, item),
    only rows with something actually sold — feeds "sabse zyada/kam bika kya".
    Left un-grouped across vendors (one row per vendor+item) so the caller can
    fold same-named items from different vendors into one ranking, same
    division of labor as get_low_stock_items."""
    if vendor_name:
        vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
        if not vendor:
            return []
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, -SUM(change) FROM stock_movements "
            "WHERE owner_id = ? AND vendor_name = ? AND reason = 'sale' GROUP BY vendor_name, item_name",
            (owner_id, vendor),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, -SUM(change) FROM stock_movements "
            "WHERE owner_id = ? AND reason = 'sale' GROUP BY vendor_name, item_name",
            (owner_id,),
        ).fetchall()
    return [{"vendor_name": r[0], "item_name": r[1], "unit": r[2], "sold": r[3]} for r in rows if r[3] > 0]


def record_sale(conn, owner_id, vendor_name, item_name, qty, unit=None, batch_id=None):
    """Sale: subtract qty from a vendor's stock of an item. Not clamped at 0 —
    a negative number is an honest signal something's off, not hidden.

    Returns None if this vendor has no record of the item at all — there's
    nothing to sell, so no row gets created (previously this silently made a
    fresh row and sold it into negative, e.g. a never-stocked "pizza" showing
    "-50 pcs bacha").

    batch_id (optional): tag applied to the stock_movements row so multiple items
    from one message can be undone together as a unit.
    """
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name) or vendor_name
    item = _find_item_for_vendor(conn, owner_id, vendor, item_name)
    if item is None:
        return None

    conn.execute(
        "UPDATE stock SET qty = qty - ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (qty, owner_id, vendor, item),
    )
    conn.execute(
        "INSERT INTO stock_movements (owner_id, vendor_name, item_name, unit, change, reason, batch_id) "
        "VALUES (?, ?, ?, ?, ?, 'sale', ?)",
        (owner_id, vendor, item, unit, -qty, batch_id),
    )
    conn.commit()

    row = conn.execute(
        "SELECT unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?", (owner_id, vendor, item)
    ).fetchone()
    return vendor, item, row[0], row[1]


MIN_SALE_DATAPOINTS = 3  # fewer sale records than this and a daily-rate guess is just noise
MIN_SALE_SPAN_DAYS = 2  # sales bunched within a day or so don't reveal an actual daily rate


def estimate_days_left(conn, owner_id, vendor_name, item_name, current_qty):
    """Rough days-until-stockout from this item's own sale history: total units
    sold over the span between its first and last recorded sale gives an average
    daily rate, and current_qty / that rate is the forecast.

    Returns None — not a lowball guess — when there isn't enough sale history to
    trust a rate (see MIN_SALE_DATAPOINTS/MIN_SALE_SPAN_DAYS), when nothing has
    sold, or when current_qty is already 0 or negative (already out, not a
    forecast question). A wrong forecast costs more shopkeeper trust than no
    forecast at all — same reasoning as checker.py's false-alarm rule."""
    if current_qty is None or current_qty <= 0:
        return None
    rows = conn.execute(
        "SELECT change, created_at FROM stock_movements "
        "WHERE owner_id = ? AND vendor_name = ? AND item_name = ? AND reason = 'sale' "
        "ORDER BY created_at",
        (owner_id, vendor_name, item_name),
    ).fetchall()
    if len(rows) < MIN_SALE_DATAPOINTS:
        return None

    total_sold = -sum(change for change, _created_at in rows)  # sale rows are stored negative
    if total_sold <= 0:
        return None
    first = datetime.strptime(rows[0][1], "%Y-%m-%d %H:%M:%S")
    last = datetime.strptime(rows[-1][1], "%Y-%m-%d %H:%M:%S")
    span_days = (last - first).total_seconds() / 86400
    if span_days < MIN_SALE_SPAN_DAYS:
        return None

    daily_rate = total_sold / span_days
    return current_qty / daily_rate


def undo_last_movement(conn, owner_id):
    """Reverses this owner's most recent BATCH of stock movements — everything
    that shares the last row's batch_id, e.g. all three items from one "Maggi,
    Egg, Pizza aaya" message, not just the last one. Old rows from before
    batch_id existed have it NULL, which falls back to the original one-row
    behavior. Reversing an undo is just a redo (the compensating rows get their
    own fresh batch_id, so undoing them again reverses the whole undo as a unit).

    Returns a list of (vendor_name, item_name, unit, new_qty, reason) tuples, one
    per movement undone, or None if there's nothing to undo."""
    last = conn.execute(
        "SELECT id, batch_id FROM stock_movements WHERE owner_id = ? ORDER BY id DESC LIMIT 1",
        (owner_id,),
    ).fetchone()
    if not last:
        return None
    last_id, batch_id = last

    if batch_id:
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, change, reason FROM stock_movements "
            "WHERE owner_id = ? AND batch_id = ? ORDER BY id",
            (owner_id, batch_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, change, reason FROM stock_movements WHERE id = ?",
            (last_id,),
        ).fetchall()

    undo_batch_id = new_batch_id()
    results = []
    for vendor, item, unit, change, reason in rows:
        conn.execute(
            "UPDATE stock SET qty = qty - ? WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
            (change, owner_id, vendor, item),
        )
        conn.execute(
            "INSERT INTO stock_movements (owner_id, vendor_name, item_name, unit, change, reason, batch_id) "
            "VALUES (?, ?, ?, ?, ?, 'undo', ?)",
            (owner_id, vendor, item, unit, -change, undo_batch_id),
        )
        result = conn.execute(
            "SELECT qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
            (owner_id, vendor, item),
        ).fetchone()
        new_qty = result[0] if result else -change
        results.append((vendor, item, unit, new_qty, reason))

    conn.commit()
    return results


def get_item_qty(conn, owner_id, vendor_name, item_name):
    """Current on-hand qty for one (fuzzy-resolved) vendor+item, or None if it
    doesn't exist. Used to resolve "sara/pura bik gaya" (sold everything) into
    a real number pulled from the ledger — not a guessed one."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return None
    item = _find_item_for_vendor(conn, owner_id, vendor, item_name)
    if item is None:
        return None
    row = conn.execute(
        "SELECT qty FROM stock WHERE owner_id = ? AND vendor_name = ? AND item_name = ?",
        (owner_id, vendor, item),
    ).fetchone()
    return row[0] if row else None


def get_owners_with_stock(conn):
    """Distinct owner_ids that have at least one stock row — candidates for the
    daily low-stock digest (nothing to check for someone with no stock yet)."""
    rows = conn.execute("SELECT DISTINCT owner_id FROM stock").fetchall()
    return [r[0] for r in rows]


def get_low_stock_items(conn, owner_id, threshold):
    """Every item across all of this owner's vendors at or below threshold
    (out-of-stock/negative included), lowest qty first — feeds the daily digest
    so it doesn't have to loop get_stock_for_vendor over every vendor itself."""
    rows = conn.execute(
        "SELECT vendor_name, item_name, unit, qty FROM stock WHERE owner_id = ? AND qty <= ? ORDER BY qty",
        (owner_id, threshold),
    ).fetchall()
    return [{"vendor_name": r[0], "item_name": r[1], "unit": r[2], "qty": r[3]} for r in rows]


def get_last_digest_date(conn, owner_id):
    row = conn.execute("SELECT last_sent_date FROM digest_log WHERE owner_id = ?", (owner_id,)).fetchone()
    return row[0] if row else None


def set_last_digest_date(conn, owner_id, date_str):
    conn.execute(
        "INSERT INTO digest_log (owner_id, last_sent_date) VALUES (?, ?) "
        "ON CONFLICT(owner_id) DO UPDATE SET last_sent_date = excluded.last_sent_date",
        (owner_id, date_str),
    )
    conn.commit()


def get_vendors(conn, owner_id):
    """Distinct vendor names this owner has any stock record for — used to answer
    "which vendors do I have" with real names instead of the bot guessing some."""
    rows = conn.execute(
        "SELECT DISTINCT vendor_name FROM stock WHERE owner_id = ? ORDER BY vendor_name",
        (owner_id,),
    ).fetchall()
    return [r[0] for r in rows]


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


def get_stock_with_last_delivery(conn, owner_id, vendor_name):
    """Same as get_stock_for_vendor, but each item also carries "last_delivery"
    — the raw UTC timestamp string of its most recent 'delivery' movement, or
    None if it's never been restocked (e.g. added only via a sale-side merge).
    Lets the stock list answer "ye kab aaya tha" without a separate query."""
    vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
    if not vendor:
        return []
    rows = conn.execute(
        "SELECT item_name, unit, qty FROM stock WHERE owner_id = ? AND vendor_name = ? ORDER BY item_name",
        (owner_id, vendor),
    ).fetchall()
    dates = dict(conn.execute(
        "SELECT item_name, MAX(created_at) FROM stock_movements "
        "WHERE owner_id = ? AND vendor_name = ? AND reason = 'delivery' GROUP BY item_name",
        (owner_id, vendor),
    ).fetchall())
    return [{"item_name": r[0], "unit": r[1], "qty": r[2], "last_delivery": dates.get(r[0])} for r in rows]


def get_delivery_history(conn, owner_id, vendor_name=None, limit=30):
    """Recent deliveries (reason='delivery' movements), newest first — answers
    "kis din kya aaya kis vendor se". Optionally scoped to one (fuzzy-resolved)
    vendor; None means across all of this owner's vendors."""
    if vendor_name:
        vendor = _find_vendor_in_stock(conn, owner_id, vendor_name)
        if not vendor:
            return []
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, change, created_at FROM stock_movements "
            "WHERE owner_id = ? AND vendor_name = ? AND reason = 'delivery' "
            "ORDER BY id DESC LIMIT ?",
            (owner_id, vendor, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT vendor_name, item_name, unit, change, created_at FROM stock_movements "
            "WHERE owner_id = ? AND reason = 'delivery' ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
    return [{"vendor_name": r[0], "item_name": r[1], "unit": r[2], "qty": r[3], "created_at": r[4]} for r in rows]
