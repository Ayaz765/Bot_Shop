"""Telegram front end (LUMO): read/summarize invoices and track per-vendor stock.
Long-polls the Telegram Bot API directly via requests — no new dependency.

Two menu paths:
1) Read & Summarize Invoice — photo or PDF in, plain summary out. No discrepancy
   checking (that flow, built around checker.py, has been retired from the bot).
2) Add Items to Stock — vendor name, then photo or typed list, then a confirmation
   before db.add_stock() runs. Stock is tracked per vendor (db.py Phase A).

Selling something is free-text at any time ("5 Maggi becha") — see Phase E.
"""

import html
import os
import sys
import time
from datetime import datetime

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

BOT_NAME = "LUMO"
WELCOME = (
    "Kya karna hai?\n\n"
    "1️⃣ Read & Summarize Invoice — bill ki photo ya PDF bhejo, summary milega "
    "(vendor, items, quantity, price, tax, total)\n\n"
    "2️⃣ Add Items to Stock — vendor se jo maal aaya wo apne stock mein jama karo, "
    "photo se ya khud type karke"
)

user_names = {}  # chat_id -> name, once they've told us
awaiting_name = set()  # chat_id currently expected to reply with their name
pending_photo = {}  # chat_id -> downloaded file path, if one arrived before we had a name

awaiting_stock_vendor = set()  # chat_id chose "Add to Stock", waiting for vendor name
awaiting_stock_method = {}  # chat_id -> vendor_name, waiting for photo-or-manual choice
awaiting_stock_photo = {}  # chat_id -> vendor_name, waiting for the delivery photo
awaiting_stock_manual_text = {}  # chat_id -> vendor_name, waiting for typed item list
pending_stock_confirmation = {}  # chat_id -> {"vendor_name", "items"}, waiting yes/no

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

# Inline buttons attach to one message and never take over the keyboard area, so
# there's nothing to "remove" the way a ReplyKeyboardMarkup panel needs — that panel
# was the actual bug (stays open until the user manually taps back to their keyboard).
NO_KEYBOARD = None

ALL_BUTTON_TEXTS = {
    BTN_SUMMARIZE, BTN_STOCK, BTN_STOCK_PHOTO, BTN_STOCK_MANUAL, BTN_YES, BTN_NO,
}

YES_WORDS = {"haan", "ha", "han", "yes", "y", "ok", "okay", "theek hai", "kar do", "add karo"}
NO_WORDS = {"nahi", "nah", "no", "n", "cancel", "mat karo", "chhodo"}


def clear_stock_flow(chat_id):
    """Drop any in-progress Add-to-Stock state for this chat — used when the user
    explicitly starts a fresh flow via the main menu, so a stray/late tap of an old
    button doesn't silently overwrite progress (it resets it on purpose instead)."""
    awaiting_stock_vendor.discard(chat_id)
    awaiting_stock_method.pop(chat_id, None)
    awaiting_stock_photo.pop(chat_id, None)
    awaiting_stock_manual_text.pop(chat_id, None)
    pending_stock_confirmation.pop(chat_id, None)

STOCK_ENTRY_SYSTEM_PROMPT = """Ek dukaandaar type karke bata raha hai ki vendor se kaunse items \
aur kitni quantity mein aaye. Naam, quantity, aur unit (kg, litre, pcs, box, dozen, bag, etc.) \
chahiye — rate ki zarurat nahi. Isi JSON shape mein nikaalo, sirf JSON do, kuch aur text nahi:

{"items": [{"name": string, "qty": number, "unit": string or null}]}

Unit na bataya gaya ho to null rakho — mat maano "pcs" hai."""

FREE_TEXT_SYSTEM_PROMPT = """Tum LUMO ho, ek Hinglish-bolne wala dukaan-stock-tracking bot. User \
ka message padhkar uska intent nikaalo, is JSON shape mein (sirf JSON do, kuch aur text nahi):

{"intent": "sale" | "stock_query" | "chat", "items": [{"name": string, "qty": number, \
"unit": string or null}], "vendor_name": string or null, "reply": string}

- "sale": user ne bataya ki kuch becha/sold hua (jaise "5 kg Sugar becha", "10 pcs soap nikal \
gaya"). items mein wo bharo, unit agar bataya ho. vendor_name sirf tab bharo jab usne khud \
vendor ka naam liya ho.
- "stock_query": user kisi vendor ka stock/hisaab pooch raha hai (jaise "Ayaz ka stock batao", \
"Ramesh se kya aaya hai"). vendor_name zaroor bharo.
- "chat": baaki sab (greeting, casual baat, sawaal). "reply" mein chhota (1-2 line) dostana \
Hinglish jawab do jaise ek dost deta hai."""

NOT_A_NAME = {
    "hi", "hii", "hiii", "hiiii", "hello", "hey", "hey lumo", "hii lumo",
    "namaste", "namaskar", "salam", "yo", "ok", "okay", "test", "hlo", "lumo",
}


def time_greeting():
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


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
    if invoice.get("invoice_number"):
        header += f" — {html.escape(str(invoice['invoice_number']))}"
    if invoice.get("invoice_date"):
        header += f" ({html.escape(str(invoice['invoice_date']))})"

    lines = [f"<b>{header}</b>", ""]
    for item in invoice.get("items", []):
        parts = [html.escape(item.get("name") or "")]
        if item.get("qty") is not None:
            parts.append(f"qty {fmt_qty(item['qty'], item.get('unit'))}")
        if item.get("rate") is not None:
            parts.append(f"rate Rs{item['rate']:g}")
        if item.get("amount") is not None:
            parts.append(f"= Rs{item['amount']:g}")
        lines.append("• " + " — ".join(parts))

    lines.append("")
    if invoice.get("sub_total") is not None:
        lines.append(f"Sub total: Rs{invoice['sub_total']:.2f}")
    if invoice.get("tax") is not None:
        lines.append(f"Tax: Rs{invoice['tax']:.2f}")
    if invoice.get("grand_total") is not None:
        lines.append(f"<b>Grand total: Rs{invoice['grand_total']:.2f}</b>")
    return "\n".join(lines)


def fmt_qty(qty, unit):
    return f"{qty:g} {unit}" if unit else f"{qty:g}"


def format_stock_confirmation(vendor_name, items):
    lines = [f"<b>{html.escape(vendor_name)} se ye mila:</b>", ""]
    for item in items:
        lines.append(f"• {html.escape(item['name'])} — {fmt_qty(item['qty'], item.get('unit'))}")
    lines.append("")
    lines.append("Stock mein add kar doon?")
    return "\n".join(lines)


def interpret_free_text(text):
    """One Gemini call classifies + extracts: sale, stock lookup, or plain chat —
    avoids a separate detect-then-reply pair of calls on every ordinary message."""
    key = os.environ.get("GEMINI_API_KEY")
    resp = requests.post(
        f"{extract.GEMINI_API_ROOT}/models/{extract.DEFAULT_GEMINI_MODEL}:generateContent",
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": FREE_TEXT_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": text}]}],
        },
    )
    resp.raise_for_status()
    return extract._parse_json_response(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


def format_stock_report(vendor_name, items):
    if not items:
        return f"{html.escape(vendor_name)} ka koi stock record nahi hai mere paas."
    lines = [f"<b>{html.escape(vendor_name)} ka stock:</b>", ""]
    for item in items:
        lines.append(f"• {html.escape(item['item_name'])} — {fmt_qty(item['qty'], item.get('unit'))}")
    return "\n".join(lines)


def handle_stock_query(chat_id, vendor_name):
    if not vendor_name:
        send_message(chat_id, "Kaunse vendor ka stock dekhna hai?", reply_markup=MAIN_MENU)
        return
    conn = db.get_connection()
    items = db.get_stock_for_vendor(conn, vendor_name)
    send_message(chat_id, format_stock_report(vendor_name, items), parse_mode="HTML", reply_markup=MAIN_MENU)


def handle_sale(chat_id, items, vendor_name):
    if not items:
        send_message(chat_id, "Samajh nahi aaya kya becha. Phir se batao?", reply_markup=MAIN_MENU)
        return

    conn = db.get_connection()
    lines = []
    for item in items:
        name, qty, unit = item.get("name"), item.get("qty"), item.get("unit")
        if not name or not qty:
            continue

        if vendor_name:
            vendor, matched_item, matched_unit, new_qty = db.record_sale(conn, vendor_name, name, qty, unit)
            lines.append(f"• {html.escape(matched_item)}: ab {fmt_qty(new_qty, matched_unit)} bacha ({html.escape(vendor)})")
            continue

        matches = db.find_item_across_vendors(conn, name)
        if not matches:
            lines.append(f"• {html.escape(name)}: ye stock mein nahi mila.")
        elif len(matches) == 1:
            m = matches[0]
            vendor, matched_item, matched_unit, new_qty = db.record_sale(conn, m["vendor_name"], m["item_name"], qty, unit)
            lines.append(f"• {html.escape(matched_item)}: ab {fmt_qty(new_qty, matched_unit)} bacha ({html.escape(vendor)})")
        else:
            vendors = " / ".join(html.escape(m["vendor_name"]) for m in matches)
            example = matches[0]["vendor_name"]
            lines.append(
                f"• {html.escape(name)}: {vendors} — dono ke paas hai, kis ka becha? "
                f"Jaise likho: \"{example} ka {html.escape(name)} becha\""
            )

    send_message(chat_id, "\n".join(lines) if lines else "Kuch update nahi hua.", parse_mode="HTML", reply_markup=MAIN_MENU)


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


def process_summarize(chat_id, file_path):
    send_message(chat_id, f"{user_names[chat_id]}, padh raha hoon...")
    try:
        invoice = extract.extract(file_path, provider=PROVIDER)
        conn = db.get_connection()
        db.save_invoice(
            conn, invoice.get("supplier_name"), invoice.get("invoice_number"),
            invoice.get("invoice_date"), invoice.get("grand_total"),
            invoice.get("items", []), [],
        )
        send_message(chat_id, format_summary_html(invoice), parse_mode="HTML", reply_markup=MAIN_MENU)
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Padhne mein dikkat aayi, dobara try karo.", reply_markup=MAIN_MENU)


def process_stock_photo(chat_id, vendor_name, file_path):
    """Returns True on success. False means the caller should let the user retry
    (keep them in the same waiting-for-photo state) instead of dropping them back
    to the main menu with no way to continue without starting the flow over."""
    send_message(chat_id, f"{user_names[chat_id]}, photo padh raha hoon...")
    try:
        invoice = extract.extract(file_path, provider=PROVIDER)
        items = [
            {"name": i["name"], "qty": i["qty"], "unit": i.get("unit")}
            for i in invoice.get("items", []) if i.get("qty")
        ]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi is photo mein. Dusri photo try karo.")
            return False
        pending_stock_confirmation[chat_id] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Padhne mein dikkat aayi. Dusri photo try karo.")
        return False


def process_stock_manual(chat_id, vendor_name, text):
    """Returns True on success, False if the caller should let the user retry
    typing (same reasoning as process_stock_photo)."""
    send_message(chat_id, f"{user_names[chat_id]}, samajh raha hoon...")
    try:
        items = [
            {"name": i["name"], "qty": i["qty"], "unit": i.get("unit")}
            for i in extract_stock_items(text) if i.get("qty")
        ]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi. Phir se batao, jaise: \"Biscuit 20 pcs\"")
            return False
        pending_stock_confirmation[chat_id] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
        return True
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Samajh nahi paya. Phir se try karo.")
        return False


def confirm_stock_addition(chat_id):
    data = pending_stock_confirmation.pop(chat_id)
    conn = db.get_connection()
    lines = ["<b>Stock update ho gaya:</b>", ""]
    for item in data["items"]:
        vendor, name, unit, new_qty = db.add_stock(conn, data["vendor_name"], item["name"], item["qty"], item.get("unit"))
        lines.append(f"• {html.escape(name)}: ab {fmt_qty(new_qty, unit)} ({html.escape(vendor)})")
    send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_MENU)


def handle_callback_query(cq):
    """Inline-button taps. Mirrors the equivalent text == BTN_X branches in
    handle_update, but keyed off callback_data instead of typed text."""
    answer_callback(cq["id"])
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    data = cq.get("data", "")
    print(f"DEBUG callback: chat_id={chat_id} data={data!r}", flush=True)
    if not chat_id or chat_id not in user_names:
        return  # menus only ever shown after onboarding

    if data == "summarize":
        clear_stock_flow(chat_id)
        send_message(chat_id, "Theek hai, bill ki photo ya PDF bhej do.")

    elif data == "stock":
        clear_stock_flow(chat_id)
        awaiting_stock_vendor.add(chat_id)
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.")

    elif data == "stock_photo" and chat_id in awaiting_stock_method:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_photo[chat_id] = vendor_name
        send_message(chat_id, "Theek hai, photo bhej do.")

    elif data == "stock_manual" and chat_id in awaiting_stock_method:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_manual_text[chat_id] = vendor_name
        send_message(chat_id, 'Batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"')

    elif data == "confirm_yes" and chat_id in pending_stock_confirmation:
        confirm_stock_addition(chat_id)

    elif data == "confirm_no" and chat_id in pending_stock_confirmation:
        pending_stock_confirmation.pop(chat_id, None)
        send_message(chat_id, "Theek hai, cancel kar diya.", reply_markup=MAIN_MENU)


def handle_update(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    print(f"DEBUG update: chat_id={chat_id} keys={list(message.keys())} text={message.get('text')!r}", flush=True)
    if not chat_id:
        return
    if "text" not in message and "photo" not in message and "document" not in message:
        return  # ignore group system messages: joins, leaves, pins, etc.

    # New chat: greet by time of day and ask for a name before doing anything else.
    if chat_id not in user_names:
        if chat_id in awaiting_name and "text" in message:
            name = message["text"].strip()
            if name.lower() in NOT_A_NAME or name in ALL_BUTTON_TEXTS:
                send_message(chat_id, "Wo naam nahi laga 😅 Bas apna naam likho, jaise: Ramesh")
                return
            user_names[chat_id] = name
            awaiting_name.discard(chat_id)
            send_message(chat_id, f"Dhanyawad, {name}! {WELCOME}", reply_markup=MAIN_MENU)
            if chat_id in pending_photo:
                process_summarize(chat_id, pending_photo.pop(chat_id))
            return

        awaiting_name.add(chat_id)
        if "photo" in message:
            pending_photo[chat_id] = save_incoming_photo(message)
        elif "document" in message:
            path = save_incoming_document(message)
            if path:
                pending_photo[chat_id] = path
        send_message(chat_id, f"{time_greeting()}! Main {BOT_NAME} hoon. Pehle apna naam bata do?")
        return

    # Photo / PDF: which flow is active decides what happens to it.
    if "photo" in message or "document" in message:
        file_path = save_incoming_photo(message) if "photo" in message else save_incoming_document(message)
        if not file_path:
            send_message(chat_id, "Ye file PDF ya image nahi lagi.", reply_markup=MAIN_MENU)
            return
        if chat_id in awaiting_stock_photo:
            vendor_name = awaiting_stock_photo[chat_id]
            if process_stock_photo(chat_id, vendor_name, file_path):
                awaiting_stock_photo.pop(chat_id, None)
        else:
            process_summarize(chat_id, file_path)
        return

    text = message["text"].strip()

    if text == BTN_SUMMARIZE:
        clear_stock_flow(chat_id)
        send_message(chat_id, "Theek hai, bill ki photo ya PDF bhej do.", reply_markup=NO_KEYBOARD)

    elif text == BTN_STOCK:
        clear_stock_flow(chat_id)
        awaiting_stock_vendor.add(chat_id)
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.", reply_markup=NO_KEYBOARD)

    elif chat_id in awaiting_stock_vendor and text in ALL_BUTTON_TEXTS:
        send_message(chat_id, "Vendor ka naam likho (button nahi), jaise: Ayaz")

    elif chat_id in awaiting_stock_vendor:
        awaiting_stock_vendor.discard(chat_id)
        awaiting_stock_method[chat_id] = text
        send_message(chat_id, "Photo bhejoge ya khud type karoge?", reply_markup=STOCK_METHOD_MENU)

    elif chat_id in awaiting_stock_method and text == BTN_STOCK_PHOTO:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_photo[chat_id] = vendor_name
        send_message(chat_id, "Theek hai, photo bhej do.", reply_markup=NO_KEYBOARD)

    elif chat_id in awaiting_stock_method and text == BTN_STOCK_MANUAL:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_manual_text[chat_id] = vendor_name
        send_message(chat_id, 'Batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"', reply_markup=NO_KEYBOARD)

    elif chat_id in awaiting_stock_manual_text:
        vendor_name = awaiting_stock_manual_text[chat_id]
        if process_stock_manual(chat_id, vendor_name, text):
            awaiting_stock_manual_text.pop(chat_id, None)

    elif chat_id in pending_stock_confirmation and (text == BTN_YES or text.lower() in YES_WORDS):
        confirm_stock_addition(chat_id)

    elif chat_id in pending_stock_confirmation and (text == BTN_NO or text.lower() in NO_WORDS):
        pending_stock_confirmation.pop(chat_id)
        send_message(chat_id, "Theek hai, cancel kar diya.", reply_markup=MAIN_MENU)

    elif chat_id in pending_stock_confirmation:
        send_message(chat_id, "Haan ya nahi bata do — stock mein add karna hai?", reply_markup=CONFIRM_MENU)

    else:
        try:
            result = interpret_free_text(text)
        except Exception:
            import traceback
            traceback.print_exc()
            send_message(chat_id, f"{user_names[chat_id]}, {WELCOME}", reply_markup=MAIN_MENU)
            return

        intent = result.get("intent")
        if intent == "stock_query":
            handle_stock_query(chat_id, result.get("vendor_name"))
        elif intent == "sale":
            handle_sale(chat_id, result.get("items", []), result.get("vendor_name"))
        else:
            send_message(chat_id, result.get("reply") or WELCOME, reply_markup=MAIN_MENU)


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
