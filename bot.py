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
import http.server
import json
import os
import re
import sys
import threading
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

# Railway sets this once a public domain exists for the service (see the "List
# edit karo" WebApp button below) — a Telegram web_app URL must be HTTPS, so
# without a domain configured (e.g. running locally) that button is just
# skipped rather than sent broken.
PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
WEBAPP_PORT = int(os.environ.get("PORT", "8080"))


def build_edit_webapp_url(vendor_name, items):
    """A stock-add confirmation's items, packed into the URL of a small HTML
    form (served by _WebAppHandler below) that Telegram opens in place — the
    form reads this same payload back out of its own URL, so no server-side
    session/lookup is needed. None if there's no public HTTPS domain to use."""
    if not PUBLIC_DOMAIN:
        return None
    payload = {
        "vendor": vendor_name,
        "items": [{"name": i["name"], "qty": i["qty"], "unit": i.get("unit")} for i in items],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"https://{PUBLIC_DOMAIN}/edit?d={encoded}"


BOT_NAME = "Genie"
LOW_CONFIDENCE_THRESHOLD = 0.6  # same cutoff as checker.py's retired LOW_CONFIDENCE rule
WELCOME = (
    "Chaliye batao — aaj kya karna hai?\n\n"
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
pending_delete_confirmation = {}  # (chat_id, sender_id) -> {"target": "item"|"vendor", "vendor_name", "item_name"}, waiting yes/no
awaiting_item_edit = {}  # (chat_id, sender_id) -> index into pending_stock_confirmation[ukey]["items"] — group-chat fallback only (see build_confirmation_menu)

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

BTN_YES = "✅ Sab sahi hai"
BTN_NO = "❌ Nahi, cancel"


def build_confirmation_menu(ukey, vendor_name, items):
    """Confirm/cancel row, plus an edit affordance:
    - Private chat: a WebApp edit button (see build_edit_webapp_url) — a real
      editable form instead of tapping through chat.
    - Group chat: Telegram rejects web_app buttons there outright
      (BUTTON_TYPE_INVALID — got a group stuck retrying the same error before
      this split existed), so groups get one '✏️ <item>' button per item
      instead, each opening the old tap-then-type-in-chat flow."""
    chat_id = ukey[0]
    keyboard = []
    if chat_id > 0:
        edit_url = build_edit_webapp_url(vendor_name, items)
        if edit_url:
            keyboard.append([{"text": "✏️ List edit karo", "web_app": {"url": edit_url}}])
    else:
        keyboard.extend([{"text": f"✏️ {item['name']}", "callback_data": f"edit_item:{i}"}] for i, item in enumerate(items))
    keyboard.append([{"text": BTN_YES, "callback_data": "confirm_yes"}])
    keyboard.append([{"text": BTN_NO, "callback_data": "confirm_no"}])
    return {"inline_keyboard": keyboard}

BTN_DELETE_YES = "🗑️ Haan, hata do"
BTN_DELETE_NO = "❌ Nahi, rehne do"
DELETE_CONFIRM_MENU = {"inline_keyboard": [
    [{"text": BTN_DELETE_YES, "callback_data": "delete_confirm_yes"}],
    [{"text": BTN_DELETE_NO, "callback_data": "delete_confirm_no"}],
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
    BTN_PHOTO_FOR_STOCK, BTN_PHOTO_FOR_SUMMARY, BTN_DELETE_YES, BTN_DELETE_NO,
}

YES_WORDS = {"haan", "ha", "han", "yes", "y", "ok", "okay", "theek hai", "kar do", "add karo", "sab sahi hai"}
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
    awaiting_item_edit.pop(ukey, None)
    awaiting_photo_purpose.pop(ukey, None)
    pending_delete_confirmation.pop(ukey, None)


STOCK_ENTRY_SYSTEM_PROMPT = """Ek dukaandaar type karke bata raha hai ki vendor se kaunse items \
aur kitni quantity mein aaye. Naam, quantity, aur unit (kg, litre, pcs, box, dozen, bag, etc.) \
chahiye — rate ki zarurat nahi. Isi JSON shape mein nikaalo, sirf JSON do, kuch aur text nahi:

{"items": [{"name": string, "qty": number, "unit": string or null}]}

Unit na bataya gaya ho to null rakho — mat maano "pcs" hai."""

FREE_TEXT_SYSTEM_PROMPT = """Tum Genie ho, ek Hinglish-bolne wala dukaan-stock-tracking bot. User \
ka message text ho sakta hai ya ek bola hua voice note — agar audio hai to pehle dhyaan se suno, \
Hinglish/Hindi mein jo bola gaya samjho, phir neeche wahi rules text ki tarah follow karo. Uske \
baad intent nikaalo, is JSON shape mein (sirf JSON do, kuch aur text nahi):

{"intent": "sale" | "restock" | "stock_query" | "undo" | "rename" | "history" | "delete" | "chat", \
"items": [{"name": string, "qty": number or null, "unit": string or null, "all": boolean}], \
"vendor_name": string or null, "old_name": string or null, "new_name": string or null, \
"rename_target": "item" | "vendor" | null, "delete_target": "item" | "vendor" | null, "reply": string}

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
- "rename": koi purana naam galat likha/bola/suna gaya tha (typo, galat OCR), use theek karna hai \
— ya to kisi ITEM ka naam, ya kisi VENDOR ka naam. Jaise "Maggie ka naam Maggi kar do" (item), \
"Ramesh ka naam Ramesh Traders kar do" (vendor). old_name mein purana (galat) naam, new_name mein \
sahi naam bharo. rename_target mein "item" ya "vendor" bharo — sirf tab jab message ya pichle \
context se saaf pata chale in dono mein se kya hai (jaise "vendor ka naam" bola, ya old_name kisi \
vendor list mein pehle se zikar ho chuka hai, to "vendor"; kisi vendor ke andar ek product jaisa \
lage to "item"). Agar clear na ho, rename_target null rakho — khud mat chuno. rename_target \
"item" ho to vendor_name bharo agar isi message ya pichle context se pata chale (kis vendor ke \
stock mein ye item hai), warna null; rename_target "vendor" ho to vendor_name ki zarurat nahi.
- "delete": user kisi item ko vendor ke stock se, ya poore vendor ko hi, hamesha ke liye list se \
hata dena chahta hai — "sale" se alag hai (sale sirf becha hua maal ghatati hai, item list mein \
rehta hai; delete record hi mita deta hai). Jaise "Maggi ko list se hata do", "isko delete kar \
do", "Ramesh vendor ko hata do", "ye vendor nikaal do". delete_target mein "item" ya "vendor" \
bharo — sirf tab jab message ya pichle context se saaf pata chale in dono mein se kya hatana hai. \
Agar sirf "hata do"/"delete karo" bola aur item ya vendor ka koi zikar nahi (na isi message mein, \
na context mein), to delete_target null rakho — khud mat chuno. Jo naam bataya gaya wo item ho to \
items[0].name mein, vendor ho to vendor_name mein bharo.
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
    """Returns the sent message's message_id (so a caller can later edit that
    exact message in place instead of sending a new one), or None on failure."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    preview = text.replace("\n", " ")[:150]
    resp = requests.post(f"{API_ROOT}/sendMessage", json=payload)
    if resp.status_code != 200:
        print(f"DEBUG send FAILED ({resp.status_code}) to {chat_id}: {resp.text[:300]} | tried to send: {preview}", flush=True)
        return None
    print(f"DEBUG sent to {chat_id}: {preview}", flush=True)
    return resp.json().get("result", {}).get("message_id")


def edit_message(chat_id, message_id, text, parse_mode=None, reply_markup=None):
    """Rewrites an already-sent message in place — used to keep the stock-add
    confirmation (and its per-item edits) as one evolving message instead of a
    new bubble at every step. reply_markup=None keeps the current keyboard;
    pass {"inline_keyboard": []} to clear it."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    preview = text.replace("\n", " ")[:150]
    resp = requests.post(f"{API_ROOT}/editMessageText", json=payload)
    if resp.status_code != 200:
        print(f"DEBUG edit FAILED ({resp.status_code}) to {chat_id}/{message_id}: {resp.text[:300]} | tried: {preview}", flush=True)
        return False
    print(f"DEBUG edited {chat_id}/{message_id}: {preview}", flush=True)
    return True


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
    lines.append("Sab sahi hai to confirm karo, ya kisi item ka naam tap karke usse edit karo.")
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


def format_stock_report(conn, owner_id, vendor_name, items):
    # A 0-qty item (fully sold out, nothing wrong) just clutters the list —
    # drop it from what's shown. Negative qty stays: that's an anomaly (sold
    # more than was on record) worth flagging, not a normal empty item.
    items = [item for item in items if item["qty"] != 0]
    if not items:
        return f"📦 {html.escape(vendor_name)} ka koi stock record nahi hai mere paas."

    name_width = min(max(len(item["item_name"]) for item in items), 18)
    rows = []
    forecasts = []  # low items with a trustworthy days-left estimate, shown below the table
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
            days_left = db.estimate_days_left(conn, owner_id, vendor_name, item["item_name"], qty)
            if days_left is not None:
                forecasts.append(f"• {html.escape(item['item_name'])}: {_format_days_left(days_left)} khatam ho sakta hai")
        row = f"{_pad(item['item_name'], name_width)}  {fmt_qty(qty, item.get('unit'))}{marker}"
        last_delivery = _format_ist_date(item.get("last_delivery"))
        if last_delivery:
            row += f"  · {last_delivery}"
        rows.append(row)

    summary = f"Total {len(items)} item"
    if low_count:
        summary += f" · {low_count} kam bacha hai"

    table = html.escape("\n".join(rows))
    out = f"📦 <b>{html.escape(vendor_name)} ka stock</b>\n\n<pre>{table}</pre>\n{summary}"
    if forecasts:
        out += "\n\n🔮 <b>Andaza (bikri ki raftaar se):</b>\n" + "\n".join(forecasts)
    return out


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


def handle_rename(ukey, rename_target, old_name, new_name, vendor_name):
    """Dispatches to the item- or vendor-level rename, or asks which one when
    the model couldn't tell (rename_target is None) — same "missing data over
    invented data" reasoning as handle_delete."""
    chat_id, owner_id = ukey
    if not old_name or not new_name:
        send_message(chat_id, 'Samajh nahi aaya kaunsa naam badalna hai. Jaise likho: "Maggie ka naam Maggi kar do"')
        return
    if rename_target not in ("item", "vendor"):
        send_message(chat_id, "Item ka naam badalna hai ya poore vendor ka? Bata do.")
        return
    if rename_target == "vendor":
        handle_rename_vendor(ukey, old_name, new_name)
    else:
        handle_rename_item(ukey, old_name, new_name, vendor_name)


def handle_rename_item(ukey, old_name, new_name, vendor_name):
    chat_id, owner_id = ukey
    vendor_name = vendor_name or active_vendor.get(ukey)
    if not vendor_name:
        send_message(chat_id, "Kaunse vendor ke stock mein naam badalna hai?")
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


def handle_rename_vendor(ukey, old_name, new_name):
    chat_id, owner_id = ukey
    conn = db.get_connection()
    result = db.rename_vendor(conn, owner_id, old_name, new_name)
    if result is None:
        send_message(chat_id, f"{html.escape(old_name)} naam ka koi vendor mila nahi.")
        return
    old_canonical, final_name = result
    active_vendor[ukey] = final_name
    send_message(
        chat_id,
        f"Theek hai, <b>{html.escape(old_canonical)}</b> ka naam ab <b>{html.escape(final_name)}</b> hai.",
        parse_mode="HTML",
    )


def handle_delete(ukey, delete_target, item_name, vendor_name):
    """Item/vendor removal is permanent (unlike a sale, which only lowers qty and
    keeps history) — so this only ever queues a pending_delete_confirmation and
    waits for an explicit yes/no, never deletes on the first message. If the
    model couldn't tell item from vendor (delete_target is None), ask instead
    of guessing — same "missing data over invented data" reasoning as elsewhere."""
    chat_id, owner_id = ukey
    vendor_name = vendor_name or active_vendor.get(ukey)

    if delete_target not in ("item", "vendor"):
        send_message(chat_id, "Kya hatana hai — koi item ya poora vendor? Naam bhi batao.")
        return

    if delete_target == "vendor":
        if not vendor_name:
            send_message(chat_id, "Kaunsa vendor hatana hai? Naam batao.")
            return
        pending_delete_confirmation[ukey] = {"target": "vendor", "vendor_name": vendor_name}
        send_message(
            chat_id,
            f"⚠️ Pakka <b>{html.escape(vendor_name)}</b> ko poora hata du? Iska saara stock record "
            "bhi chala jayega, wapas nahi aayega.",
            parse_mode="HTML",
            reply_markup=DELETE_CONFIRM_MENU,
        )
        return

    if not item_name:
        send_message(chat_id, "Kaunsa item hatana hai? Naam batao.")
        return
    if not vendor_name:
        send_message(chat_id, "Kaunse vendor ke stock se hatana hai? Naam batao.")
        return
    pending_delete_confirmation[ukey] = {"target": "item", "vendor_name": vendor_name, "item_name": item_name}
    send_message(
        chat_id,
        f"⚠️ Pakka <b>{html.escape(item_name)}</b> ko <b>{html.escape(vendor_name)}</b> ke stock se hata du?",
        parse_mode="HTML",
        reply_markup=DELETE_CONFIRM_MENU,
    )


def confirm_delete(ukey):
    chat_id, owner_id = ukey
    data = pending_delete_confirmation.pop(ukey)
    conn = db.get_connection()

    if data["target"] == "vendor":
        vendor = db.delete_vendor(conn, owner_id, data["vendor_name"])
        if vendor is None:
            send_message(chat_id, f"{html.escape(data['vendor_name'])} mila nahi, kuch hataya nahi.")
            return
        if active_vendor.get(ukey) == vendor:
            active_vendor.pop(ukey, None)
        send_message(chat_id, f"Theek hai, <b>{html.escape(vendor)}</b> aur uska poora stock hata diya.", parse_mode="HTML")
        return

    result = db.delete_item(conn, owner_id, data["vendor_name"], data["item_name"])
    if result is None:
        send_message(chat_id, f"{html.escape(data['item_name'])} {html.escape(data['vendor_name'])} ke paas mila nahi.")
        return
    vendor, item = result
    active_vendor[ukey] = vendor
    send_message(chat_id, f"Theek hai, <b>{html.escape(item)}</b> ko {html.escape(vendor)} ke stock se hata diya.", parse_mode="HTML")


MAX_MESSAGE_CHARS = 3500  # Telegram caps at 4096; leave headroom for HTML entities


def handle_stock_query(ukey, vendor_name):
    chat_id, owner_id = ukey
    conn = db.get_connection()
    if not vendor_name:
        vendors = db.get_vendors(conn, owner_id)
        if not vendors:
            send_message(chat_id, "Abhi koi vendor record nahi hai mere paas.")
            return
        # No single vendor named — show every vendor's stock together in one
        # place instead of just listing names and making them ask again per
        # vendor. Chunked across messages if it's too long for one (Telegram's
        # 4096-char cap), split cleanly between vendors.
        chunk = ""
        for v in vendors:
            report = format_stock_report(conn, owner_id, v, db.get_stock_with_last_delivery(conn, owner_id, v))
            candidate = f"{chunk}\n\n{report}" if chunk else report
            if len(candidate) > MAX_MESSAGE_CHARS and chunk:
                send_message(chat_id, chunk, parse_mode="HTML")
                chunk = report
            else:
                chunk = candidate
        if chunk:
            send_message(chat_id, chunk, parse_mode="HTML")
        return
    active_vendor[ukey] = vendor_name
    items = db.get_stock_with_last_delivery(conn, owner_id, vendor_name)
    send_message(chat_id, format_stock_report(conn, owner_id, vendor_name, items), parse_mode="HTML")


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


def _format_days_left(days_left):
    """'~0 din' reads wrong when the rate says it's basically out already."""
    if days_left < 1:
        return "bahut jald"
    return f"~{days_left:.0f} din mein"


def _sale_line(conn, owner_id, vendor, matched_item, new_qty, matched_unit, qty_sold):
    line = f"• {html.escape(matched_item)}: {fmt_qty(new_qty, matched_unit)} bacha"
    qty_before = new_qty + qty_sold
    if new_qty < 0:
        line += "\n   ⚠️ Itna stock tha hi nahi, phir bhi kaat diya — ek baar check kar lena."
    elif new_qty <= LOW_STOCK_THRESHOLD < qty_before:
        # only fires the sale that crosses the line, not every sale after — otherwise
        # every subsequent sale of an already-low item would repeat the same nudge
        line += f"\n   📉 Kam bacha hai ({fmt_qty(new_qty, matched_unit)}), mangwa lena."
        days_left = db.estimate_days_left(conn, owner_id, vendor, matched_item, new_qty)
        if days_left is not None:
            line += f" Is raftaar se {_format_days_left(days_left)} khatam ho sakta hai."
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
                entries.append((vendor, _sale_line(conn, owner_id, vendor, matched_item, new_qty, matched_unit, qty)))
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
                entries.append((vendor, _sale_line(conn, owner_id, vendor, matched_item, new_qty, matched_unit, actual_qty)))
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
        send_message(
            chat_id,
            "📦 <b>Kya aaya, samajh nahi paaya</b>\n\n"
            "Item ka naam aur quantity dono batao, jaise:\n"
            "<code>Biscuit 20 pcs, Soap 10 pcs</code>",
            parse_mode="HTML",
        )
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
        message_id = send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=build_confirmation_menu(ukey, vendor_name, items))
        pending_stock_confirmation[ukey] = {"vendor_name": vendor_name, "items": items, "message_id": message_id}
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
        message_id = send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=build_confirmation_menu(ukey, vendor_name, items))
        pending_stock_confirmation[ukey] = {"vendor_name": vendor_name, "items": items, "message_id": message_id}
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Samajh nahi paya. Phir se try karo.")
        return False


def _show_in_confirmation_message(ukey, data, text, reply_markup):
    """Edits the tracked confirmation message in place (so tapping through an
    edit stays one evolving message instead of a new bubble each step);
    falls back to a new message only if we never got that message's id
    (e.g. the original send failed)."""
    chat_id = ukey[0]
    if data.get("message_id"):
        edit_message(chat_id, data["message_id"], text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


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
    _show_in_confirmation_message(ukey, data, header + "\n\n" + "\n".join(lines), {"inline_keyboard": []})


def handle_webapp_edit(ukey, raw_data):
    """A person saved the '✏️ List edit karo' form — Telegram delivers whatever
    it passed to sendData() as a normal message with web_app_data on it. Same
    trust level as any text reply (it only reaches us via a real Telegram
    update from this user), so no extra verification beyond the usual shape
    check. Malformed/empty payload leaves the pending confirmation untouched
    rather than guessing — missing data over invented data, as elsewhere."""
    chat_id = ukey[0]
    data = pending_stock_confirmation.get(ukey)
    if not data:
        return
    try:
        items = json.loads(raw_data).get("items", [])
        cleaned = [
            {"name": str(i["name"]).strip(), "qty": float(i["qty"]), "unit": (i.get("unit") or None)}
            for i in items
            if str(i.get("name") or "").strip() and i.get("qty") not in (None, "")
        ]
    except (ValueError, TypeError, KeyError):
        cleaned = []
    if not cleaned:
        send_message(chat_id, "Form se kuch samajh nahi aaya. Dobara try karo.")
        return
    data["items"] = cleaned
    _show_in_confirmation_message(
        ukey, data, format_stock_confirmation(data["vendor_name"], data["items"]), build_confirmation_menu(ukey, data["vendor_name"], data["items"])
    )


# Group-chat edit fallback — Telegram won't allow the WebApp button there (see
# build_confirmation_menu), so this reproduces the same "edit stays in one
# message" idea via tap-then-type-in-chat instead of a form.
ITEM_EDIT_PATTERN = re.compile(r"^(?P<name>.*?)\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)?\s*$")

EDIT_CANCEL_MENU = {"inline_keyboard": [[{"text": "🔙 Wapas list pe jao", "callback_data": "cancel_item_edit"}]]}


def _parse_item_edit_reply(text, current_name):
    """Parses a per-item correction: 'qty', 'qty unit', or 'new name qty unit' —
    a name is optional (keeps current_name if left out), a qty is not. Local
    regex instead of another Gemini call: this is a single short reply about one
    already-known item, not free-form extraction, so a deterministic parse is
    both faster and more predictable than an API round trip."""
    match = ITEM_EDIT_PATTERN.match(text.strip())
    if not match or not match.group("qty"):
        return None
    name = match.group("name").strip() or current_name
    return {"name": name, "qty": float(match.group("qty")), "unit": match.group("unit")}


def handle_item_edit_tap(ukey, index):
    """User tapped '✏️ <item>' on a pending stock confirmation (group chat) —
    edits that same message to ask just for this one item's corrected
    qty/name, instead of making them retype everything."""
    data = pending_stock_confirmation.get(ukey)
    if not data or index >= len(data["items"]):
        return
    awaiting_item_edit[ukey] = index
    item = data["items"][index]
    text = (
        f"<b>{html.escape(item['name'])}</b> ki sahi qty/naam batao, jaise:\n"
        '"8 pcs" (sirf qty badalni hai) ya "Soap 8 pcs" (naam bhi badalna hai)'
    )
    _show_in_confirmation_message(ukey, data, text, EDIT_CANCEL_MENU)


def cancel_item_edit(ukey):
    """'Wapas list pe jao' tap — drops back to the confirmation list unchanged,
    editing the same message rather than sending a new one."""
    awaiting_item_edit.pop(ukey, None)
    data = pending_stock_confirmation.get(ukey)
    if not data:
        return
    _show_in_confirmation_message(
        ukey, data, format_stock_confirmation(data["vendor_name"], data["items"]), build_confirmation_menu(ukey, data["vendor_name"], data["items"])
    )


def apply_item_edit(ukey, text):
    """Reply to handle_item_edit_tap's prompt — updates one item in place and
    edits the same confirmation message back (with fresh edit buttons) so more
    items can be fixed, or the delivery confirmed, without piling up messages."""
    index = awaiting_item_edit.pop(ukey)
    data = pending_stock_confirmation.get(ukey)
    if not data or index >= len(data["items"]):
        return
    current = data["items"][index]
    parsed = _parse_item_edit_reply(text, current["name"])
    if parsed is None:
        awaiting_item_edit[ukey] = index
        retry_text = (
            'Samajh nahi aaya. Qty ke saath batao, jaise: "8 pcs"\n\n'
            f"<b>{html.escape(current['name'])}</b> ki sahi qty/naam batao:"
        )
        _show_in_confirmation_message(ukey, data, retry_text, EDIT_CANCEL_MENU)
        return
    data["items"][index] = parsed
    _show_in_confirmation_message(
        ukey, data, format_stock_confirmation(data["vendor_name"], data["items"]), build_confirmation_menu(ukey, data["vendor_name"], data["items"])
    )


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

    elif data.startswith("edit_item:") and ukey in pending_stock_confirmation:
        handle_item_edit_tap(ukey, int(data.split(":", 1)[1]))

    elif data == "cancel_item_edit" and ukey in pending_stock_confirmation:
        cancel_item_edit(ukey)

    elif data == "confirm_no" and ukey in pending_stock_confirmation:
        cancelled = pending_stock_confirmation.pop(ukey, None)
        awaiting_item_edit.pop(ukey, None)
        _show_in_confirmation_message(ukey, cancelled or {}, "Theek hai, cancel kar diya.", {"inline_keyboard": []})

    elif data == "delete_confirm_yes" and ukey in pending_delete_confirmation:
        confirm_delete(ukey)

    elif data == "delete_confirm_no" and ukey in pending_delete_confirmation:
        pending_delete_confirmation.pop(ukey, None)
        send_message(chat_id, "Theek hai, rehne diya.")

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
    if (
        "text" not in message
        and "photo" not in message
        and "document" not in message
        and "voice" not in message
        and "web_app_data" not in message
    ):
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
            send_message(chat_id, f"Dhanyawad, {name}! Kaise ho aap? 😊\n\n{WELCOME}", reply_markup=MAIN_MENU)
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

    if "web_app_data" in message:
        handle_webapp_edit(ukey, message["web_app_data"].get("data", ""))
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

    elif ukey in awaiting_item_edit:
        apply_item_edit(ukey, text)

    elif ukey in pending_stock_confirmation and (text == BTN_YES or text.lower() in YES_WORDS):
        confirm_stock_addition(ukey)

    elif ukey in pending_stock_confirmation and (text == BTN_NO or text.lower() in NO_WORDS):
        cancelled = pending_stock_confirmation.pop(ukey)
        _show_in_confirmation_message(ukey, cancelled, "Theek hai, cancel kar diya.", {"inline_keyboard": []})

    elif ukey in pending_stock_confirmation:
        data = pending_stock_confirmation[ukey]
        _show_in_confirmation_message(
            ukey, data, format_stock_confirmation(data["vendor_name"], data["items"]), build_confirmation_menu(ukey, data["vendor_name"], data["items"])
        )

    elif ukey in pending_delete_confirmation and (text == BTN_DELETE_YES or text.lower() in YES_WORDS):
        confirm_delete(ukey)

    elif ukey in pending_delete_confirmation and (text == BTN_DELETE_NO or text.lower() in NO_WORDS):
        pending_delete_confirmation.pop(ukey)
        send_message(chat_id, "Theek hai, rehne diya.")

    elif ukey in pending_delete_confirmation:
        send_message(chat_id, "Haan ya nahi bata do — hatana hai?", reply_markup=DELETE_CONFIRM_MENU)

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
        handle_rename(ukey, result.get("rename_target"), result.get("old_name"), result.get("new_name"), result.get("vendor_name"))
    elif intent == "history":
        handle_history(ukey, result.get("vendor_name"))
    elif intent == "delete":
        items = result.get("items") or []
        item_name = items[0].get("name") if items else None
        handle_delete(ukey, result.get("delete_target"), item_name, result.get("vendor_name"))
    else:
        reply = result.get("reply")
        send_message(chat_id, reply or WELCOME, reply_markup=None if reply else MAIN_MENU)


DIGEST_HOUR_IST = 9  # don't message before a shopkeeper's likely awake and at the shop


def _chat_id_for_owner(owner_id):
    """A proactive digest needs a chat_id to send to, but stock is scoped by
    owner_id (== sender_id) — find the chat this person last talked to the bot
    in, from whoever's already onboarded. None if they've never messaged us
    (shouldn't happen for an owner_id with stock rows, but not worth a crash)."""
    for chat_id, sender_id in user_names:
        if sender_id == owner_id:
            return chat_id
    return None


def format_daily_digest(conn, owner_id, low_items):
    """Same item name can come from several vendors — group by item name so
    the alert reads as one combined line per item (total qty across vendors),
    not a repeated vendor-tagged entry for what's conceptually one item."""
    lines = ["🌅 <b>Aaj ka stock alert</b>", ""]
    by_item = {}
    for item in low_items:
        key = item["item_name"].strip().lower()
        group = by_item.setdefault(key, {"name": item["item_name"], "qty": 0, "unit": item.get("unit"), "vendors": []})
        group["qty"] += item["qty"]
        group["vendors"].append(item["vendor_name"])
    for group in by_item.values():
        marker = "⚠️" if group["qty"] < 0 else "📉"
        line = f"{marker} {html.escape(group['name'])}: {fmt_qty(group['qty'], group['unit'])}"
        if len(group["vendors"]) == 1:
            days_left = db.estimate_days_left(conn, owner_id, group["vendors"][0], group["name"], group["qty"])
            if days_left is not None:
                line += f" — {_format_days_left(days_left)} khatam ho sakta hai"
        lines.append(line)
    return "\n".join(lines)


def maybe_send_daily_digests():
    """Called once per poll-loop tick (main()). Sends at most one digest per
    owner per IST calendar day, and only once that day's check has passed
    DIGEST_HOUR_IST — this is on top of, not instead of, the reactive low-stock
    nudge _sale_line already gives at sale time; this is the safety net for
    whatever that missed (never sold in a way that crossed the threshold, or
    the shopkeeper hasn't messaged the bot in a few days)."""
    now = datetime.now(IST)
    if now.hour < DIGEST_HOUR_IST:
        return
    today = now.strftime("%Y-%m-%d")
    conn = db.get_connection()
    for owner_id in db.get_owners_with_stock(conn):
        if db.get_last_digest_date(conn, owner_id) == today:
            continue
        db.set_last_digest_date(conn, owner_id, today)  # mark checked regardless, so a quiet day isn't re-scanned all day
        low_items = db.get_low_stock_items(conn, owner_id, LOW_STOCK_THRESHOLD)
        if not low_items:
            continue
        chat_id = _chat_id_for_owner(owner_id)
        if chat_id is None:
            continue
        send_message(chat_id, format_daily_digest(conn, owner_id, low_items), parse_mode="HTML")


# The "List edit karo" WebApp page (build_edit_webapp_url above builds its URL).
# Fully self-contained and static: all the data it needs travels in its own
# query string, and saving hands the result back to Telegram itself (sendData),
# which delivers it to this same bot as a normal update — so this page never
# calls back to our server, and _WebAppRequestHandler below has nothing to look
# up or store, just this one HTML string to serve.
EDIT_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit karo</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root {
    --bg: #ffffff;
    --text: #111111;
    --hint: #707579;
    --button: #2481cc;
    --button-text: #ffffff;
    --border: #e3e3e3;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 16px 16px 80px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .hint { color: var(--hint); font-size: 13px; margin-bottom: 16px; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .row input {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px;
    font-size: 15px;
    background: transparent;
    color: var(--text);
    min-width: 0;
  }
  .row input.name { flex: 3; }
  .row input.qty { flex: 1.2; }
  .row input.unit { flex: 1; }
  .row button.remove {
    border: none;
    background: none;
    font-size: 18px;
    padding: 6px;
    cursor: pointer;
    color: #e04b4b;
  }
  #add-row {
    display: block;
    width: 100%;
    padding: 10px;
    margin-top: 8px;
    border: 1px dashed var(--border);
    border-radius: 8px;
    background: transparent;
    color: var(--button);
    font-size: 14px;
    cursor: pointer;
  }
  .error { color: #e04b4b; font-size: 13px; margin-top: 12px; display: none; }
  #save-btn {
    display: block;
    width: 100%;
    padding: 14px;
    margin-top: 16px;
    border: none;
    border-radius: 8px;
    background: var(--button);
    color: var(--button-text);
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
  }
</style>
</head>
<body>
  <h1 id="vendor-name">Stock</h1>
  <div class="hint">Item ki qty/naam/unit badal sakte ho. 🗑 se hatao, + se naya jodo.</div>
  <div id="rows"></div>
  <button id="add-row" type="button">+ Item jodo</button>
  <div class="error" id="error-msg"></div>
  <button id="save-btn" type="button">💾 Save karo</button>

<script>
try {
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  function applyTheme() {
    if (!tg || !tg.themeParams) return;
    var p = tg.themeParams;
    var root = document.documentElement.style;
    if (p.bg_color) root.setProperty('--bg', p.bg_color);
    if (p.text_color) root.setProperty('--text', p.text_color);
    if (p.hint_color) root.setProperty('--hint', p.hint_color);
    if (p.button_color) root.setProperty('--button', p.button_color);
    if (p.button_text_color) root.setProperty('--button-text', p.button_text_color);
  }
  applyTheme();
  if (tg) tg.onEvent('themeChanged', applyTheme);

  function b64urlDecode(str) {
    str = str.replace(/-/g, '+').replace(/_/g, '/');
    while (str.length % 4) str += '=';
    var binary = atob(str);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    if (window.TextDecoder) return new TextDecoder('utf-8').decode(bytes);
    return decodeURIComponent(escape(binary)); // older WebViews without TextDecoder
  }

  var params = new URLSearchParams(window.location.search);
  var payload = { vendor: '', items: [] };
  try {
    payload = JSON.parse(b64urlDecode(params.get('d') || ''));
  } catch (e) {
    var errEl0 = document.getElementById('error-msg');
    errEl0.textContent = 'Data load nahi hua. Bot mein wapas try karo.';
    errEl0.style.display = 'block';
  }

  document.getElementById('vendor-name').textContent = (payload.vendor || 'Stock') + ' se ye mila';

  var rowsEl = document.getElementById('rows');

  function addRow(item) {
    item = item || { name: '', qty: '', unit: '' };
    var row = document.createElement('div');
    row.className = 'row';
    row.innerHTML =
      '<input class="name" type="text" placeholder="Item naam">' +
      '<input class="qty" type="number" step="any" placeholder="Qty">' +
      '<input class="unit" type="text" placeholder="Unit">' +
      '<button class="remove" type="button">🗑</button>';
    row.querySelector('.name').value = item.name || '';
    row.querySelector('.qty').value = (item.qty === null || item.qty === undefined) ? '' : item.qty;
    row.querySelector('.unit').value = item.unit || '';
    row.querySelector('.remove').onclick = function () { row.remove(); };
    rowsEl.appendChild(row);
  }

  (payload.items || []).forEach(addRow);
  if (!payload.items || !payload.items.length) addRow();

  document.getElementById('add-row').onclick = function () { addRow(); };

  function collectItems() {
    var out = [];
    var bad = false;
    rowsEl.querySelectorAll('.row').forEach(function (row) {
      var name = row.querySelector('.name').value.trim();
      var qtyRaw = row.querySelector('.qty').value.trim();
      var unit = row.querySelector('.unit').value.trim();
      if (!name && !qtyRaw) return; // fully blank row, silently skip
      var qty = parseFloat(qtyRaw);
      if (!name || qtyRaw === '' || isNaN(qty)) { bad = true; return; }
      out.push({ name: name, qty: qty, unit: unit || null });
    });
    return bad ? null : out;
  }

  function trySave() {
    var items = collectItems();
    var errEl = document.getElementById('error-msg');
    if (items === null) {
      errEl.textContent = 'Har item ka naam aur qty dono bharo (ya poori row khaali chhod do).';
      errEl.style.display = 'block';
      return;
    }
    if (!items.length) {
      errEl.textContent = 'Kam se kam ek item rakho.';
      errEl.style.display = 'block';
      return;
    }
    errEl.style.display = 'none';
    if (tg && tg.sendData) {
      tg.sendData(JSON.stringify({ items: items }));
      tg.close();
    } else {
      alert('Ye page sirf Telegram ke andar (WebApp button se) kaam karta hai — browser mein direct khol ke save nahi hoga.');
    }
  }

  // Always-visible in-page button (some Telegram clients' native MainButton
  // bar at the bottom can be easy to miss) — MainButton is wired too, as a
  // bonus shortcut, but saving never depends on the user noticing it.
  document.getElementById('save-btn').onclick = trySave;
  if (tg && tg.MainButton) {
    tg.MainButton.setText('Save karo');
    tg.MainButton.show();
    tg.MainButton.onClick(trySave);
  }
} catch (e) {
  alert('Page load karne mein dikkat aayi: ' + (e && e.message ? e.message : e));
}
</script>
</body>
</html>
"""


class _WebAppRequestHandler(http.server.BaseHTTPRequestHandler):
    """Serves exactly one static page (EDIT_PAGE_HTML) at /edit — no routing or
    per-request state, since the page carries its own data in its URL."""

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/edit":
            body = EDIT_PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep Railway logs focused on bot activity, not every HTTP hit


def start_webapp_server():
    """Runs the edit-form HTTP server in a background thread alongside the
    Telegram long-poll loop in main() — same process, same deploy, just also
    listening on $PORT so Railway's public domain can reach it."""
    server = http.server.ThreadingHTTPServer(("0.0.0.0", WEBAPP_PORT), _WebAppRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"WebApp edit form serving on :{WEBAPP_PORT}", flush=True)


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN not set. export TELEGRAM_BOT_TOKEN=...")
    start_webapp_server()
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
            try:
                maybe_send_daily_digests()
            except Exception:
                import traceback
                traceback.print_exc()
        except requests.exceptions.RequestException as e:
            print(f"Network hiccup, retrying in 5s: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
