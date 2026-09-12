"""Vision model call. Photo of a supplier invoice -> structured JSON.

SYSTEM_PROMPT is the single biggest lever on extraction accuracy — tune it
here, not the parsing code below it.
"""

import argparse
import base64
import json
import os
import sys

import requests

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are reading a photo of a paper invoice from a small Indian shop supplier \
(kirana, hardware, electrical, or pharmacy wholesaler). Extract exactly what is printed.

Rules:
1. Copy every number EXACTLY as printed, even if the arithmetic looks wrong. If a line says
   12 x 132 = 1684, output amount 1684 — do NOT correct it to 1584. Catching supplier math
   errors is the entire point of this tool; a "helpful" correction destroys that.
2. The photo may be tilted. Match each row's qty/rate/amount to its item name by READING
   ORDER (1st item goes with the 1st qty/rate/amount, 2nd with 2nd, ...), never by vertical
   pixel position — tilt shifts numbers up or down relative to the item names next to them.
3. If a field is unreadable (torn, blurry, handwriting you can't parse), set it to null and
   add its name to unreadable_fields. Never guess a number to fill a gap.
4. Output ONLY valid JSON, no prose, no markdown fences, matching this shape:

{
  "supplier_name": string or null,
  "invoice_number": string or null,
  "invoice_date": string or null,
  "items": [
    {"name": string, "qty": number or null, "rate": number or null, "amount": number or null}
  ],
  "sub_total": number or null,
  "tax": number or null,
  "grand_total": number or null,
  "confidence": number,        // your honest 0.0-1.0 estimate of how legible this photo was
  "unreadable_fields": [string]
}
"""

MOCK_INVOICE = {
    "supplier_name": "Sharma Traders",
    "invoice_number": "INV-2291",
    "invoice_date": "2026-09-01",
    "items": [
        {"name": "Tata Salt 1Kg", "qty": 96, "rate": 32, "amount": 3072},
        {"name": "Parle-G Biscuit 100g", "qty": 54, "rate": 10, "amount": 540},
        {"name": "Maggi Noodles 70g (12pk)", "qty": 12, "rate": 132, "amount": 1684},
        {"name": "Colgate Toothpaste 100g", "qty": 24, "rate": 45, "amount": 1080},
        {"name": "Lifebuoy Soap 125g", "qty": 48, "rate": 22, "amount": 1056},
        {"name": "Surf Excel 1Kg", "qty": 12, "rate": 118, "amount": 1416},
        {"name": "Good Day Biscuit 100g", "qty": 36, "rate": 12, "amount": 432},
        {"name": "Britannia Bread 400g", "qty": 20, "rate": 35, "amount": 700},
    ],
    "sub_total": 9980,
    "tax": 0,
    "grand_total": 9980,
    "confidence": 0.95,
    "unreadable_fields": [],
}

MOCK_ALL_OK_INVOICE = {
    **MOCK_INVOICE,
    "items": [
        {**item, "amount": round(item["qty"] * item["rate"], 2)}
        for item in MOCK_INVOICE["items"]
    ],
}


def _media_type(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp"}.get(ext, "image/jpeg")


def _api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set. export ANTHROPIC_API_KEY=sk-ant-...")
    return key


def list_models():
    resp = requests.get(
        ANTHROPIC_MODELS_URL,
        headers={"x-api-key": _api_key(), "anthropic-version": ANTHROPIC_VERSION},
    )
    resp.raise_for_status()
    for model in resp.json().get("data", []):
        print(model["id"])


def extract_invoice(image_path, model=DEFAULT_MODEL):
    """Real vision call. Returns the parsed invoice dict."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers={
            "x-api-key": _api_key(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": _media_type(image_path),
                        "data": image_b64,
                    }},
                    {"type": "text", "text": "Extract this invoice as JSON."},
                ],
            }],
        },
    )
    resp.raise_for_status()
    text = resp.json()["content"][0]["text"].strip()
    return json.loads(text)


def extract(image_path, mock=False, all_ok=False, model=DEFAULT_MODEL):
    if mock:
        return MOCK_ALL_OK_INVOICE if all_ok else MOCK_INVOICE
    return extract_invoice(image_path, model=model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract a supplier invoice photo to JSON.")
    parser.add_argument("image", nargs="?", help="Path to the invoice photo")
    parser.add_argument("--mock", action="store_true", help="Skip the API, return fixed test JSON")
    parser.add_argument("--all-ok", action="store_true", help="With --mock, return a JSON with no arithmetic trap")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--list-models", action="store_true", help="List available models and exit")
    args = parser.parse_args()

    if args.list_models:
        list_models()
        sys.exit(0)

    if not args.image:
        parser.error("image path is required unless --list-models is given")

    invoice = extract(args.image, mock=args.mock, all_ok=args.all_ok, model=args.model)
    print(json.dumps(invoice, indent=2))
