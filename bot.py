"""Telegram front end (Genie): read/summarize invoices and track per-vendor stock.
Long-polls the Telegram Bot API directly via requests — no new dependency.

Two menu paths:
1) Read & Summarize Invoice — photo or PDF in, plain summary out. No discrepancy
   checking (that flow, built around checker.py, has been retired from the bot).
2) Add Items to Stock — vendor name, then photo or typed list, then a confirmation
   before db.add_stock() runs. Stock is tracked per vendor (db.py Phase A).

Selling something is free-text at any time ("5 Maggi becha") — see Phase E.

All per-conversation state is keyed by (chat_id, sender_id), not chat_id alone —
in a group chat, multiple real people can talk to the bot, and chat_id alone would
merge them into one shared identity (person B's message finishing person A's
flow). chat_id is still what messages get sent to; sender_id is who's mid-flow.
"""

import base64
import html
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# Windows' console defaults to cp1252, which can't print emoji/Devanagari in our
# debug logs (crashed the bot mid-message with UnicodeEncodeError). Force UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import db
import extract

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API_ROOT = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_ROOT = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
PROVIDER = os.environ.get("BILLCHECK_PROVIDER", "gemini")

BOT_NAME = "Genie"
LOW_CONFIDENCE_THRESHOLD = 0.6  # same cutoff as checker.py's retired LOW_CONFIDENCE rule
WELCOME = (
    "Kya karna hai?\n\n"
    "1️⃣ Read & Summarize Invoice — bill ki photo ya PDF bhejo, summary milega "
    "(vendor, items, quantity, price, tax, total)\n\n"
    "2️⃣ Add Items to Stock — vendor se jo maal aaya wo apne stock mein jama karo, "
    "photo se ya khud type karke"
)

user_names = {}  # (chat_id, sender_id) -> name, once they've told us
awaiting_name = set()  # (chat_id, sender_id) currently expected to reply with their name
pending_photo = {}  # (chat_id, sender_id) -> downloaded file path, if one arrived before we had a name

CHAT_HISTORY_TURNS = 3  # past exchanges kept per person, so "isko"/"ye" resolve to what was just said
chat_history = {}  # (chat_id, sender_id) -> [{"role": "user"|"model", "text": str}, ...], oldest first

active_vendor = {}  # (chat_id, sender_id) -> vendor_name last talked about, so it doesn't need repeating

awaiting_stock_vendor = set()  # (chat_id, sender_id) chose "Add to Stock", waiting for vendor name
awaiting_stock_method = {}  # (chat_id, sender_id) -> vendor_name, waiting for photo-or-manual choice
awaiting_stock_photo = {}  # (chat_id, sender_id) -> vendor_name, waiting for the delivery photo
awaiting_stock_manual_text = {}  # (chat_id, sender_id) -> vendor_name, waiting for typed item list
pending_stock_confirmation = {}  # (chat_id, sender_id) -> {"vendor_name", "items"}, waiting yes/no
awaiting_photo_purpose = {}  # (chat_id, sender_id) -> (vendor_name, file_path), waiting "stock ya summary?"

BTN_SUMMARIZE = "1️⃣ Read & Summarize Invoice"
BTN_STOCK = "2️⃣ Add Items to Stock"
MAIN_MENU = {"inline_keyboard": [
    [{"text": BTN_SUMMARIZE, "callback_data": "summarize"}],
    [{"text": BTN_STOCK, "callback_data": "stock"}],
]}

BTN_STOCK_PHOTO = "📸 Add from Image"
BTN_STOCK_MANUAL = "✍️ Add Manually"
STOCK_METHOD_MENU = {"inline_keyboard": [
    [{"text": BTN_STOCK_PHOTO, "callback_data": "stock_photo"}],
    [{"text": BTN_STOCK_MANUAL, "callback_data": "stock_manual"}],
]}

BTN_YES = "✅ Haan, add karo"
BTN_NO = "❌ Nahi, cancel"
CONFIRM_MENU = {"inline_keyboard": [
    [{"text": BTN_YES, "callback_data": "confirm_yes"}],
    [{"text": BTN_NO, "callback_data": "confirm_no"}],
]}

BTN_PHOTO_FOR_STOCK = "📦 Stock mein add karo"
BTN_PHOTO_FOR_SUMMARY = "🧾 Sirf summary chahiye"
PHOTO_PURPOSE_MENU = {"inline_keyboard": [
    [{"text": BTN_PHOTO_FOR_STOCK, "callback_data": "photo_purpose_stock"}],
    [{"text": BTN_PHOTO_FOR_SUMMARY, "callback_data": "photo_purpose_summary"}],
]}

# Inline buttons attach to one message and never take over the keyboard area, so
# there's nothing to "remove" the way a ReplyKeyboardMarkup panel needs — that panel
# was the actual bug (stays open until the user manually taps back to their keyboard).
NO_KEYBOARD = None

ALL_BUTTON_TEXTS = {
    BTN_SUMMARIZE, BTN_STOCK, BTN_STOCK_PHOTO, BTN_STOCK_MANUAL, BTN_YES, BTN_NO,
    BTN_PHOTO_FOR_STOCK, BTN_PHOTO_FOR_SUMMARY,
}

YES_WORDS = {"haan", "ha", "han", "yes", "y", "ok", "okay", "theek hai", "kar do", "add karo"}
NO_WORDS = {"nahi", "nah", "no", "n", "cancel", "mat karo", "chhodo"}


def clear_stock_flow(ukey):
    """Drop any in-progress Add-to-Stock state for this person — used when they
    explicitly start a fresh flow via the main menu, so a stray/late tap of an old
    button doesn't silently overwrite progress (it resets it on purpose instead)."""
    awaiting_stock_vendor.discard(ukey)
    awaiting_stock_method.pop(ukey, None)
    awaiting_stock_photo.pop(ukey, None)
    awaiting_stock_manual_text.pop(ukey, None)
    pending_stock_confirmation.pop(ukey, None)
    awaiting_photo_purpose.pop(ukey, None)


STOCK_ENTRY_SYSTEM_PROMPT = """Ek dukaandaar type karke bata raha hai ki vendor se kaunse items \
aur kitni quantity mein aaye. Naam, quantity, aur unit (kg, litre, pcs, box, dozen, bag, etc.) \
chahiye — rate ki zarurat nahi. Isi JSON shape mein nikaalo, sirf JSON do, kuch aur text nahi:

{"items": [{"name": string, "qty": number, "unit": string or null}]}

Unit na bataya gaya ho to null rakho — mat maano "pcs" hai."""

FREE_TEXT_SYSTEM_PROMPT = """Tum Genie ho, ek Hinglish-bolne wala dukaan-stock-tracking bot. User \
ka message text ho sakta hai ya ek bola hua voice note — agar audio hai to pehle dhyaan se suno, \
Hinglish/Hindi mein jo bola gaya samjho, phir neeche wahi rules text ki tarah follow karo. Uske \
baad intent nikaalo, is JSON shape mein (sirf JSON do, kuch aur text nahi):

{"intent": "sale" | "restock" | "stock_query" | "undo" | "rename" | "history" | "chat", "items": \
[{"name": string, "qty": number or null, "unit": string or null, "all": boolean}], "vendor_name": \
string or null, "old_name": string or null, "new_name": string or null, "reply": string}

Is conversation ke pichle 1-2 messages bhi tumhe upar mil sakte hain. Agar user "isko", "ye", \
"wahi wala" jaisa kuch bole, pehle wahi context dekho ki pichle message mein kaunsa item/vendor \
zikar hua tha — khud se mat banao, agar context mein bhi na mile to null/khaali rakho.

- "sale": user ne bataya ki kuch becha/sold/nikal gaya hai — maal DUKAAN SE BAAHAR ja raha hai. \
Jaise "5 kg Sugar becha", "10 pcs soap nikal gaya". items mein wo bharo, unit agar bataya ho. \
vendor_name bharo agar isi message ya pichle context se pata chale, warna null. Agar user koi \
number nahi bata raha, sirf "sara/pura/poora/total/saara maal bik gaya" jaisa keh raha hai, to \
qty null hi rakho par us item ka "all": true kar do — number khud mat banao, hum current stock \
nikaal ke bech denge.
- "restock": user ne bataya ki kisi vendor se naya maal AAYA hai aur stock mein jama/add karna \
hai — maal DUKAAN MEIN AA raha hai. Jaise "Ramesh se 20 Maggi aaya", "iske paas Pizza hai isko \
stock me add karo", "naya maal aaya hai". items mein wo bharo. vendor_name bharo agar isi message \
ya pichle context se pata chale, warna null.
- "stock_query": user kisi vendor ka stock/hisaab pooch raha hai (jaise "Ayaz ka stock batao", \
"Ramesh se kya aaya hai") — tab vendor_name zaroor bharo. YA user saare vendors ki list maang \
raha hai (jaise "kaun kaun se vendor hai", "sab vendor batao", "koi vendor ka naam bata") — tab \
vendor_name null rakho, "chat" mat samjhna, ye bhi stock_query hai.
- "undo": user keh raha hai ki abhi jo pichli entry hui (sale ya restock) wo galat thi, use wapas \
karo. Jaise "galti ho gayi", "undo karo", "pichla wapas le lo", "cancel karo pichla wala". \
items/vendor_name ki zarurat nahi.
- "history": user delivery ka purana record poochh raha hai — kis din kya aaya (jaise "Karan ka \
history dikhao", "kab kya aaya", "delivery record batao", "pichle hafte kya aaya"). Ye sirf naya \
maal AANE (restock) ka record hai, sale ka nahi. vendor_name bharo agar specific vendor ho, warna \
null (sab vendors ka record).
- "rename": kisi item ka naam galat likha/bola gaya tha, use theek karna hai (typo, galat OCR, \
galat suna gaya). Jaise "Maggie ka naam Maggi kar do", "iska sahi naam XYZ hai", "naam galat hai, \
ise ABC bolo". old_name mein purana (galat) naam, new_name mein sahi naam bharo. vendor_name bharo \
agar isi message ya pichle context se pata chale, warna null.
- "chat": baaki sab (greeting, casual baat, sawaal jiska jawab tumhare data mein nahi hai). \
"reply" mein chhota (1-2 line) dostana Hinglish jawab do jaise ek dost deta hai. KABHI BHI koi \
vendor ka naam, item ka naam, ya stock number khud se mat banao — tumhe pata nahi ki user ke \
paas asal mein kaunse vendors/items hain, aur wo galat lag sakta hai."""

NOT_A_NAME = {
    "hi", "hii", "hiii", "hiiii", "hello", "hey", "hey lumo", "hii lumo",
    "namaste", "namaskar", "salam", "yo", "ok", "okay", "test", "hlo", "lumo",
}

QUESTION_WORDS = ("kya", "kaise", "kyu", "kyun", "kaun", "kab", "kahan", "help", "madad", "samajh")


def looks_like_question(text):
    """A stuck user asking 'what's happening?' shouldn't have that swallowed as
    their name/vendor name — reply to the question first, then re-prompt."""
    lowered = text.lower().strip()
    if "?" in text:
        return True
    words = lowered.split()
    return any(w in QUESTION_WORDS for w in words)


IST = timezone(timedelta(hours=5, minutes=30))


def time_greeting():
    """Railway's server clock runs in UTC, not IST — greeting by datetime.now()
    alone was ~5.5 hours off from what a shopkeeper in India was actually seeing
    (e.g. always "Good afternoon" well into their evening/night)."""
    hour = datetime.now(IST).hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _format_ist_date(utc_timestamp):
    """stock_movements.created_at is SQLite's CURRENT_TIMESTAMP — naive UTC.
    Converting to IST before showing a date matters for the same reason as
    time_greeting: a delivery logged at 2am IST is 8:30pm UTC the PREVIOUS
    day, so showing the raw UTC date would make it look like "yesterday"."""
    if not utc_timestamp:
        return None
    dt = datetime.strptime(utc_timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b")


def send_message(chat_id, text, parse_mode=None, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    preview = text.replace("\n", " ")[:150]
    resp = requests.post(f"{API_ROOT}/sendMessage", json=payload)
    if resp.status_code != 200:
        print(f"DEBUG send FAILED ({resp.status_code}) to {chat_id}: {resp.text[:300]} | tried to send: {preview}", flush=True)
    else:
        print(f"DEBUG sent to {chat_id}: {preview}", flush=True)


def answer_callback(callback_query_id):
    """Stops the tap's loading spinner on the user's inline button."""
    requests.post(f"{API_ROOT}/answerCallbackQuery", json={"callback_query_id": callback_query_id})


def format_summary_html(invoice):
    header = html.escape(invoice.get("supplier_name") or "Unknown Vendor")
    meta_bits = []
    if invoice.get("invoice_number"):
        meta_bits.append(f"Bill #{html.escape(str(invoice['invoice_number']))}")
    if invoice.get("invoice_date"):
        meta_bits.append(html.escape(str(invoice["invoice_date"])))

    lines = [f"🧾 <b>{header}</b>"]
    if meta_bits:
        lines.append(" · ".join(meta_bits))
    lines.append("")

    warning = confidence_warning(invoice)
    if warning:
        lines.append(warning)
        lines.append("")

    items = invoice.get("items", [])
    if items:
        name_width = min(max(len(item.get("name") or "?") for item in items), 18)
        rows = []
        for item in items:
            qty_str = fmt_qty(item["qty"], item.get("unit")) if item.get("qty") is not None else "-"
            rate_str = f"Rs{fmt_money(item['rate'])}" if item.get("rate") is not None else "-"
            amount_str = f"Rs{fmt_money(item['amount'])}" if item.get("amount") is not None else "-"
            rows.append(
                f"{_pad(item.get('name') or '?', name_width)}  "
                f"{qty_str.rjust(9)}  {rate_str.rjust(8)}  {amount_str.rjust(10)}"
            )
        table = html.escape("\n".join(rows))
        lines.append(f"<pre>{table}</pre>")
    else:
        lines.append("Koi item nahi mila.")

    lines.append("")
    lines.append("────────────")
    if invoice.get("sub_total") is not None:
        lines.append(f"Sub total: Rs{fmt_money(invoice['sub_total'])}")
    if invoice.get("tax") is not None:
        lines.append(f"Tax: Rs{fmt_money(invoice['tax'])}")
    if invoice.get("grand_total") is not None:
        lines.append(f"<b>Grand total: Rs{fmt_money(invoice['grand_total'])}</b>")
    return "\n".join(lines)


def confidence_warning(invoice):
    """Honest 'couldn't read this' beats a confidently wrong number (see CLAUDE.md).
    Returns a Hinglish caveat line, or None if the extraction looked trustworthy."""
    confidence = invoice.get("confidence")
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        return "⚠️ Photo saaf nahi thi, kuch numbers galat ho sakte hain. Bill se ek baar milaa lena."
    unreadable = invoice.get("unreadable_fields")
    if unreadable:
        fields = ", ".join(html.escape(str(f)) for f in unreadable)
        return f"⚠️ Ye padh nahi paya: {fields}. Bill par khud check kar lena."
    return None


def fmt_qty(qty, unit):
    return f"{qty:g} {unit}" if unit else f"{qty:g}"


def fmt_money(amount):
    """Thousands separator + trims a bare .00, so bills read like a bill
    (Rs3,072) instead of a raw float dump (Rs3072.00 or Rs3072.0)."""
    text = f"{amount:,.2f}"
    return text[:-3] if text.endswith(".00") else text


def format_stock_confirmation(vendor_name, items):
    lines = [f"<b>{html.escape(vendor_name)} se ye mila:</b>", ""]
    for item in items:
        line = f"• {html.escape(item['name'])} — {fmt_qty(item['qty'], item.get('unit'))}"
        if item.get("rate") is not None:
            line += f" — Rs{fmt_money(item['rate'])}"
        lines.append(line)
    lines.append("")
    lines.append("Stock mein add kar doon?")
    return "\n".join(lines)


def _classify_with_history(user_part, history_label, ukey):
    """Shared Gemini call behind interpret_free_text/interpret_voice — builds the
    contents list from this person's chat_history (so pronouns like "isko"/"ye"
    resolve against what was actually said, not just the current line alone),
    appends the new turn, then records the exchange back into chat_history.
    history_label is what gets stored as this turn's "user" text — the raw text
    itself for typed messages, a placeholder for voice notes (no separate
    transcript is kept, just Gemini's structured reply)."""
    key = os.environ.get("GEMINI_API_KEY")
    contents = [
        {"role": turn["role"], "parts": [{"text": turn["text"]}]}
        for turn in chat_history.get(ukey, [])
    ] if ukey is not None else []
    contents.append({"role": "user", "parts": [user_part]})

    resp = requests.post(
        f"{extract.GEMINI_API_ROOT}/models/{extract.DEFAULT_GEMINI_MODEL}:generateContent",
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": FREE_TEXT_SYSTEM_PROMPT}]},
            "contents": contents,
        },
    )
    resp.raise_for_status()
    reply_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    if ukey is not None:
        history = chat_history.setdefault(ukey, [])
        history.append({"role": "user", "text": history_label})
        history.append({"role": "model", "text": reply_text})
        del history[:-(CHAT_HISTORY_TURNS * 2)]

    return extract._parse_json_response(reply_text)


def interpret_free_text(text, ukey=None):
    """One Gemini call classifies + extracts: sale, restock, stock lookup, or plain
    chat — avoids a separate detect-then-reply pair of calls on every ordinary
    message."""
    return _classify_with_history({"text": text}, text, ukey)


def interpret_voice(file_path, mime_type, ukey=None):
    """Voice-note version of interpret_free_text — Gemini listens and classifies
    in the same call, no separate transcription step. Typing is real effort for a
    shopkeeper standing at the counter; speaking "5 Maggi becha" is lighter, and
    CLAUDE.md's whole point is that input must get lighter, never heavier."""
    with open(file_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
    part = {"inline_data": {"mime_type": mime_type, "data": audio_b64}}
    return _classify_with_history(part, "[voice message]", ukey)


def answer_question(text, ukey=None):
    """Natural-language answer for a question asked mid-flow (e.g. someone stuck
    at 'what's your name?' asks 'what does this do?' instead). Reuses the same
    classifier as the main fallback — its 'reply' field stands alone fine here."""
    try:
        result = interpret_free_text(text, ukey)
        return result.get("reply") or "Bas thoda sa detail chahiye, phir aage badhte hain."
    except Exception:
        return "Bas thoda sa detail chahiye, phir aage badhte hain."


def _pad(text, width):
    """Truncates/pads to a fixed character width — for <pre> column alignment.
    Padding on the raw (unescaped) name is correct even after html.escape later:
    Telegram decodes entities back to one rendered character before it draws
    the monospace grid, so the escaped wire text still lines up on screen."""
    text = str(text)
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


def format_stock_report(vendor_name, items):
    if not items:
        return f"📦 {html.escape(vendor_name)} ka koi stock record nahi hai mere paas."

    name_width = min(max(len(item["item_name"]) for item in items), 18)
    rows = []
    low_count = 0
    for item in items:
        qty = item["qty"]
        marker = ""
        if qty < 0:
            marker = " ⚠️"
            low_count += 1
        elif qty <= LOW_STOCK_THRESHOLD:
            marker = " 📉"
            low_count += 1
        row = f"{_pad(item['item_name'], name_width)}  {fmt_qty(qty, item.get('unit'))}{marker}"
        last_delivery = _format_ist_date(item.get("last_delivery"))
        if last_delivery:
            row += f"  · {last_delivery}"
        rows.append(row)

    summary = f"Total {len(items)} item"
    if low_count:
        summary += f" · {low_count} kam bacha hai"

    table = html.escape("\n".join(rows))
    return f"📦 <b>{html.escape(vendor_name)} ka stock</b>\n\n<pre>{table}</pre>\n{summary}"


def _same_vendor(a, b):
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def _format_grouped(entries):
    """entries: list of (vendor_or_None, item_line). A multi-item reply used to
    repeat "(Karan)" after every single line; this groups items under one
    "<b>Karan:</b>" header instead, printed once. Entries with vendor=None
    (item not found / ambiguous-vendor prompts) get no header, in their
    original position. Returns None if entries is empty."""
    if not entries:
        return None
    groups = []
    index_by_vendor = {}
    for vendor, line in entries:
        if vendor not in index_by_vendor:
            index_by_vendor[vendor] = len(groups)
            groups.append((vendor, []))
        groups[index_by_vendor[vendor]][1].append(line)

    out = []
    for vendor, item_lines in groups:
        if vendor:
            out.append(f"<b>{html.escape(vendor)}:</b>")
        out.extend(item_lines)
        out.append("")
    return "\n".join(out).rstrip()


REASON_LABELS = {"sale": "sale", "delivery": "stock add", "undo": "undo"}


def handle_undo(ukey):
    chat_id, owner_id = ukey
    conn = db.get_connection()
    results = db.undo_last_movement(conn, owner_id)
    if not results:
        send_message(chat_id, "Wapas lene ke liye koi entry mili nahi.")
        return

    reasons = {reason for *_, reason in results}
    label = REASON_LABELS.get(next(iter(reasons)), "entry") if len(reasons) == 1 else "entry"
    entries = []
    for vendor, item, unit, new_qty, _reason in results:
        active_vendor[ukey] = vendor
        entries.append((vendor, f"• <b>{html.escape(item)}</b>: {fmt_qty(new_qty, unit)}"))
    body = _format_grouped(entries)
    send_message(chat_id, f"Theek hai, pichla {label} wapas le liya:\n\n{body}", parse_mode="HTML")


def handle_rename(ukey, old_name, new_name, vendor_name):
    chat_id, owner_id = ukey
    vendor_name = vendor_name or active_vendor.get(ukey)
    if not vendor_name:
        send_message(chat_id, "Kaunse vendor ke stock mein naam badalna hai?")
        return
    if not old_name or not new_name:
        send_message(chat_id, 'Samajh nahi aaya kaunsa naam badalna hai. Jaise likho: "Maggie ka naam Maggi kar do"')
        return

    conn = db.get_connection()
    result = db.rename_item(conn, owner_id, vendor_name, old_name, new_name)
    if result is None:
        send_message(chat_id, f"{html.escape(vendor_name)} ke paas {html.escape(old_name)} mila nahi.")
        return
    vendor, old_canonical, final_name, unit, qty = result
    active_vendor[ukey] = vendor
    send_message(
        chat_id,
        f"Theek hai, <b>{html.escape(old_canonical)}</b> ka naam ab <b>{html.escape(final_name)}</b> hai — "
        f"{fmt_qty(qty, unit)} ({html.escape(vendor)})",
        parse_mode="HTML",
    )


def handle_stock_query(ukey, vendor_name):
    chat_id, owner_id = ukey
    conn = db.get_connection()
    if not vendor_name:
        vendors = db.get_vendors(conn, owner_id)
        if not vendors:
            send_message(chat_id, "Abhi koi vendor record nahi hai mere paas.")
        else:
            names = ", ".join(html.escape(v) for v in vendors)
            send_message(chat_id, f"Ye vendors hain: {names}. Kiska stock dekhna hai?")
        return
    active_vendor[ukey] = vendor_name
    items = db.get_stock_with_last_delivery(conn, owner_id, vendor_name)
    send_message(chat_id, format_stock_report(vendor_name, items), parse_mode="HTML")


def handle_history(ukey, vendor_name):
    """vendor_name=None means "across all vendors" (like stock_query's vendor
    list) — no active_vendor fallback here, since that would silently narrow
    an explicit "sab vendors ka record dikhao" down to just one."""
    chat_id, owner_id = ukey
    conn = db.get_connection()
    rows = db.get_delivery_history(conn, owner_id, vendor_name, limit=30)
    if not rows:
        send_message(chat_id, "Abhi koi delivery record nahi hai mere paas.")
        return
    if vendor_name:
        active_vendor[ukey] = vendor_name

    by_date = {}
    for r in rows:
        label = _format_ist_date(r["created_at"]) or "?"
        line = f"• {html.escape(r['item_name'])} — {fmt_qty(r['qty'], r['unit'])}"
        if not vendor_name:  # scoped to one vendor already says who; across all, say each time
            line += f" ({html.escape(r['vendor_name'])})"
        by_date.setdefault(label, []).append(line)

    out = []
    for label, item_lines in by_date.items():
        out.append(f"<b>{label}:</b>")
        out.extend(item_lines)
        out.append("")
    send_message(chat_id, "\n".join(out).rstrip(), parse_mode="HTML")


LOW_STOCK_THRESHOLD = 5  # heads-up once stock drops to/below this, so a shortage doesn't go unnoticed


def _sale_line(matched_item, new_qty, matched_unit, qty_sold):
    line = f"• {html.escape(matched_item)}: {fmt_qty(new_qty, matched_unit)} bacha"
    qty_before = new_qty + qty_sold
    if new_qty < 0:
        line += "\n   ⚠️ Itna stock tha hi nahi, phir bhi kaat diya — ek baar check kar lena."
    elif new_qty <= LOW_STOCK_THRESHOLD < qty_before:
        # only fires the sale that crosses the line, not every sale after — otherwise
        # every subsequent sale of an already-low item would repeat the same nudge
        line += f"\n   📉 Kam bacha hai ({fmt_qty(new_qty, matched_unit)}), mangwa lena."
    return line


def handle_sale(ukey, items, vendor_name):
    chat_id, owner_id = ukey
    if not items:
        send_message(chat_id, "Samajh nahi aaya kya becha. Phir se batao?")
        return

    conn = db.get_connection()
    batch_id = db.new_batch_id()  # all items from this one message undo together
    entries = []  # (vendor_or_None, line) — grouped under one vendor header at send time
    missing_qty = []
    for item in items:
        name, qty, unit = item.get("name"), item.get("qty"), item.get("unit")
        sell_all = bool(item.get("all"))
        if not name:
            continue
        if not qty and not sell_all:
            missing_qty.append(name)
            continue

        if vendor_name:
            if sell_all:
                on_hand = db.get_item_qty(conn, owner_id, vendor_name, name)
                if on_hand is None:
                    entries.append((None, f"• {html.escape(name)}: {html.escape(vendor_name)} ke paas ye stock mein nahi mila."))
                    continue
                if on_hand <= 0:
                    entries.append((vendor_name, f"• {html.escape(name)}: pehle se hi 0 hai, bechne ko kuch nahi bacha."))
                    continue
                qty = on_hand
            result = db.record_sale(conn, owner_id, vendor_name, name, qty, unit, batch_id)
            if result is None:
                entries.append((None, f"• {html.escape(name)}: {html.escape(vendor_name)} ke paas ye stock mein nahi mila."))
            else:
                vendor, matched_item, matched_unit, new_qty = result
                active_vendor[ukey] = vendor
                entries.append((vendor, _sale_line(matched_item, new_qty, matched_unit, qty)))
            continue

        matches = db.find_item_across_vendors(conn, owner_id, name)
        current = active_vendor.get(ukey)
        active_match = next((m for m in matches if _same_vendor(m["vendor_name"], current)), None)

        if not matches:
            entries.append((None, f"• {html.escape(name)}: ye stock mein nahi mila."))
        elif len(matches) == 1 or active_match:
            m = active_match or matches[0]
            actual_qty = m["qty"] if sell_all else qty
            if actual_qty <= 0:
                entries.append((m["vendor_name"], f"• {html.escape(name)}: pehle se hi 0 hai, bechne ko kuch nahi bacha."))
            else:
                vendor, matched_item, matched_unit, new_qty = db.record_sale(conn, owner_id, m["vendor_name"], m["item_name"], actual_qty, unit, batch_id)
                active_vendor[ukey] = vendor
                entries.append((vendor, _sale_line(matched_item, new_qty, matched_unit, actual_qty)))
        else:
            vendors = " / ".join(html.escape(m["vendor_name"]) for m in matches)
            example = matches[0]["vendor_name"]
            entries.append((None,
                f"• {html.escape(name)}: {vendors} — dono ke paas hai, kis ka becha? "
                f"Jaise likho: \"{example} ka {html.escape(name)} becha\""
            ))

    parts = []
    body = _format_grouped(entries)
    if body:
        parts.append(body)
    if missing_qty:
        names = ", ".join(html.escape(n) for n in missing_qty)
        parts.append(f"⚠️ {names} — kitna becha nahi bataya, quantity ke saath phir se batao.")

    send_message(chat_id, "\n\n".join(parts) if parts else "Kuch update nahi hua.", parse_mode="HTML")


def handle_restock(ukey, items, vendor_name):
    """Free-text version of the Add-to-Stock flow (db.add_stock, not record_sale) —
    "iske paas Maggi aaya, stock me add karo" was previously misread as a sale and
    silently decremented stock instead of adding it."""
    chat_id, owner_id = ukey
    if not items:
        send_message(chat_id, "Samajh nahi aaya kya aaya. Phir se batao?")
        return
    vendor_name = vendor_name or active_vendor.get(ukey)
    if not vendor_name:
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.")
        return

    conn = db.get_connection()
    batch_id = db.new_batch_id()  # all items from this one message undo together
    lines = []
    missing_qty = []
    resolved_vendor = None
    for item in items:
        name, qty, unit = item.get("name"), item.get("qty"), item.get("unit")
        if not name:
            continue
        if not qty:
            missing_qty.append(name)
            continue
        vendor, matched_item, matched_unit, new_qty = db.add_stock(conn, owner_id, vendor_name, name, qty, unit, batch_id)
        active_vendor[ukey] = vendor
        resolved_vendor = vendor
        lines.append(f"• {html.escape(matched_item)}: {fmt_qty(new_qty, matched_unit)}")

    parts = []
    if lines:
        parts.append(f"<b>{html.escape(resolved_vendor)}:</b>\n" + "\n".join(lines))
    if missing_qty:
        names = ", ".join(html.escape(n) for n in missing_qty)
        parts.append(f"⚠️ {names} — kitna aaya nahi bataya, quantity ke saath phir se batao.")

    send_message(chat_id, "\n\n".join(parts) if parts else "Kuch update nahi hua.", parse_mode="HTML")


def extract_stock_items(text):
    key = os.environ.get("GEMINI_API_KEY")
    resp = requests.post(
        f"{extract.GEMINI_API_ROOT}/models/{extract.DEFAULT_GEMINI_MODEL}:generateContent",
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": STOCK_ENTRY_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": text}]}],
        },
    )
    resp.raise_for_status()
    parsed = extract._parse_json_response(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
    return parsed.get("items", [])


def download_file(file_id, dest_path):
    file_info = requests.get(f"{API_ROOT}/getFile", params={"file_id": file_id}).json()["result"]
    data = requests.get(f"{FILE_ROOT}/{file_info['file_path']}").content
    with open(dest_path, "wb") as f:
        f.write(data)


def save_incoming_photo(message):
    os.makedirs("bot_uploads", exist_ok=True)
    file_id = message["photo"][-1]["file_id"]
    path = f"bot_uploads/{file_id}.jpg"
    download_file(file_id, path)
    return path


def save_incoming_document(message):
    doc = message.get("document", {})
    if doc.get("mime_type") != "application/pdf":
        return None
    os.makedirs("bot_uploads", exist_ok=True)
    file_id = doc["file_id"]
    path = f"bot_uploads/{file_id}.pdf"
    download_file(file_id, path)
    return path


def save_incoming_voice(message):
    """Telegram voice notes come as OGG/Opus — Gemini accepts that mime type
    directly as inline_data, no local conversion needed."""
    voice = message["voice"]
    os.makedirs("bot_uploads", exist_ok=True)
    file_id = voice["file_id"]
    path = f"bot_uploads/{file_id}.ogg"
    download_file(file_id, path)
    return path, voice.get("mime_type") or "audio/ogg"


def process_summarize(ukey, file_path):
    chat_id = ukey[0]
    send_message(chat_id, f"{user_names[ukey]}, padh raha hoon...")
    try:
        invoice = extract.extract(file_path, provider=PROVIDER)
        conn = db.get_connection()
        db.save_invoice(
            conn, invoice.get("supplier_name"), invoice.get("invoice_number"),
            invoice.get("invoice_date"), invoice.get("grand_total"),
            invoice.get("items", []), [],
        )
        send_message(chat_id, format_summary_html(invoice), parse_mode="HTML")
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Padhne mein dikkat aayi, dobara try karo.")


def process_stock_photo(ukey, vendor_name, file_path):
    """Returns True on success. False means the caller should let the user retry
    (keep them in the same waiting-for-photo state) instead of dropping them back
    to the main menu with no way to continue without starting the flow over."""
    chat_id = ukey[0]
    send_message(chat_id, f"{user_names[ukey]}, photo padh raha hoon...")
    try:
        invoice = extract.extract(file_path, provider=PROVIDER)
        items = [
            {"name": i["name"], "qty": i["qty"], "unit": i.get("unit"), "rate": i.get("rate")}
            for i in invoice.get("items", []) if i.get("qty")
        ]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi is photo mein. Dusri photo try karo.")
            return False
        pending_stock_confirmation[ukey] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Padhne mein dikkat aayi. Dusri photo try karo.")
        return False


def process_stock_manual(ukey, vendor_name, text):
    """Returns True on success, False if the caller should let the user retry
    typing (same reasoning as process_stock_photo)."""
    chat_id = ukey[0]
    send_message(chat_id, f"{user_names[ukey]}, samajh raha hoon...")
    try:
        items = [
            {"name": i["name"], "qty": i["qty"], "unit": i.get("unit")}
            for i in extract_stock_items(text) if i.get("qty")
        ]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi. Phir se batao, jaise: \"Biscuit 20 pcs\"")
            return False
        pending_stock_confirmation[ukey] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Samajh nahi paya. Phir se try karo.")
        return False


def confirm_stock_addition(ukey):
    chat_id, owner_id = ukey
    data = pending_stock_confirmation.pop(ukey)
    conn = db.get_connection()
    batch_id = db.new_batch_id()  # all items from this one confirmation undo together
    lines = []
    resolved_vendor = None
    for item in data["items"]:
        vendor, name, unit, new_qty = db.add_stock(conn, owner_id, data["vendor_name"], item["name"], item["qty"], item.get("unit"), batch_id)
        active_vendor[ukey] = vendor
        resolved_vendor = vendor
        line = f"• {html.escape(name)}: {fmt_qty(new_qty, unit)}"
        if item.get("rate") is not None:
            line += f" (Rs{fmt_money(item['rate'])}/each)"
        lines.append(line)
    header = f"<b>{html.escape(resolved_vendor)} — stock update ho gaya:</b>"
    send_message(chat_id, header + "\n\n" + "\n".join(lines), parse_mode="HTML")


def handle_callback_query(cq):
    """Inline-button taps. Mirrors the equivalent text == BTN_X branches in
    handle_update, but keyed off callback_data instead of typed text."""
    answer_callback(cq["id"])
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    sender_id = cq.get("from", {}).get("id", chat_id)  # cq["from"] is the tapper, not the bot
    ukey = (chat_id, sender_id)
    data = cq.get("data", "")
    print(f"DEBUG callback: ukey={ukey} data={data!r}", flush=True)
    if not chat_id or ukey not in user_names:
        return  # menus only ever shown after onboarding

    if data == "summarize":
        clear_stock_flow(ukey)
        send_message(chat_id, "Theek hai, bill ki photo ya PDF bhej do.")

    elif data == "stock":
        clear_stock_flow(ukey)
        awaiting_stock_vendor.add(ukey)
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.")

    elif data == "stock_photo" and ukey in awaiting_stock_method:
        vendor_name = awaiting_stock_method.pop(ukey)
        awaiting_stock_photo[ukey] = vendor_name
        send_message(chat_id, "Theek hai, photo bhej do.")

    elif data == "stock_manual" and ukey in awaiting_stock_method:
        vendor_name = awaiting_stock_method.pop(ukey)
        awaiting_stock_manual_text[ukey] = vendor_name
        send_message(chat_id, 'Batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"')

    elif data == "confirm_yes" and ukey in pending_stock_confirmation:
        confirm_stock_addition(ukey)

    elif data == "confirm_no" and ukey in pending_stock_confirmation:
        pending_stock_confirmation.pop(ukey, None)
        send_message(chat_id, "Theek hai, cancel kar diya.")

    elif data == "photo_purpose_stock" and ukey in awaiting_photo_purpose:
        vendor_name, file_path = awaiting_photo_purpose.pop(ukey)
        process_stock_photo(ukey, vendor_name, file_path)

    elif data == "photo_purpose_summary" and ukey in awaiting_photo_purpose:
        _, file_path = awaiting_photo_purpose.pop(ukey)
        process_summarize(ukey, file_path)


def handle_update(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    sender_id = message.get("from", {}).get("id", chat_id)
    ukey = (chat_id, sender_id)
    print(f"DEBUG update: ukey={ukey} keys={list(message.keys())} text={message.get('text')!r}", flush=True)
    if not chat_id:
        return
    if "text" not in message and "photo" not in message and "document" not in message and "voice" not in message:
        return  # ignore group system messages: joins, leaves, pins, etc.

    # New sender in this chat: check for a saved name first (survives restarts),
    # then greet by time of day and ask for one before doing anything else.
    if ukey not in user_names:
        conn = db.get_connection()
        saved_name = db.get_user_name(conn, sender_id)
        if saved_name:
            user_names[ukey] = saved_name
            # fall through — handled like any other message below, no re-onboarding
        elif ukey in awaiting_name and "text" in message:
            name = message["text"].strip()
            if looks_like_question(name):
                send_message(chat_id, answer_question(name, ukey))
                send_message(chat_id, "Ab apna naam bata do?")
                return
            if name.lower() in NOT_A_NAME or name in ALL_BUTTON_TEXTS:
                send_message(chat_id, "Wo naam nahi laga 😅 Bas apna naam likho, jaise: Ramesh")
                return
            user_names[ukey] = name
            db.set_user_name(conn, sender_id, name)
            awaiting_name.discard(ukey)
            # One-time cleanup: clears any old-style reply keyboard still showing from
            # before this bot switched to inline buttons. Can't combine remove_keyboard
            # and inline_keyboard in the same message, so this takes two sends.
            send_message(chat_id, "🔔", reply_markup={"remove_keyboard": True})
            send_message(chat_id, f"Dhanyawad, {name}! {WELCOME}", reply_markup=MAIN_MENU)
            if ukey in pending_photo:
                process_summarize(ukey, pending_photo.pop(ukey))
            return
        else:
            awaiting_name.add(ukey)
            if "photo" in message:
                pending_photo[ukey] = save_incoming_photo(message)
            elif "document" in message:
                path = save_incoming_document(message)
                if path:
                    pending_photo[ukey] = path
            send_message(chat_id, f"{time_greeting()}! Main {BOT_NAME} hoon. Pehle apna naam bata do?")
            return

    # Photo / PDF: which flow is active decides what happens to it.
    if "photo" in message or "document" in message:
        file_path = save_incoming_photo(message) if "photo" in message else save_incoming_document(message)
        if not file_path:
            send_message(chat_id, "Ye file PDF ya image nahi lagi.")
            return
        if ukey in awaiting_stock_photo:
            vendor_name = awaiting_stock_photo[ukey]
            if process_stock_photo(ukey, vendor_name, file_path):
                awaiting_stock_photo.pop(ukey, None)
        elif active_vendor.get(ukey):
            # An unprompted photo with a vendor already "in focus" (from earlier
            # free-text stock chat) is genuinely ambiguous — silently defaulting
            # to Read & Summarize meant a photo meant for stock never got added,
            # with no clue why. Ask once instead of guessing either way.
            awaiting_photo_purpose[ukey] = (active_vendor[ukey], file_path)
            send_message(
                chat_id,
                f"Ye photo <b>{html.escape(active_vendor[ukey])}</b> ke stock mein add karni hai, "
                f"ya sirf padh ke summary chahiye?",
                parse_mode="HTML", reply_markup=PHOTO_PURPOSE_MENU,
            )
        else:
            process_summarize(ukey, file_path)
        return

    # Voice note: only wired into the free-text path (sale/restock/stock_query/
    # undo/chat) — not into the structured Add-to-Stock button flow's typed
    # prompts (vendor name, item list), which still need text for now.
    if "voice" in message:
        file_path, mime_type = save_incoming_voice(message)
        try:
            result = interpret_voice(file_path, mime_type, ukey)
        except Exception:
            import traceback
            traceback.print_exc()
            send_message(chat_id, f"{user_names[ukey]}, awaaz samajh nahi aayi. Phir se bolo ya type kar do.")
            return
        dispatch_intent(ukey, result)
        return

    text = message["text"].strip()

    if text == BTN_SUMMARIZE:
        clear_stock_flow(ukey)
        send_message(chat_id, "Theek hai, bill ki photo ya PDF bhej do.", reply_markup=NO_KEYBOARD)

    elif text == BTN_STOCK:
        clear_stock_flow(ukey)
        awaiting_stock_vendor.add(ukey)
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.", reply_markup=NO_KEYBOARD)

    elif ukey in awaiting_stock_vendor and text in ALL_BUTTON_TEXTS:
        send_message(chat_id, "Vendor ka naam likho (button nahi), jaise: Ayaz")

    elif ukey in awaiting_stock_vendor and looks_like_question(text):
        send_message(chat_id, answer_question(text, ukey))
        send_message(chat_id, "Ab batao, kaunse vendor se maal aaya?")

    elif ukey in awaiting_stock_vendor:
        awaiting_stock_vendor.discard(ukey)
        awaiting_stock_method[ukey] = text
        send_message(chat_id, "Photo bhejoge ya khud type karoge?", reply_markup=STOCK_METHOD_MENU)

    elif ukey in awaiting_stock_method and text == BTN_STOCK_PHOTO:
        vendor_name = awaiting_stock_method.pop(ukey)
        awaiting_stock_photo[ukey] = vendor_name
        send_message(chat_id, "Theek hai, photo bhej do.", reply_markup=NO_KEYBOARD)

    elif ukey in awaiting_stock_method and text == BTN_STOCK_MANUAL:
        vendor_name = awaiting_stock_method.pop(ukey)
        awaiting_stock_manual_text[ukey] = vendor_name
        send_message(chat_id, 'Batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"', reply_markup=NO_KEYBOARD)

    elif ukey in awaiting_stock_manual_text and looks_like_question(text):
        send_message(chat_id, answer_question(text, ukey))
        send_message(chat_id, 'Ab batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"')

    elif ukey in awaiting_stock_manual_text:
        vendor_name = awaiting_stock_manual_text[ukey]
        if process_stock_manual(ukey, vendor_name, text):
            awaiting_stock_manual_text.pop(ukey, None)

    elif ukey in pending_stock_confirmation and (text == BTN_YES or text.lower() in YES_WORDS):
        confirm_stock_addition(ukey)

    elif ukey in pending_stock_confirmation and (text == BTN_NO or text.lower() in NO_WORDS):
        pending_stock_confirmation.pop(ukey)
        send_message(chat_id, "Theek hai, cancel kar diya.")

    elif ukey in pending_stock_confirmation:
        send_message(chat_id, "Haan ya nahi bata do — stock mein add karna hai?", reply_markup=CONFIRM_MENU)

    elif ukey in awaiting_photo_purpose and (text == BTN_PHOTO_FOR_STOCK or "stock" in text.lower()):
        vendor_name, file_path = awaiting_photo_purpose.pop(ukey)
        process_stock_photo(ukey, vendor_name, file_path)

    elif ukey in awaiting_photo_purpose and (text == BTN_PHOTO_FOR_SUMMARY or "summar" in text.lower()):
        _, file_path = awaiting_photo_purpose.pop(ukey)
        process_summarize(ukey, file_path)

    elif ukey in awaiting_photo_purpose:
        send_message(chat_id, "Stock mein add karna hai ya sirf summary chahiye?", reply_markup=PHOTO_PURPOSE_MENU)

    else:
        try:
            result = interpret_free_text(text, ukey)
        except Exception:
            import traceback
            traceback.print_exc()
            send_message(chat_id, f"{user_names[ukey]}, {WELCOME}", reply_markup=MAIN_MENU)
            return
        dispatch_intent(ukey, result)


def dispatch_intent(ukey, result):
    """Routes a classified result (from typed text or a voice note — same JSON
    shape either way) to the right handler. Shared so voice messages get exactly
    the same sale/restock/stock_query/undo/chat behavior as typed ones."""
    chat_id = ukey[0]
    intent = result.get("intent")
    if intent == "stock_query":
        handle_stock_query(ukey, result.get("vendor_name"))
    elif intent == "sale":
        handle_sale(ukey, result.get("items", []), result.get("vendor_name"))
    elif intent == "restock":
        handle_restock(ukey, result.get("items", []), result.get("vendor_name"))
    elif intent == "undo":
        handle_undo(ukey)
    elif intent == "rename":
        handle_rename(ukey, result.get("old_name"), result.get("new_name"), result.get("vendor_name"))
    elif intent == "history":
        handle_history(ukey, result.get("vendor_name"))
    else:
        reply = result.get("reply")
        send_message(chat_id, reply or WELCOME, reply_markup=None if reply else MAIN_MENU)


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN not set. export TELEGRAM_BOT_TOKEN=...")
    print("Bot chalu hai. Ctrl+C se roko.", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            updates = requests.get(f"{API_ROOT}/getUpdates", params=params, timeout=35).json()["result"]
            for update in updates:
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback_query(update["callback_query"])
                else:
                    handle_update(update)
        except requests.exceptions.RequestException as e:
            print(f"Network hiccup, retrying in 5s: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
