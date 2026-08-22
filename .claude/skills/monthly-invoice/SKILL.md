---
name: monthly-invoice
description: Generate the monthly usage-based invoice (HTML + PDF) for the Waddle Waddle NY booking system, billed by Simbino. Pulls the month's real AWS usage from the waddle-booking-live stack, applies the fixed rate card, adds optional feature add-ons, applies NY (Nassau County) sales tax, and outputs a PDF. Use when the user says "generate the monthly invoice", "bill the client", "monthly charge", "invoice June", or "/monthly-invoice".
---

# Monthly Invoice — Simbino → Waddle Waddle NY

Produces a usage-metered invoice PDF for the `waddle-booking-live` AWS stack (us-east-1).

## Run

```bash
python3 .claude/skills/monthly-invoice/generate_invoice.py [options]
```

| Option | Meaning |
|---|---|
| `--month YYYY-MM` | Billing month. Default: **last month**. |
| `--invoice-no INV-...` | Override invoice number. Default: `INV-<YYYY>-<MM>`. |
| `--addon "Name:Amount"` | Feature add-on line (repeatable). E.g. `--addon "SMS booking reminders:15.00"`. |
| `--out DIR` | Output folder. Default: `~/Documents/simbino-invoices` (outside the repo — see warning). |
| `--dry-run` | Print the computed table only; skip file/PDF generation. |

**Examples**
```bash
# Preview last month's numbers
python3 .claude/skills/monthly-invoice/generate_invoice.py --dry-run
# Bill June 2026 with a new feature add-on
python3 .claude/skills/monthly-invoice/generate_invoice.py --month 2026-06 \
    --addon "SMS booking reminders:15.00"
```

## Rate card (fixed — edit constants at the top of `generate_invoice.py`)

| Metered service | Source metric | Rate |
|---|---|---|
| Booking transactions | DynamoDB items created that month | $2.50 / booking |
| API requests | API Gateway `Count` (ApiId `ew68um0lpa`) | $0.008 / req |
| Compute execution | Lambda `Invocations`, summed over the 4 stack functions | $0.0006 / invoke |
| Booking confirmation emails | 1 per booking | $0.015 / email |
| Database operations | DynamoDB `SuccessfulRequestLatency` SampleCount, all ops | $0.0006 / op |
| **Feature add-ons** | `--addon` flags | fixed amount each |
| **NY sales tax** | Nassau County | 8.625% on subtotal |

## Payment section (added Aug 2026)

Every invoice ends with a **Payment — Zelle** box: pay-to name **SIMBINO LLC**, Zelle email
`simbinoart@gmail.com`, the amount due, a memo instruction (invoice number), and the Zelle QR
(`simbino-zelle-qr.png`, kept next to the script, embedded as base64 so the PDF is self-contained).
If the Zelle account changes, replace the PNG and the `ZELLE_*` constants at the top of the script.

Notes baked into the logic:
- **Lambda is summed per stack-function**, not account-wide, so other projects in the AWS account don't inflate the bill.
- **Emails = booking count** (one customer confirmation per booking), per the client's billing agreement — not raw SES send volume.
- Stripe processing fees are charged to the client's own Stripe account and are **not** on this invoice.

## ⚠️ Do not commit generated invoices

This repo is published to **GitHub Pages (public)**. Generated invoices contain client info, so the script writes them **outside the repo** (`~/Documents/simbino-invoices`) by default. Never move an invoice PDF/HTML into the repo tree.

## Google Drive mirror

After the PDF is written, the script uploads it to **Drive → `Waddle Waddle Invoices/`** via the
rclone remote `gdrive` (`~/bin/rclone`, same transport as the rednote-insight pipeline). Best-effort
and idempotent (rclone skips unchanged files): a missing rclone or a failed upload only warns —
the local PDF is always the source of truth.

## Requirements
- AWS CLI authenticated for account `642662060437` (read-only CloudWatch + DynamoDB).
- Google Chrome or Chromium (used headless for `--print-to-pdf`). Resolved automatically: macOS app bundle, then `google-chrome` / `chromium` / `chromium-browser` on PATH. On Linux, `sudo apt install chromium` if none present.
- Optional: `~/bin/rclone` with remote `gdrive` configured (for the Drive mirror).
