#!/usr/bin/env python3
"""Generate the monthly usage-based invoice PDF for Simbino -> Waddle Waddle NY.

Pulls the given month's real usage from AWS (CloudWatch + DynamoDB) for the
`waddle-booking-live` stack, applies the fixed rate card, adds any feature
add-ons, applies NY (Nassau County) sales tax, and renders an HTML + PDF invoice.

Usage:
  python3 generate_invoice.py [--month YYYY-MM] [--invoice-no INV-...] \
      [--addon "Name:Amount"] ... [--out DIR] [--dry-run]

Examples:
  python3 generate_invoice.py --month 2026-06
  python3 generate_invoice.py --month 2026-06 --addon "SMS booking reminders:15.00"
  python3 generate_invoice.py --dry-run          # last month, print table only
"""
import argparse
import base64
import datetime
import html
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal, ROUND_HALF_UP

# --- Fixed infra (waddle-booking-live stack, us-east-1) ---
REGION = "us-east-1"
API_ID = "ew68um0lpa"
TABLE = "waddle-booking-live-bookings"
FUNCTIONS = [
    "waddle-booking-live-Admin",
    "waddle-booking-live-Availability",
    "waddle-booking-live-CreateCheckout",
    "waddle-booking-live-StripeWebhook",
]
DB_OPS = ["GetItem", "PutItem", "UpdateItem", "DeleteItem", "Query", "Scan",
          "BatchGetItem", "BatchWriteItem"]

# --- Rate card (same rates) ---
RATE_BOOKING = 2.50      # per booking transaction
RATE_API = 0.008         # per API Gateway request
RATE_LAMBDA = 0.0006     # per Lambda invocation
RATE_EMAIL = 0.015       # per booking confirmation email (1 per booking)
RATE_DB_OP = 0.0006      # per DynamoDB operation
TAX_RATE = 0.08625       # Nassau County, NY
TAX_LABEL = "NY sales tax (Nassau County, 8.625%)"

PROVIDER = {"name": "Simbino", "email": "info@simbino.com"}
CLIENT = {"name": "Waddle Waddle NY", "email": "info@waddlewaddleny.com"}

# --- Payment (Zelle) ---
ZELLE_NAME = "SIMBINO LLC"
ZELLE_TOKEN = "simbinoart@gmail.com"
ZELLE_QR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "simbino-zelle-qr.png")

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Google Drive mirror (rclone remote "gdrive", same transport as the
# rednote-insight pipeline). Best-effort: a missing rclone/remote or a failed
# upload warns but never fails the invoice run.
RCLONE = os.path.expanduser("~/bin/rclone")
DRIVE_FOLDER = "Waddle Waddle Invoices"
RCLONE_FLAGS = ["--timeout", "60s", "--contimeout", "15s", "--transfers", "2",
                "--low-level-retries", "10", "--retries", "2"]


def aws_text(*args):
    r = subprocess.run(["aws", *args, "--region", REGION, "--output", "text"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def cw_sum(namespace, metric, dims, start, end, stat="Sum"):
    """CloudWatch sum over [start, end) using daily buckets (avoids large-period bleed)."""
    period = 86400
    cmd = ["cloudwatch", "get-metric-statistics",
           "--namespace", namespace, "--metric-name", metric,
           "--start-time", start.strftime("%Y-%m-%dT00:00:00Z"),
           "--end-time", end.strftime("%Y-%m-%dT00:00:00Z"),
           "--period", str(period), "--statistics", stat,
           "--query", "Datapoints[].%s" % stat]
    if dims:
        cmd += ["--dimensions"] + ["Name=%s,Value=%s" % (k, v) for k, v in dims]
    out = aws_text(*cmd)
    return sum(float(x) for x in out.split() if x not in ("", "None"))


def gather_usage(start, end):
    api = cw_sum("AWS/ApiGateway", "Count", [("ApiId", API_ID)], start, end)
    lam = sum(cw_sum("AWS/Lambda", "Invocations", [("FunctionName", fn)], start, end)
              for fn in FUNCTIONS)
    dbops = sum(cw_sum("AWS/DynamoDB", "SuccessfulRequestLatency",
                       [("TableName", TABLE), ("Operation", op)], start, end,
                       stat="SampleCount") for op in DB_OPS)
    # Bookings created this month (createdAt prefix) — small table, single scan.
    out = subprocess.run(
        ["aws", "dynamodb", "scan", "--table-name", TABLE,
         "--projection-expression", "createdAt", "--region", REGION, "--output", "json"],
        capture_output=True, text=True).stdout
    items = json.loads(out).get("Items", []) if out.strip() else []
    prefix = start.strftime("%Y-%m")
    bookings = sum(1 for i in items if i.get("createdAt", {}).get("S", "").startswith(prefix))
    return {"api": int(api), "lambda": int(lam), "db": int(dbops), "bookings": bookings}


def build_lines(u, addons):
    """Return metered lines and add-on lines as (title, desc, usage, rate_label, amount)."""
    emails = u["bookings"]  # 1 confirmation email per booking
    metered = [
        ("Booking transactions processed",
         "Completed & pending bookings captured, with Stripe deposit checkout and payment reconciliation.",
         u["bookings"], "$%.2f / booking" % RATE_BOOKING, r2(u["bookings"] * RATE_BOOKING, 2)),
        ("API requests (API Gateway)",
         "Availability, checkout, admin & webhook API calls served.",
         u["api"], "$%.3f / req" % RATE_API, r2(u["api"] * RATE_API, 2)),
        ("Compute execution (AWS Lambda)",
         "Serverless function invocations backing every request and event.",
         u["lambda"], "$%.4f / invoke" % RATE_LAMBDA, r2(u["lambda"] * RATE_LAMBDA, 2)),
        ("Booking confirmation emails (AWS SES)",
         "One customer confirmation email sent per booking.",
         emails, "$%.3f / email" % RATE_EMAIL, r2(emails * RATE_EMAIL, 2)),
        ("Database operations (DynamoDB)",
         "Availability queries plus booking reads/writes served.",
         u["db"], "$%.4f / op" % RATE_DB_OP, r2(u["db"] * RATE_DB_OP, 2)),
    ]
    addon_lines = [(name, "Feature add-on.", "—", "—", r2(amt, 2))
                   for name, amt in addons]
    return metered, addon_lines


def r2(x, ndigits=2):
    """Round to cents, half-up (commercial), after clearing binary-float noise."""
    d = Decimal(str(x)).quantize(Decimal("0.000001"))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def money(x):
    return "${:,.2f}".format(x)


def qty(x):
    return "{:,}".format(x) if isinstance(x, int) else str(x)


def row_html(title, desc, usage, rate_label, amount):
    return """      <tr>
        <td>
          <div class="desc-title">{title}</div>
          <div class="desc-sub">{desc}</div>
        </td>
        <td class="num">{usage}</td>
        <td class="num">{rate}</td>
        <td class="num">{amount}</td>
      </tr>""".format(title=html.escape(title), desc=html.escape(desc),
                      usage=qty(usage), rate=html.escape(rate_label), amount=money(amount))


def render_html(inv_no, issued, period_label, metered, addon_lines, subtotal, tax, total):
    rows = "\n".join(row_html(*r) for r in metered)
    if addon_lines:
        rows += ("\n      <tr><td colspan=\"4\" style=\"padding:16px 0 4px;font-size:11px;"
                 "text-transform:uppercase;letter-spacing:.08em;color:#6b7280;"
                 "border:none;\">Feature add-ons</td></tr>\n")
        rows += "\n".join(row_html(*r) for r in addon_lines)
    usage_note = ("Charges reflect metered platform usage for %s. Stripe processing fees on "
                  "customer bookings are charged directly to the client's own Stripe account "
                  "and are not included in this invoice." % period_label)
    with open(ZELLE_QR_FILE, "rb") as f:
        zelle_qr_uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    return TEMPLATE.format(
        inv_no=html.escape(inv_no), issued=html.escape(issued),
        period=html.escape(period_label), rows=rows,
        provider_name=html.escape(PROVIDER["name"]), provider_email=html.escape(PROVIDER["email"]),
        client_name=html.escape(CLIENT["name"]), client_email=html.escape(CLIENT["email"]),
        subtotal=money(subtotal), tax_label=html.escape(TAX_LABEL), tax=money(tax),
        total=money(total), usage_note=html.escape(usage_note),
        zelle_name=html.escape(ZELLE_NAME), zelle_token=html.escape(ZELLE_TOKEN),
        zelle_qr=zelle_qr_uri, total_plain=money(total))


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice {inv_no} — Simbino</title>
<style>
  :root {{ --ink:#1a1a1a; --muted:#6b7280; --line:#e5e7eb; --accent:#0f766e; --bg:#fff; }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; padding:0; background:#f3f4f6; color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }}
  .sheet {{ max-width:820px; margin:32px auto; background:var(--bg); padding:56px 60px;
    box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  header {{ display:flex; justify-content:space-between; align-items:flex-start;
    border-bottom:2px solid var(--accent); padding-bottom:24px; margin-bottom:32px; }}
  .brand h1 {{ margin:0; font-size:26px; letter-spacing:-.02em; }}
  .brand .tag {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  .doc {{ text-align:right; }}
  .doc .word {{ font-size:28px; font-weight:700; letter-spacing:.14em; color:var(--accent);
    text-transform:uppercase; margin:0; }}
  .doc .meta {{ font-size:13px; color:var(--muted); margin-top:8px; line-height:1.7; }}
  .doc .meta b {{ color:var(--ink); }}
  .parties {{ display:flex; gap:48px; margin-bottom:36px; }}
  .parties .col {{ flex:1; }}
  .label {{ font-size:11px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted);
    margin-bottom:8px; }}
  .parties .name {{ font-weight:600; font-size:15px; }}
  .parties .line {{ font-size:13px; color:var(--muted); line-height:1.7; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:8px; }}
  thead th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.08em;
    color:var(--muted); border-bottom:1px solid var(--line); padding:0 0 12px; }}
  thead th.num, tbody td.num {{ text-align:right; }}
  tbody td {{ padding:16px 0; border-bottom:1px solid var(--line); vertical-align:top; font-size:14px; }}
  tbody td .desc-title {{ font-weight:600; }}
  tbody td .desc-sub {{ color:var(--muted); font-size:12.5px; margin-top:5px; line-height:1.6; max-width:430px; }}
  .totals {{ margin-left:auto; width:320px; margin-top:20px; font-size:14px; }}
  .totals .row {{ display:flex; justify-content:space-between; padding:8px 0; }}
  .totals .row.grand {{ border-top:2px solid var(--ink); margin-top:6px; padding-top:14px;
    font-size:18px; font-weight:700; }}
  .totals .row.grand .amt {{ color:var(--accent); }}
  .payment {{ display:flex; align-items:center; gap:24px; margin-top:40px;
    border:1px solid var(--line); border-left:3px solid var(--accent);
    padding:20px 24px; background:#fafafa; }}
  .payment .info {{ flex:1; }}
  .payment .info .head {{ font-size:11px; text-transform:uppercase; letter-spacing:.1em;
    color:var(--accent); font-weight:700; margin-bottom:8px; }}
  .payment .info .line {{ font-size:13px; line-height:1.8; }}
  .payment .info .line b {{ font-weight:600; }}
  .payment .info .hint {{ font-size:12px; color:var(--muted); margin-top:6px; line-height:1.6; }}
  .payment img {{ width:120px; height:120px; display:block; }}
  .notes {{ margin-top:44px; border-top:1px solid var(--line); padding-top:24px;
    font-size:12.5px; color:var(--muted); line-height:1.7; }}
  .notes b {{ color:var(--ink); }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; margin-top:40px; }}
  @media print {{ body {{ background:#fff; }} .sheet {{ box-shadow:none; margin:0; max-width:none; padding:40px; }} }}
</style></head>
<body><div class="sheet">
  <header>
    <div class="brand"><h1>{provider_name}</h1><div class="tag">Software &amp; Managed Hosting</div></div>
    <div class="doc"><p class="word">Invoice</p>
      <div class="meta"><div><b>No.</b> {inv_no}</div><div><b>Issued</b> {issued}</div>
        <div><b>Due</b> Upon receipt</div></div></div>
  </header>
  <div class="parties">
    <div class="col"><div class="label">From</div><div class="name">{provider_name}</div>
      <div class="line">{provider_email}</div></div>
    <div class="col"><div class="label">Bill to</div><div class="name">{client_name}</div>
      <div class="line">{client_email}</div></div>
  </div>
  <table>
    <thead><tr><th>Metered service &mdash; {period}</th><th class="num">Usage</th>
      <th class="num">Unit rate</th><th class="num">Amount</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
  <div class="totals">
    <div class="row"><span>Subtotal</span><span>{subtotal}</span></div>
    <div class="row"><span>{tax_label}</span><span>{tax}</span></div>
    <div class="row grand"><span>Total due</span><span class="amt">{total}</span></div>
  </div>
  <div class="payment">
    <div class="info">
      <div class="head">Payment &mdash; Zelle</div>
      <div class="line">Send <b>{total_plain}</b> via Zelle to <b>{zelle_name}</b></div>
      <div class="line">Zelle email: <b>{zelle_token}</b></div>
      <div class="hint">Scan the QR code with your banking app, or add the Zelle email above
        as a recipient. Please include invoice number <b>{inv_no}</b> in the payment memo.</div>
    </div>
    <img src="{zelle_qr}" alt="Zelle QR code — {zelle_name}">
  </div>
  <div class="notes">
    <p><b>Terms</b> &mdash; Usage-based service, billed monthly in arrears. {usage_note}</p>
    <p>Thank you for your business.</p>
  </div>
  <footer>{provider_name} &middot; {provider_email}</footer>
</div></body></html>"""


def month_range(month):
    y, m = int(month[:4]), int(month[5:7])
    start = datetime.date(y, m, 1)
    end = datetime.date(y + (m // 12), (m % 12) + 1, 1)
    return start, end


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--month", help="YYYY-MM (default: last month)")
    p.add_argument("--invoice-no")
    p.add_argument("--addon", action="append", default=[], metavar="NAME:AMOUNT",
                   help='Feature add-on line, repeatable, e.g. "SMS reminders:15.00"')
    p.add_argument("--out", default=os.path.expanduser("~/Documents/simbino-invoices"))
    p.add_argument("--dry-run", action="store_true", help="Print table, skip PDF")
    # Optional usage overrides (force a specific figure instead of the measured one)
    p.add_argument("--bookings", type=int)
    p.add_argument("--api", type=int)
    p.add_argument("--lambda", type=int, dest="lam")
    p.add_argument("--db", type=int)
    a = p.parse_args()

    if not a.month:
        today = datetime.date.today()
        first = today.replace(day=1)
        last = first - datetime.timedelta(days=1)
        a.month = last.strftime("%Y-%m")
    start, end = month_range(a.month)
    period_label = start.strftime("%B %-d") + " – " + (end - datetime.timedelta(days=1)).strftime("%-d, %Y")
    inv_no = a.invoice_no or ("INV-%s" % a.month)

    addons = []
    for s in a.addon:
        name, _, amt = s.rpartition(":")
        addons.append((name.strip(), float(amt)))

    print("Pulling %s usage from AWS ..." % a.month, file=sys.stderr)
    u = gather_usage(start, end)
    for key, val in (("bookings", a.bookings), ("api", a.api), ("lambda", a.lam), ("db", a.db)):
        if val is not None:
            u[key] = val
    metered, addon_lines = build_lines(u, addons)
    subtotal = r2(sum(r[4] for r in metered) + sum(r[4] for r in addon_lines), 2)
    tax = r2(subtotal * TAX_RATE, 2)
    total = r2(subtotal + tax, 2)

    print("\n%s  (%s)" % (inv_no, period_label))
    for t, _, us, rl, am in metered + addon_lines:
        print("  %-38s %10s  %-16s %8s" % (t[:38], qty(us), rl, money(am)))
    print("  %-38s %10s  %-16s %8s" % ("Subtotal", "", "", money(subtotal)))
    print("  %-38s %10s  %-16s %8s" % (TAX_LABEL, "", "", money(tax)))
    print("  %-38s %10s  %-16s %8s" % ("TOTAL DUE", "", "", money(total)))

    if a.dry_run:
        return

    os.makedirs(a.out, exist_ok=True)
    issued = datetime.date.today().strftime("%b %-d, %Y")
    html_doc = render_html(inv_no, issued, period_label, metered, addon_lines, subtotal, tax, total)
    html_path = os.path.join(a.out, inv_no + ".html")
    pdf_path = os.path.join(a.out, inv_no + ".pdf")
    with open(html_path, "w") as f:
        f.write(html_doc)
    # Fresh profile dir + hard timeout: Chrome headless can hang after writing the
    # PDF when the user's main Chrome is running (observed 2026-08-22).
    profile = tempfile.mkdtemp(prefix="invoice-chrome-")
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        "--user-data-dir=" + profile,
                        "--print-to-pdf=" + pdf_path, "file://" + html_path],
                       capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        pass  # PDF is written before the hang; verified below
    if not os.path.exists(pdf_path):
        sys.exit("Chrome failed to produce %s" % pdf_path)
    print("\nWrote: %s" % pdf_path, file=sys.stderr)
    print(pdf_path)
    upload_to_drive(pdf_path)


def upload_to_drive(pdf_path):
    if not os.path.exists(RCLONE):
        print("[gdrive] rclone not installed (~/bin/rclone) — skipping Drive upload",
              file=sys.stderr)
        return
    r = subprocess.run([RCLONE, "copy", os.path.dirname(pdf_path),
                        "gdrive:" + DRIVE_FOLDER,
                        "--include", os.path.basename(pdf_path), *RCLONE_FLAGS],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[gdrive] upload failed: %s" % (r.stderr or "").strip()[-300:],
              file=sys.stderr)
    else:
        print("[gdrive] %s -> Drive/%s ✓" % (os.path.basename(pdf_path), DRIVE_FOLDER),
              file=sys.stderr)


if __name__ == "__main__":
    main()
