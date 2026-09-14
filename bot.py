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
MAIN_MENU = {"keyboard": [[BTN_SUMMARIZE], [BTN_STOCK]], "resize_keyboard": True}

BTN_STOCK_PHOTO = "📸 Add from Image"
BTN_STOCK_MANUAL = "✍️ Add Manually"
STOCK_METHOD_MENU = {"keyboard": [[BTN_STOCK_PHOTO], [BTN_STOCK_MANUAL]], "resize_keyboard": True}

BTN_YES = "✅ Haan, add karo"
BTN_NO = "❌ Nahi, cancel"
CONFIRM_MENU = {"keyboard": [[BTN_YES], [BTN_NO]], "resize_keyboard": True}

STOCK_ENTRY_SYSTEM_PROMPT = """Ek dukaandaar type karke bata raha hai ki vendor se kaunse items \
aur kitni quantity mein aaye. Sirf naam aur quantity chahiye, rate ki zarurat nahi. Isi JSON \
shape mein nikaalo, sirf JSON do, kuch aur text nahi:

{"items": [{"name": string, "qty": number}]}"""

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
    requests.post(f"{API_ROOT}/sendMessage", json=payload)


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
            parts.append(f"qty {item['qty']:g}")
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


def format_stock_confirmation(vendor_name, items):
    lines = [f"<b>{html.escape(vendor_name)} se ye mila:</b>", ""]
    for item in items:
        lines.append(f"• {html.escape(item['name'])} — {item['qty']:g}")
    lines.append("")
    lines.append("Stock mein add kar doon?")
    return "\n".join(lines)


def casual_reply(chat_id, text):
    """For chit-chat ('kaise ho', 'hi') instead of the canned WELCOME every time."""
    name = user_names.get(chat_id, "")
    system_prompt = (
        f"Tum {BOT_NAME} ho, ek dostana Hinglish-bolne wala Telegram bot jo dukaandaar ke bills "
        f"padhta/summarize karta hai aur unka stock track karta hai. User ka naam {name or 'pata nahi'} "
        "hai. Koi casual baat kare (haal-chaal, hi, kaise ho) to garmjoshi se, chhota sa (1-2 line) "
        "Hinglish reply do, jaise ek dost jawab deta hai. Kaam ka zikar sirf tab karo jab natural lage."
    )
    key = os.environ.get("GEMINI_API_KEY")
    resp = requests.post(
        f"{extract.GEMINI_API_ROOT}/models/{extract.DEFAULT_GEMINI_MODEL}:generateContent",
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"parts": [{"text": text}]}],
        },
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


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
    send_message(chat_id, f"{user_names[chat_id]}, photo padh raha hoon...")
    try:
        invoice = extract.extract(file_path, provider=PROVIDER)
        items = [{"name": i["name"], "qty": i["qty"]} for i in invoice.get("items", []) if i.get("qty")]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi is photo mein.", reply_markup=MAIN_MENU)
            return
        pending_stock_confirmation[chat_id] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Padhne mein dikkat aayi, dobara try karo.", reply_markup=MAIN_MENU)


def process_stock_manual(chat_id, vendor_name, text):
    send_message(chat_id, f"{user_names[chat_id]}, samajh raha hoon...")
    try:
        items = [{"name": i["name"], "qty": i["qty"]} for i in extract_stock_items(text) if i.get("qty")]
        if not items:
            send_message(chat_id, "Koi item/quantity samajh nahi aayi.", reply_markup=MAIN_MENU)
            return
        pending_stock_confirmation[chat_id] = {"vendor_name": vendor_name, "items": items}
        send_message(chat_id, format_stock_confirmation(vendor_name, items), parse_mode="HTML", reply_markup=CONFIRM_MENU)
    except Exception:
        import traceback
        traceback.print_exc()
        send_message(chat_id, "Samajh nahi paya, dobara try karo.", reply_markup=MAIN_MENU)


def confirm_stock_addition(chat_id):
    data = pending_stock_confirmation.pop(chat_id)
    conn = db.get_connection()
    lines = ["<b>Stock update ho gaya:</b>", ""]
    for item in data["items"]:
        vendor, name, new_qty = db.add_stock(conn, data["vendor_name"], item["name"], item["qty"])
        lines.append(f"• {html.escape(name)}: ab {new_qty:g} ({html.escape(vendor)})")
    send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_MENU)


def handle_update(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    print(f"DEBUG update: chat_id={chat_id} keys={list(message.keys())}", flush=True)
    if not chat_id:
        return
    if "text" not in message and "photo" not in message and "document" not in message:
        return  # ignore group system messages: joins, leaves, pins, etc.

    # New chat: greet by time of day and ask for a name before doing anything else.
    if chat_id not in user_names:
        if chat_id in awaiting_name and "text" in message:
            name = message["text"].strip()
            if name.lower() in NOT_A_NAME:
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
            vendor_name = awaiting_stock_photo.pop(chat_id)
            process_stock_photo(chat_id, vendor_name, file_path)
        else:
            process_summarize(chat_id, file_path)
        return

    text = message["text"].strip()

    if text == BTN_SUMMARIZE:
        send_message(chat_id, "Theek hai, bill ki photo ya PDF bhej do.")

    elif text == BTN_STOCK:
        awaiting_stock_vendor.add(chat_id)
        send_message(chat_id, "Kaunse vendor se maal aaya? Naam batao.")

    elif chat_id in awaiting_stock_vendor:
        awaiting_stock_vendor.discard(chat_id)
        awaiting_stock_method[chat_id] = text
        send_message(chat_id, "Photo bhejoge ya khud type karoge?", reply_markup=STOCK_METHOD_MENU)

    elif chat_id in awaiting_stock_method and text == BTN_STOCK_PHOTO:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_photo[chat_id] = vendor_name
        send_message(chat_id, "Theek hai, photo bhej do.")

    elif chat_id in awaiting_stock_method and text == BTN_STOCK_MANUAL:
        vendor_name = awaiting_stock_method.pop(chat_id)
        awaiting_stock_manual_text[chat_id] = vendor_name
        send_message(chat_id, 'Batao kya-kya aaya, jaise:\n"Biscuit 20 pcs, Soap 10 pcs"')

    elif chat_id in awaiting_stock_manual_text:
        vendor_name = awaiting_stock_manual_text.pop(chat_id)
        process_stock_manual(chat_id, vendor_name, text)

    elif chat_id in pending_stock_confirmation and text == BTN_YES:
        confirm_stock_addition(chat_id)

    elif chat_id in pending_stock_confirmation and text == BTN_NO:
        pending_stock_confirmation.pop(chat_id)
        send_message(chat_id, "Theek hai, cancel kar diya.", reply_markup=MAIN_MENU)

    else:
        try:
            send_message(chat_id, casual_reply(chat_id, text), reply_markup=MAIN_MENU)
        except Exception:
            send_message(chat_id, f"{user_names[chat_id]}, {WELCOME}", reply_markup=MAIN_MENU)


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
                handle_update(update)
        except requests.exceptions.RequestException as e:
            print(f"Network hiccup, retrying in 5s: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
