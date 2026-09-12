"""Dev utility: split a scanned PDF (many invoices/DI/bilty in one file) into
per-page JPGs, so each page can be fed to extract.py one at a time — the tool
is built around one photo = one document, not a bulk PDF.
"""

import argparse
import os

import pymupdf as fitz


def parse_pages(spec, total):
    """'1-10,15' -> 0-based page indices. None/empty -> every page."""
    if not spec:
        return list(range(total))
    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            indices.update(range(int(start) - 1, int(end)))
        else:
            indices.add(int(part) - 1)
    return sorted(i for i in indices if 0 <= i < total)


def split_pdf(pdf_path, out_dir, pages=None, dpi=200):
    doc = fitz.open(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    indices = parse_pages(pages, len(doc))
    written = []
    for i in indices:
        pix = doc[i].get_pixmap(matrix=matrix)
        out_path = os.path.join(out_dir, f"page_{i + 1:03d}.jpg")
        pix.save(out_path)
        written.append(out_path)

    doc.close()
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Split a PDF into per-page JPGs for testing extract.py.")
    parser.add_argument("pdf", help="Path to the PDF")
    parser.add_argument("--out", default="real_bills", help="Output folder")
    parser.add_argument("--pages", default=None, help='e.g. "1-10" or "1,5,9". Default: all pages')
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    files = split_pdf(args.pdf, args.out, args.pages, args.dpi)
    print(f"Wrote {len(files)} pages to {args.out}/")
