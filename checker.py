"""The rules engine. Each check_* function is one rule, independent of the others.

Every rule returns a list of issue dicts: {type, severity, item, msg, loss}.
`loss` is real rupees stuck (0.0 for informational flags) so callers can just
sum it for a total.
"""

import statistics

import db

PAISA_TOLERANCE = 1.0    # ignore rounding below this
RATE_HIKE_PCT = 5.0      # % rise before flagging
MIN_FLAG_AMOUNT = 5.0    # don't alert on trivial amounts
LOW_CONFIDENCE_THRESHOLD = 0.6


def check_qty_short(items, received_qty):
    """received_qty: {1-based item index: qty actually received}. Missing index = matches invoice."""
    issues = []
    for i, item in enumerate(items, start=1):
        if item.get("qty") is None or item.get("rate") is None:
            continue
        received = received_qty.get(i, item["qty"])
        short = item["qty"] - received
        if short <= 0:
            continue
        loss = round(short * item["rate"], 2)
        if loss < MIN_FLAG_AMOUNT:
            continue
        issues.append({
            "type": "QTY_SHORT",
            "severity": "high",
            "item": item["name"],
            "msg": f"Bill mein {item['qty']:g} {item['name']}, aaya sirf {received:g}. {short:g} kam.",
            "loss": loss,
        })
    return issues


def check_qty_extra(items, received_qty):
    issues = []
    for i, item in enumerate(items, start=1):
        if item.get("qty") is None or item.get("rate") is None:
            continue
        received = received_qty.get(i, item["qty"])
        extra = received - item["qty"]
        if extra <= 0:
            continue
        potential = round(extra * item["rate"], 2)
        if potential < MIN_FLAG_AMOUNT:
            continue
        issues.append({
            "type": "QTY_EXTRA",
            "severity": "medium",
            "item": item["name"],
            "msg": f"Bill mein {item['qty']:g} {item['name']}, aaya {received:g}. {extra:g} zyada — supplier baad mein iska paisa maang sakta hai.",
            "loss": 0.0,
        })
    return issues


def check_line_math(items):
    issues = []
    for item in items:
        qty, rate, amount = item.get("qty"), item.get("rate"), item.get("amount")
        if qty is None or rate is None or amount is None:
            continue
        expected = round(qty * rate, 2)
        diff = round(amount - expected, 2)
        if diff <= PAISA_TOLERANCE or diff < MIN_FLAG_AMOUNT:
            continue
        issues.append({
            "type": "LINE_MATH",
            "severity": "high",
            "item": item["name"],
            "msg": f"{item['name']}: {qty:g} x Rs{rate:g} = Rs{expected:g} hona chahiye, bill mein Rs{amount:g} likha hai. Rs{diff:g} zyada.",
            "loss": diff,
        })
    return issues


def check_subtotal_math(items, sub_total):
    if sub_total is None:
        return []
    amounts = [item["amount"] for item in items if item.get("amount") is not None]
    if len(amounts) != len(items):
        return []
    actual_sum = round(sum(amounts), 2)
    diff = round(sub_total - actual_sum, 2)
    if diff <= PAISA_TOLERANCE or diff < MIN_FLAG_AMOUNT:
        return []
    return [{
        "type": "SUBTOTAL_MATH",
        "severity": "high",
        "item": None,
        "msg": f"Sub total Rs{actual_sum:g} hona chahiye (lines jodkar), bill mein Rs{sub_total:g} likha hai. Rs{diff:g} zyada.",
        "loss": diff,
    }]


def check_grandtotal_math(sub_total, tax, grand_total):
    if sub_total is None or grand_total is None:
        return []
    expected = round(sub_total + (tax or 0), 2)
    diff = round(grand_total - expected, 2)
    if diff <= PAISA_TOLERANCE or diff < MIN_FLAG_AMOUNT:
        return []
    return [{
        "type": "GRANDTOTAL_MATH",
        "severity": "high",
        "item": None,
        "msg": f"Grand total Rs{expected:g} hona chahiye (sub total + tax), bill mein Rs{grand_total:g} likha hai. Rs{diff:g} zyada.",
        "loss": diff,
    }]


def check_rate_hike(conn, supplier_name, items):
    """Skips items with no rate history — nothing to compare a first sighting against."""
    issues = []
    for item in items:
        if item.get("rate") is None or item.get("qty") is None:
            continue
        history = db.get_rate_history(conn, supplier_name, item["name"])
        if not history:
            continue
        median = statistics.median(history)
        if median <= 0 or item["rate"] <= median * (1 + RATE_HIKE_PCT / 100):
            continue
        loss = round((item["rate"] - median) * item["qty"], 2)
        if loss < MIN_FLAG_AMOUNT:
            continue
        issues.append({
            "type": "RATE_HIKE",
            "severity": "medium",
            "item": item["name"],
            "msg": f"{item['name']} ka rate pehle Rs{median:g} tha, is baar Rs{item['rate']:g} hai — badh gaya hai.",
            "loss": loss,
        })
    return issues


def check_duplicate(conn, supplier_name, invoice_number, grand_total):
    prior_id = db.is_duplicate(conn, supplier_name, invoice_number)
    if not prior_id:
        return []
    return [{
        "type": "DUPLICATE",
        "severity": "high",
        "item": None,
        "msg": f"Invoice number {invoice_number} pehle bhi is supplier se aa chuka hai. Dobara payment mat karo.",
        "loss": grand_total or 0.0,
    }]


def check_low_confidence(confidence, unreadable_fields):
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        return [{
            "type": "LOW_CONFIDENCE",
            "severity": "warning",
            "item": None,
            "msg": "Photo saaf nahi thi, numbers galat ho sakte hain. Dobara, seedhi photo kheenchein.",
            "loss": 0.0,
        }]
    if unreadable_fields:
        fields = ", ".join(unreadable_fields)
        return [{
            "type": "LOW_CONFIDENCE",
            "severity": "warning",
            "item": None,
            "msg": f"Ye padh nahi paya: {fields}. Bill par khud check kar lein.",
            "loss": 0.0,
        }]
    return []


def check_invoice(conn, invoice, received_qty):
    """Run every rule against one extracted invoice. Returns all issues, unsorted."""
    items = invoice.get("items", [])
    issues = []
    issues += check_qty_short(items, received_qty)
    issues += check_qty_extra(items, received_qty)
    issues += check_line_math(items)
    issues += check_subtotal_math(items, invoice.get("sub_total"))
    issues += check_grandtotal_math(invoice.get("sub_total"), invoice.get("tax"), invoice.get("grand_total"))
    issues += check_rate_hike(conn, invoice.get("supplier_name"), items)
    issues += check_duplicate(conn, invoice.get("supplier_name"), invoice.get("invoice_number"), invoice.get("grand_total"))
    issues += check_low_confidence(invoice.get("confidence"), invoice.get("unreadable_fields"))
    return issues
