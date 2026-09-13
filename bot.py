"""Telegram front end for the same pipeline main.py uses (extract -> checker -> db).
Long-polls the Telegram Bot API directly via requests — no new dependency.

Flow: shopkeeper sends a bill photo -> bot replies with the Hinglish report,
assuming everything matched the bill. If something was short/extra, they
reply with the same shorthand as --received (e.g. "1:94, 3:10") and get an
updated report. Only the first pass (before any correction) is saved to the
database — db.py has no update path yet, so a correction is shown but not
re-saved. Good enough for testing; revisit if this becomes the real product.
"""

import html
import os
import sys
import time
from datetime import datetime

import requests

import checker
import db
import extract
from main import parse_received

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API_ROOT = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_ROOT = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
PROVIDER = os.environ.get("BILLCHECK_PROVIDER", "gemini")

BOT_NAME = "LUMO"
WELCOME = (
    "Teen tareeke se bata sakte ho:\n\n"
    "📷 Photo — bill kheech ke bhejo, main check kar dunga kitna paisa phansa hai\n"
    "🏪 Vendor bata ke — pehle vendor ka naam bolo (jaise Ayaz), fir uski bill ki photo bhejo\n"
    "✍️ Bina photo — type karke batao kya-kya aaya, price ke saath\n\n"
    "Aur kabhi bhi \"purana hisaab\" dabake dekh sakte ho kisi vendor se ab tak kya-kya aaya."
)

pending = {}  # chat_id -> last extracted invoice, so a follow-up text can re-check it
user_names = {}  # chat_id -> name, once they've told us
awaiting_name = set()  # chat_id currently expected to reply with their name
pending_photo = {}  # chat_id -> downloaded image path, if a photo arrived before we had a name
vendor_override = {}  # chat_id -> vendor name to force onto invoices, set via /vendor
awaiting_manual_entry = set()  # chat_id currently expected to describe a delivery in text
awaiting_vendor_name = set()  # chat_id currently expected to reply with a vendor name
awaiting_history_vendor = set()  # chat_id currently expected to name a vendor to look up

BTN_PHOTO = "📷 Photo bhejunga"
BTN_VENDOR = "🏪 Vendor ka naam batau"
BTN_MANUAL = "✍️ Bina photo ke likhunga"
BTN_HISTORY = "📜 Purana hisaab"

MAIN_MENU = {
    "keyboard": [[BTN_PHOTO], [BTN_VENDOR], [BTN_MANUAL], [BTN_HISTORY]],
    "resize_keyboard": True,
}

MANUAL_ENTRY_SYSTEM_PROMPT = """Ek dukaandaar bina bill ki photo ke bata raha hai ki supplier se \
kya maal aaya. Jo bhi items, quantity aur rate usne bataye hain unhe isi JSON shape mein nikaalo:

{
  "supplier_name": string or null,
  "invoice_number": null,
  "invoice_date": null,
  "items": [{"name": string, "qty": number, "rate": number or null, "amount": number or null}],
  "sub_total": null,
  "tax": null,
  "grand_total": null,
  "confidence": 1.0,
  "unreadable_fields": []
}

Rate na bataya gaya ho to null rakho (amount bhi null). Sirf JSON do, kuch aur text nahi."""

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


def format_report_html(invoice, issues):
    header = html.escape(invoice.get("supplier_name") or "Unknown Supplier")
    if invoice.get("invoice_number"):
        header += f" — {html.escape(str(invoice['invoice_number']))}"

    lines = [f"<b>{header}</b>", ""]
    if not issues:
        lines.append("✅ Sab sahi hai. Koi gadbad nahi mili.")
    else:
        for issue in issues:
            lines.append(f"⚠️ {html.escape(issue['msg'])}")

    total = round(sum(issue["loss"] for issue in issues), 2)
    lines.append("")
    lines.append(f"<b>💰 Total phansa paisa: Rs{total:.2f}</b>")
    return "\n".join(lines)


def format_correction_prompt(invoice):
    numbered_items = "\n".join(
        f"{i}. {html.escape(item['name'])}"
        for i, item in enumerate(invoice.get("items", []), start=1)
    )
    return (
        "<b>Maal gin liya?</b> Agar koi cheez kam ya zyada nikli, to uska number "
        "aur jitna asal mein mila wo bhej do.\n\n"
        f"{numbered_items}\n\n"
        "Jaise: upar wali list mein No. 1 wali cheez ginne pe sirf 94 nikli (bill mein zyada thi), "
        "to bhejo: <code>1:94</code>\n"
        "Ek se zyada cheez mein farak ho to comma se: <code>1:94, 3:10</code>\n\n"
        "Sab kuch bill jitna hi mila? Kuch bhejne ki zarurat nahi hai."
    )


def format_vendor_history(vendor_name, invoices):
    if not invoices:
        return f"<b>{html.escape(vendor_name)}</b> se abhi tak koi bill nahi mila mere paas."

    lines = [f"<b>{html.escape(vendor_name)} — pichle {len(invoices)} bill</b>", ""]
    total_loss = 0.0
    for inv in invoices:
        num = inv["invoice_number"] or "(number nahi)"
        date = inv["invoice_date"] or ""
        loss = inv["total_loss"] or 0.0
        total_loss += loss
        loss_note = f" — ⚠️ Rs{loss:.2f} phansa" if loss > 0 else ""
        lines.append(f"• {html.escape(str(num))} {date}: Rs{inv['grand_total'] or 0:.2f}{loss_note}")

    lines.append("")
    lines.append(f"<b>💰 In sab mein total phansa: Rs{total_loss:.2f}</b>")
    return "\n".join(lines)


def casual_reply(chat_id, text):
    """For chit-chat ('kaise ho', 'hi') instead of the canned WELCOME every time."""
    name = user_names.get(chat_id, "")
    system_prompt = (
        f"Tum {BOT_NAME} ho, ek dostana Hinglish-bolne wala Telegram bot jo supplier bills check "
        f"karta hai (dukaandaar photo bhejta hai, tum bataate ho kitna paisa phansa hai). "
        f"User ka naam {name or 'pata nahi'} hai. Koi casual baat kare (haal-chaal, hi, kaise ho) "
        "to garmjoshi se, chhota sa (1-2 line) Hinglish reply do, jaise ek dost jawab deta hai. "
        "Bill ka zikar sirf tab karo jab natural lage, zabardasti har baar mat dohrao."
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


def extract_from_text(text):
    """Manual entry: same invoice JSON shape as extract.py, but parsed from typed text."""
    key = os.environ.get("GEMINI_API_KEY")
    resp = requests.post(
        f"{extract.GEMINI_API_ROOT}/models/{extract.DEFAULT_GEMINI_MODEL}:generateContent",
        params={"key": key},
        json={
            "system_instruction": {"parts": [{"text": MANUAL_ENTRY_SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": text}]}],
        },
    )
    resp.raise_for_status()
    return extract._parse_json_response(resp.json()["candidates"][0]["content"]["parts"][0]["text"])


def download_photo(file_id, dest_path):
    file_info = requests.get(f"{API_ROOT}/getFile", params={"file_id": file_id}).json()["result"]
    data = requests.get(f"{FILE_ROOT}/{file_info['file_path']}").content
    with open(dest_path, "wb") as f:
        f.write(data)


def process_invoice_data(chat_id, invoice):
    """Shared by photo and manual-entry paths, once we have an invoice dict."""
    if chat_id in vendor_override:
        invoice["supplier_name"] = vendor_override[chat_id]

    conn = db.get_connection()
    issues = checker.check_invoice(conn, invoice, received_qty={})
    db.save_invoice(
        conn, invoice.get("supplier_name"), invoice.get("invoice_number"),
        invoice.get("invoice_date"), invoice.get("grand_total"),
        invoice.get("items", []), issues,
    )
    pending[chat_id] = invoice
    send_message(chat_id, format_report_html(invoice, issues), parse_mode="HTML")
    send_message(chat_id, format_correction_prompt(invoice), parse_mode="HTML")


def process_photo(chat_id, image_path):
    send_message(chat_id, f"{user_names[chat_id]}, bill padh raha hoon...")
    try:
        invoice = extract.extract(image_path, provider=PROVIDER)
        process_invoice_data(chat_id, invoice)
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_message(chat_id, f"Padhne mein dikkat aayi: {e}")


def process_manual_entry(chat_id, text):
    send_message(chat_id, f"{user_names[chat_id]}, samajh raha hoon...")
    try:
        invoice = extract_from_text(text)
        process_invoice_data(chat_id, invoice)
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_message(chat_id, f"Samajh nahi paya: {e}")


def process_correction(chat_id, text):
    try:
        received_qty = parse_received(text)
    except ValueError:
        send_message(chat_id, 'Samajh nahi aaya. Aise likho: "1:94, 3:10"')
        return
    conn = db.get_connection()
    issues = checker.check_invoice(conn, pending[chat_id], received_qty)
    send_message(chat_id, format_report_html(pending[chat_id], issues), parse_mode="HTML")


def save_incoming_photo(message):
    os.makedirs("bot_uploads", exist_ok=True)
    file_id = message["photo"][-1]["file_id"]
    image_path = f"bot_uploads/{file_id}.jpg"
    download_photo(file_id, image_path)
    return image_path


def handle_update(update):
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    print(f"DEBUG update: chat_id={chat_id} keys={list(message.keys())}", flush=True)
    if not chat_id:
        return
    if "text" not in message and "photo" not in message:
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
                process_photo(chat_id, pending_photo.pop(chat_id))
            return

        awaiting_name.add(chat_id)
        if "photo" in message:
            pending_photo[chat_id] = save_incoming_photo(message)
        send_message(chat_id, f"{time_greeting()}! Main {BOT_NAME} hoon. Pehle apna naam bata do?")
        return

    if "photo" in message:
        process_photo(chat_id, save_incoming_photo(message))

    elif "text" in message:
        text = message["text"].strip()

        if text == BTN_PHOTO:
            send_message(chat_id, "Theek hai, bill ki photo bhej do jab ready ho.")

        elif text == BTN_VENDOR:
            awaiting_vendor_name.add(chat_id)
            send_message(chat_id, "Vendor ka naam batao (jaise: Ayaz) — jab tak na badlo, saare bills usi ke maane jayenge.")

        elif text == BTN_MANUAL or text.lower() == "/manual":
            awaiting_manual_entry.add(chat_id)
            send_message(chat_id, 'Bina photo ke batao kya aaya, jaise: "5 Biscuit @10, 6 Maggi @132"')

        elif text.lower().startswith("/vendor"):
            name = text[len("/vendor"):].strip()
            if not name:
                send_message(chat_id, "Vendor ka naam bhi likho, jaise: /vendor Ayaz")
            else:
                vendor_override[chat_id] = name
                send_message(chat_id, f"Theek hai — jab tak na badlo, saare bills {name} ke maane jayenge.")

        elif chat_id in awaiting_vendor_name:
            awaiting_vendor_name.discard(chat_id)
            vendor_override[chat_id] = text
            send_message(chat_id, f"Theek hai — jab tak na badlo, saare bills {text} ke maane jayenge.", reply_markup=MAIN_MENU)

        elif text == BTN_HISTORY:
            awaiting_history_vendor.add(chat_id)
            send_message(chat_id, "Kis vendor ka hisaab dekhna hai? Naam batao.")

        elif chat_id in awaiting_history_vendor:
            awaiting_history_vendor.discard(chat_id)
            conn = db.get_connection()
            matched = db.find_supplier(conn, text)
            if not matched:
                send_message(chat_id, f"{text} ka koi bill mere paas nahi hai abhi.", reply_markup=MAIN_MENU)
            else:
                invoices = db.get_invoices_for_supplier(conn, matched)
                send_message(chat_id, format_vendor_history(matched, invoices), parse_mode="HTML", reply_markup=MAIN_MENU)

        elif chat_id in awaiting_manual_entry:
            awaiting_manual_entry.discard(chat_id)
            process_manual_entry(chat_id, text)

        elif chat_id in pending:
            process_correction(chat_id, text)

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
