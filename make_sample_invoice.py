"""Generates a fake supplier invoice image for testing extract.py without a
real photo. --messy simulates a bad phone photo: tilt, uneven light, blur,
grain, low-quality JPEG. Same invoice data as extract.py's MOCK_INVOICE.
"""

import argparse
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

INVOICE = {
    "supplier_name": "Sharma Traders",
    "invoice_number": "INV-2291",
    "invoice_date": "2026-09-01",
    "items": [
        {"name": "Tata Salt 1Kg", "qty": 96, "rate": 32, "amount": 3072},
        {"name": "Parle-G Biscuit 100g", "qty": 54, "rate": 10, "amount": 540},
        {"name": "Maggi Noodles 70g (12pk)", "qty": 12, "rate": 132, "amount": 1684},  # arithmetic trap: should be 1584
        {"name": "Colgate Toothpaste 100g", "qty": 24, "rate": 45, "amount": 1080},
        {"name": "Lifebuoy Soap 125g", "qty": 48, "rate": 22, "amount": 1056},
        {"name": "Surf Excel 1Kg", "qty": 12, "rate": 118, "amount": 1416},
        {"name": "Good Day Biscuit 100g", "qty": 36, "rate": 12, "amount": 432},
        {"name": "Britannia Bread 400g", "qty": 20, "rate": 35, "amount": 700},
    ],
    "sub_total": 9980,
    "tax": 0,
    "grand_total": 9980,
}


def _font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_invoice():
    width, height = 900, 700
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    title_font = _font(30)
    header_font = _font(20)
    body_font = _font(18)

    y = 30
    draw.text((width // 2 - 110, y), INVOICE["supplier_name"].upper(), font=title_font, fill="black")
    y += 50
    draw.text((40, y), f"Invoice No: {INVOICE['invoice_number']}", font=header_font, fill="black")
    draw.text((500, y), f"Date: {INVOICE['invoice_date']}", font=header_font, fill="black")
    y += 40

    for label, x in [("S.No", 40), ("Item", 90), ("Qty", 480), ("Rate", 570), ("Amount", 680)]:
        draw.text((x, y), label, font=header_font, fill="black")
    y += 30
    draw.line((40, y, 830, y), fill="black", width=2)
    y += 15

    for i, item in enumerate(INVOICE["items"], start=1):
        draw.text((40, y), str(i), font=body_font, fill="black")
        draw.text((90, y), item["name"], font=body_font, fill="black")
        draw.text((480, y), str(item["qty"]), font=body_font, fill="black")
        draw.text((570, y), str(item["rate"]), font=body_font, fill="black")
        draw.text((680, y), str(item["amount"]), font=body_font, fill="black")
        y += 35

    y += 15
    draw.line((40, y, 830, y), fill="black", width=2)
    y += 20
    for label, value in [("Sub Total:", INVOICE["sub_total"]), ("Tax:", INVOICE["tax"]), ("Grand Total:", INVOICE["grand_total"])]:
        draw.text((570, y), label, font=header_font, fill="black")
        draw.text((680, y), str(value), font=header_font, fill="black")
        y += 30

    return img


def make_messy(img, seed=None):
    """Tilt, dim, blur, add grain, then re-compress — like a rushed phone photo."""
    rng = random.Random(seed)

    img = img.rotate(rng.uniform(-8, 8), expand=True, fillcolor="white")

    brightness = rng.uniform(0.55, 0.85)
    img = Image.eval(img, lambda p: int(p * brightness))

    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.8, 2.2)))

    noise = Image.effect_noise(img.size, rng.uniform(20, 45)).convert("RGB")
    img = Image.blend(img, noise, rng.uniform(0.08, 0.18))

    return img, rng.randint(35, 60)  # (image, jpeg quality to save at)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a fake supplier invoice image.")
    parser.add_argument("--out", default="sample_invoice.jpg")
    parser.add_argument("--messy", action="store_true", help="Simulate a bad phone photo")
    parser.add_argument("--seed", type=int, default=None, help="Reproducible messiness")
    args = parser.parse_args()

    invoice_img = render_invoice()
    if args.messy:
        invoice_img, quality = make_messy(invoice_img, seed=args.seed)
        invoice_img.save(args.out, "JPEG", quality=quality)
    else:
        invoice_img.save(args.out, "JPEG", quality=95)

    print(f"Wrote {args.out}")
