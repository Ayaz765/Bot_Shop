# CLAUDE.md

Project context for Claude Code. Read this before making any changes.

---

## What this is

**Bill Checker** — an AI tool for small Indian shopkeepers (kirana, hardware,
electrical, pharmacy, small wholesalers).

The shopkeeper photographs a supplier's invoice. The tool reads it, compares it
against what actually arrived, and reports how much money is stuck — short
quantities, arithmetic errors, silent rate hikes, duplicate bills.

Current state: **working CLI prototype.** Not a product yet.

### Why this exists

The customer does not want AI. They want to stop losing money. Every decision
in this codebase should be judged against: *does this help a shopkeeper catch
money he would otherwise have lost?*

A shopkeeper receiving a 30-line invoice never checks the arithmetic. Suppliers
know this. That gap is the entire business.

### The hardest problem (do not lose sight of this)

Reading the invoice is the easy part. Getting **"what actually arrived"** is
hard — shopkeepers have no GRN system and write nothing down.

So the received-quantity input must stay near-zero effort. Current design: the
user names only the items that were short or extra (`1:94, 3:10`); everything
else defaults to correct. On a 30-item bill he touches two things.

**Never make this input heavier.** Any change that adds steps here kills daily
usage, and without daily usage there is no product.

---

## Architecture

```
photo → extract.py → invoice JSON
                          ↓
        received qty ← main.py (asks user)
                          ↓
                     checker.py  ← db.py (rate history, past invoices)
                          ↓
                   Hinglish report
```

| File | Role |
|---|---|
| `extract.py` | Vision model call. Image → structured JSON. `SYSTEM_PROMPT` lives here and is the single biggest lever on accuracy. |
| `checker.py` | The rules engine. **This is the product's brain.** |
| `db.py` | SQLite. Rate history per supplier + past invoices + running mismatch totals. |
| `main.py` | CLI. Wires it together, handles received-qty input. |
| `make_sample_invoice.py` | Generates a fake supplier invoice for testing. |

Deliberately not present, and should stay absent for now: WhatsApp bot, web
app, mobile app, login, payments, any trained model of our own.

---

## Rules currently implemented

All in `checker.py`, one function each, so any rule can be disabled
independently when it misfires on real bills.

| Rule | Detects |
|---|---|
| `QTY_SHORT` | Invoiced 100, received 94 |
| `QTY_EXTRA` | More arrived than billed (supplier will ask for money later) |
| `LINE_MATH` | qty × rate ≠ line amount |
| `SUBTOTAL_MATH` | Sum of lines ≠ stated sub total |
| `GRANDTOTAL_MATH` | Sub total + tax ≠ grand total |
| `RATE_HIKE` | Rate more than 5% above this supplier's historical median |
| `DUPLICATE` | Same supplier + same invoice number seen before |
| `LOW_CONFIDENCE` | Photo too poor to trust — say so instead of guessing |

---

## Conventions — follow these

**Output language is Hinglish (Roman script).** The user is a shopkeeper, not
an accountant. `"Bill mein 100 PCS, aaya sirf 94. 6 kam."` — not `"Quantity
variance detected on line item 1"`. Code, comments, and variable names stay in
English.

**Never silently correct the supplier's arithmetic.** In `extract.py`, numbers
must be copied exactly as printed. If the bill says 12 × ₹132 = ₹1,684, extract
1684. Detecting that error *is the product*. A model that "helpfully" fixes it
destroys the core feature.

**Prefer missing data over invented data.** If a field is unreadable, return
`null` and list it in `unreadable_fields`. One confidently wrong number costs
more trust than ten honest "couldn't read this".

**False alarms are the main risk.** A shopkeeper who gets one wrong alert stops
using the tool. Catching 5 real problems beats flagging 20 with 8 wrong. When
in doubt, loosen the threshold rather than tighten it.

**One rule = one function.** New checks follow the shape of `check_line_math`:
take what they need, return a list of issue dicts with
`type`, `severity`, `item`, `msg`, `loss`. Never fold two checks together.

**Keep it small.** No new dependencies beyond `requests` and `Pillow` without
asking. No frameworks. No abstraction layers for problems we don't have yet.

Tunable thresholds at the top of `checker.py` — change these, not the logic,
when tuning against real bills:

```python
PAISA_TOLERANCE = 1.0    # ignore rounding below this
RATE_HIKE_PCT = 5.0      # % rise before flagging
MIN_FLAG_AMOUNT = 5.0    # don't alert on trivial amounts
```

---

## How to run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# no API key needed — dummy extraction, full matching logic
python main.py sample_invoice.jpg --mock --received "1:94, 2:48"

# real vision extraction
export ANTHROPIC_API_KEY=sk-ant-...
python extract.py --list-models
python main.py sample_invoice.jpg
```

`--mock` skips the API and returns fixed JSON. Use it for any work on
`checker.py`, `db.py`, or `main.py` — it's instant and free.

Flags: `--mock`, `--all-ok`, `--received "1:94"`, `--json`, `--no-save`

### Regression check — run after every change

```bash
rm -f billcheck.db
python main.py sample_invoice.jpg --mock --received "1:94, 2:48"
```

Expected output, every time:

- Maggi line: 12 × ₹132 = ₹1,584 but bill says ₹1,684 → ₹100
- Tata Salt: 2 short → ₹64
- Parle-G: 6 short → ₹60
- **Total: ₹224**

If this total changes and you did not intend it, something broke. Fix it before
moving on.

Note `sample_invoice.jpg` contains an intentional arithmetic trap (the Maggi
line). Do not "fix" the sample invoice.

---

## Working style

The owner is learning this codebase while building it. Code he cannot explain
is worse than no code, because he cannot debug it when a real invoice breaks it
at a customer's shop.

- One change at a time. Do not build several features in one pass.
- Explain the *why* in Hinglish, briefly, after each change.
- Show the regression check result after touching `checker.py`.
- If a request would add a framework, a new service, or more than ~100 lines,
  say so and propose something smaller first.
- Push back when a request conflicts with the notes above, especially anything
  that makes the received-qty input heavier or hides supplier errors.

---

## Next priority

**Accuracy on 20 real invoices — nothing else.**

The immediate task is collecting real supplier bills (printed, thermal,
handwritten, crumpled, badly lit) and measuring how often extraction is
completely correct. Target: 85%+. Below that, no feature matters.

After that, in order: tune thresholds on real data → WhatsApp delivery →
5 free pilot shops for one month → charge ₹299/month → only then think about
a specialised model trained on the accumulated dataset.

Do not jump ahead of this list. Suggest the next step, not the last one.
