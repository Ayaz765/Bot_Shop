"""CLI. Wires extract.py -> received-qty input -> checker.py -> db.py together."""

import argparse
import json

import checker
import db
import extract


def parse_received(received_str):
    """'1:94, 3:10' -> {1: 94.0, 3: 10.0}. Empty/missing items default to matching the bill."""
    result = {}
    for part in received_str.split(","):
        part = part.strip()
        if not part:
            continue
        idx_str, qty_str = part.split(":")
        result[int(idx_str.strip())] = float(qty_str.strip())
    return result


def format_report(invoice, issues):
    header = invoice.get("supplier_name") or "Unknown Supplier"
    if invoice.get("invoice_number"):
        header += f" — {invoice['invoice_number']}"

    lines = [header]
    if not issues:
        lines.append("Sab sahi hai. Koi gadbad nahi mili.")
    else:
        for issue in issues:
            lines.append(f"[!] {issue['msg']}")

    total = round(sum(issue["loss"] for issue in issues), 2)
    lines.append(f"\nTOTAL PHANSA PAISA: Rs{total:.2f}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Check a supplier invoice photo for money stuck.")
    parser.add_argument("image", help="Path to the invoice photo")
    parser.add_argument("--mock", action="store_true", help="Skip the API, use fixed test JSON")
    parser.add_argument("--all-ok", action="store_true", help="With --mock, use a bill with no arithmetic trap")
    parser.add_argument("--provider", choices=["anthropic", "gemini"], default="anthropic", help="Vision API to use for real extraction")
    parser.add_argument("--model", default=None, help="Defaults to the provider's standard model")
    parser.add_argument("--received", default="", help='Items that differ from the bill, e.g. "1:94, 3:10"')
    parser.add_argument("--json", action="store_true", help="Print raw issues as JSON instead of the report")
    parser.add_argument("--no-save", action="store_true", help="Don't save this invoice to the database")
    args = parser.parse_args()

    invoice = extract.extract(args.image, mock=args.mock, all_ok=args.all_ok, provider=args.provider, model=args.model)
    received_qty = parse_received(args.received)

    conn = db.get_connection()
    issues = checker.check_invoice(conn, invoice, received_qty)

    if args.json:
        print(json.dumps(issues, indent=2))
    else:
        print(format_report(invoice, issues))

    if not args.no_save:
        db.save_invoice(
            conn,
            invoice.get("supplier_name"),
            invoice.get("invoice_number"),
            invoice.get("invoice_date"),
            invoice.get("grand_total"),
            invoice.get("items", []),
            issues,
        )


if __name__ == "__main__":
    main()
