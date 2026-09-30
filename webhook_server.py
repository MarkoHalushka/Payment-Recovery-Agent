"""
WEBHOOK SERVER — production version (hardened).

Real Stripe signature verification, real secrets from environment
variables (never hardcoded), real email sending via Resend, real
idempotency protection, real error handling.

=======================================================================
KNOWN LIMITATIONS — read before connecting a real client
=======================================================================
1. SINGLE-TENANT: this handles ONE business (set via env vars below).
   Serving multiple clients from one deployment requires a real
   database keyed by Stripe account/webhook ID — not built yet.
   For your first pilot client, this is fine: one client = one
   deployment, or one client = these env vars set for them.

2. SQLite PERSISTENCE: tracking.db survives sleep/wake cycles on
   Render's free tier, but is WIPED on every redeploy (new code push).
   Fine for a pilot. For real multi-week production use, move to a
   real hosted database (e.g. Render's own Postgres) before this
   matters — i.e. before your tracking history needs to survive you
   shipping code updates.

3. CURRENCY: formats using the invoice's real currency code (see
   format_amount below) rather than assuming USD — but symbol display
   is generic (e.g. "12.00 EUR") rather than "€12.00". Fine for now,
   worth prettifying later if a client bills in a non-USD currency.

4. DECLINE REASON: Stripe's invoice.payment_failed payload does not
   reliably include a human-readable decline reason at the invoice
   level (it lives deeper, on the associated PaymentIntent/Charge).
   We pass a generic "a payment issue" to the email generator rather
   than guess at an unverified field and risk it being wrong. This
   is a reasonable place to improve later, not a bug now.
=======================================================================
"""

import os
import sys
import sqlite3
import stripe
import resend
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)


# =======================================================================
# STARTUP VALIDATION — fail loudly and clearly if required config is
# missing, instead of crashing later with a confusing KeyError deep in
# a request handler.
# =======================================================================
REQUIRED_ENV_VARS = ["ANTHROPIC_API_KEY", "STRIPE_API_KEY", "STRIPE_WEBHOOK_SECRET", "RESEND_API_KEY"]

missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
if missing:
    print(f"FATAL: missing required environment variables: {', '.join(missing)}")
    print("Set these in Render's Environment tab before this service can start.")
    sys.exit(1)


# =======================================================================
# SECRETS — all from environment variables, never hardcoded
# =======================================================================
anthropic_client = Anthropic()
stripe.api_key = os.environ["STRIPE_API_KEY"]  # client's RESTRICTED (read-only) key
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
resend.api_key = os.environ["RESEND_API_KEY"]

# Optional: protects the /stats endpoint from being publicly viewable.
# If unset, /stats is open to anyone with the URL — set this before
# sharing your server URL with anyone, including a client.
STATS_ACCESS_KEY = os.environ.get("STATS_ACCESS_KEY", "")

MODEL = "claude-sonnet-5"


# =======================================================================
# CLIENT BUSINESS CONFIG — single-tenant for now (see limitation #1)
# =======================================================================
BUSINESS = {
    "name": os.environ.get("CLIENT_BUSINESS_NAME", "PixelCraft Studio"),
    "type": os.environ.get("CLIENT_BUSINESS_TYPE", "online design tool subscription service"),
    "category": os.environ.get("CLIENT_CATEGORY", "playful"),  # "playful" or "serious"
    "from_email": os.environ.get("CLIENT_FROM_EMAIL", "billing@yourtool.com"),
    "reply_to": os.environ.get("CLIENT_REPLY_TO_EMAIL", ""),  # the CLIENT's real inbox
}


# =======================================================================
# TRACKING DATABASE — idempotency + basic reporting
# =======================================================================
DB_PATH = "tracking.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            invoice_id TEXT,
            attempt INTEGER,
            customer_email TEXT,
            amount REAL,
            currency TEXT DEFAULT 'usd',
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            recovered INTEGER DEFAULT 0,
            PRIMARY KEY (invoice_id, attempt)
        )
    """)
    return conn


def already_sent(invoice_id: str, attempt: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM sent_emails WHERE invoice_id=? AND attempt=?", (invoice_id, attempt)
    ).fetchone()
    conn.close()
    return row is not None


def mark_sent(invoice_id: str, attempt: int, customer_email: str, amount: float, currency: str):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO sent_emails (invoice_id, attempt, customer_email, amount, currency) "
        "VALUES (?, ?, ?, ?, ?)",
        (invoice_id, attempt, customer_email, amount, currency),
    )
    conn.commit()
    conn.close()


def mark_recovered(invoice_id: str):
    conn = get_db()
    conn.execute("UPDATE sent_emails SET recovered=1 WHERE invoice_id=?", (invoice_id,))
    conn.commit()
    conn.close()


# =======================================================================
# HELPERS
# =======================================================================
def format_amount(amount_cents: int, currency: str) -> str:
    """Stripe amounts are in the smallest currency unit (cents for USD/EUR).
    This assumes a 2-decimal currency (true for USD, EUR, GBP — the vast
    majority of cases). Zero-decimal currencies (e.g. JPY) would need a
    special case if a client ever bills in one — not handled here yet."""
    amount = amount_cents / 100
    return f"{amount:.2f} {currency.upper()}"


def get_customer_details(invoice: dict) -> tuple[str | None, str]:
    """Get email + name, with fallback to fetching the Customer object,
    since Stripe frequently leaves these null on the invoice itself."""
    customer_email = invoice.get("customer_email")
    customer_name = invoice.get("customer_name") or "there"

    if not customer_email and invoice.get("customer"):
        try:
            customer = stripe.Customer.retrieve(invoice["customer"])
            customer_email = customer.get("email")
            customer_name = customer.get("name") or customer_name
        except Exception as e:
            print(f"WARNING: could not fetch customer {invoice.get('customer')}: {e}")

    return customer_email, customer_name


# =======================================================================
# EMAIL GENERATION
# =======================================================================
FALLBACK_EMAIL = {
    "subject": "A payment update is needed",
    "header": "Payment Update Needed",
    "body": "We weren't able to process your recent payment. Please update your payment details to keep your account active.",
    "button": "Update Payment",
}


def generate_recovery_email(customer_name: str, amount_display: str, attempt: int) -> dict:
    """Returns dict with subject, header, body, button.
    Falls back to a plain, honest generic email if the AI call fails
    for any reason — a customer should NEVER get nothing because of
    an API hiccup."""
    category = BUSINESS.get("category", "playful")

    if category == "serious":
        tone_rule = (
            "SERIOUS category business (medical, financial, legal, or "
            "other essential service). Be warm and genuinely human, but "
            "NEVER joke, use wordplay, or reference the business's "
            "'theme' cleverly — that reads as tone-deaf when money and "
            "essential services are involved. Instead: be calm, direct, "
            "reassuring, and respectful of the customer's time. Avoid "
            "corporate-cold phrasing ('per our records', 'kindly note') "
            "but also avoid forced casualness. Think: a trustworthy, "
            "competent person calmly telling you something you need to "
            "know, not a friend joking with you."
        )
    else:
        tone_rule = (
            "PLAYFUL category business. Use ONE light, clever reference "
            "tied SPECIFICALLY to what THIS business actually does or "
            "sells — never a generic joke that could apply to any "
            "company. The reference should feel like it could only have "
            "been written by someone who genuinely knows this business. "
            "Avoid: forced puns, exclamation-point energy, or trying too "
            "hard. The bar is 'a sharp friend who works there texted "
            "you', not 'a marketing team tried to be funny'."
        )

    if attempt == 1:
        stage_rule = (
            "ATTEMPT 1 — the hook. This is the FIRST thing the customer "
            "hears about this. Low pressure, high personality. Make them "
            "want to read it, not feel chased. A light challenge or "
            "question tied to the business's theme works well, but only "
            "if it lands naturally — don't force it if this business "
            "doesn't lend itself to one. Assume good faith: they probably "
            "just forgot to update an expired card, not avoiding payment."
        )
    elif attempt == 2:
        stage_rule = (
            "ATTEMPT 2 — the follow-up. Acknowledge this is a repeat "
            "nudge naturally, in the business's own voice — never say "
            "'this is attempt 2' robotically. Slightly warmer and more "
            "direct than attempt 1, but still not urgent or guilt-"
            "inducing. Something like a genuine 'hey, did you catch my "
            "last note?' feeling, reworded to fit this specific business."
        )
    else:
        stage_rule = (
            "ATTEMPT 3+ — memorable and reflective, the last real chance "
            "before they likely lose access. This should feel noticeably "
            "different from attempts 1-2, not just 'more urgent' in the "
            "same voice. For a PLAYFUL business: you may include ONE "
            "short, evocative or poetic line if it genuinely fits the "
            "mood — otherwise skip it, forced poetry is worse than none. "
            "Then, in the business's own voice, sincerely ask why they "
            "might be leaving — this should read as real curiosity from "
            "a person, not a formal exit survey. For a SERIOUS business: "
            "skip any poetic flourish entirely, stay calm and direct, "
            "and ask sincerely and respectfully whether something's "
            "wrong or whether they need help resolving this."
        )

    system_prompt = (
        f"You write short, distinctive payment-failure emails for "
        f"{BUSINESS['name']}, a {BUSINESS['type']}.\n\n{tone_rule}\n\n"
        f"{stage_rule}\n\nHARD FORMAT RULES:\n"
        "- Subject: under 8 words\n"
        "- Header: punchy headline, 3-6 words, shown big at top of email\n"
        "- Body: 1-3 sentences max, no filler, no 'Dear'/'Sincerely'\n"
        "- Button: 2-4 words, imperative\n\n"
        "Respond in EXACTLY this format:\nSubject: ...\nHeader: ...\nBody: ...\nButton: ..."
    )
    user_prompt = f"Customer: {customer_name}\nAmount: {amount_display}\nAttempt number: {attempt}"

    try:
        response = anthropic_client.messages.create(
            model=MODEL, max_tokens=500, system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text")

        if not raw.strip():
            print("WARNING: empty response from Claude, using fallback email.")
            return dict(FALLBACK_EMAIL)

        result = dict(FALLBACK_EMAIL)  # defaults, overwritten below if present
        for line in raw.strip().split("\n"):
            if line.startswith("Subject:"): result["subject"] = line.replace("Subject:", "").strip()
            elif line.startswith("Header:"): result["header"] = line.replace("Header:", "").strip()
            elif line.startswith("Body:"): result["body"] = line.replace("Body:", "").strip()
            elif line.startswith("Button:"): result["button"] = line.replace("Button:", "").strip()
        return result

    except Exception as e:
        # A customer should NEVER receive nothing just because the AI
        # call had a bad moment — send the honest generic fallback instead.
        print(f"WARNING: Claude API call failed ({e}), using fallback email.")
        return dict(FALLBACK_EMAIL)


def wrap_in_html_email(parsed: dict, cta_link: str) -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,Helvetica,Arial,sans-serif;">
<table width="100%"><tr><td align="center" style="padding:40px 20px;">
<table width="480" style="background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
<tr><td style="padding:32px 32px 0 32px;text-align:center;">
<div style="font-size:15px;font-weight:700;letter-spacing:0.02em;color:#6b7280;text-transform:uppercase;">{BUSINESS['name']}</div>
<div style="height:1px;background:#e5e7eb;margin:20px 0 0 0;"></div>
</td></tr>
<tr><td style="padding:32px 32px 40px 32px;text-align:center;">
<h1 style="font-size:22px;font-weight:700;color:#111827;margin:0 0 16px 0;">{parsed['header']}</h1>
<p style="font-size:16px;line-height:1.6;color:#374151;margin:0 0 28px 0;">{parsed['body']}</p>
<a href="{cta_link}" style="display:inline-block;background:#111827;color:#fff;text-decoration:none;font-weight:600;font-size:15px;padding:14px 32px;border-radius:8px;">{parsed['button']}</a>
<p style="font-size:13px;color:#9ca3af;margin:24px 0 0 0;">Questions? Just reply to this email.</p>
</td></tr></table></td></tr></table></body></html>"""


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Returns True on success, False on failure — caller decides what
    to do (e.g. not mark as sent, let Stripe retry the webhook)."""
    email_data = {
        "from": f"{BUSINESS['name']} <{BUSINESS['from_email']}>",
        "to": to_email,
        "subject": subject,
        "html": html_body,
    }
    if BUSINESS.get("reply_to"):
        email_data["reply_to"] = BUSINESS["reply_to"]

    try:
        resend.Emails.send(email_data)
        return True
    except Exception as e:
        print(f"ERROR: Resend send failed for {to_email}: {e}")
        return False


# =======================================================================
# ROUTES
# =======================================================================
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    # --- SECURITY: verify this request genuinely came from Stripe ---
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        # FIX: newer stripe-python returns a StripeObject, not a plain dict —
        # .get() doesn't work the same way on it. Convert once, here, so
        # every .get() call below (event, invoice, etc.) works as expected.
        event = event.to_dict()
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f"WARNING: rejected webhook with invalid signature: {e}")
        return jsonify({"error": "invalid signature"}), 400

    try:
        if event["type"] == "invoice.payment_failed":
            invoice = event["data"]["object"]
            invoice_id = invoice.get("id", "unknown")

            # Skip anything with no real amount owed (e.g. $0 invoices,
            # already-voided invoices that still fire this event type).
            amount_due_cents = invoice.get("amount_due", 0)
            if amount_due_cents <= 0:
                print(f"Skipping invoice {invoice_id} — amount_due is 0.")
                return jsonify({"received": True, "skipped": "zero_amount"}), 200

            attempt = invoice.get("attempt_count", 1)

            # --- IDEMPOTENCY: don't resend for the same invoice+attempt.
            # Stripe guarantees at-least-once delivery, so duplicates are
            # expected, normal behavior — not a bug when they happen. ---
            if already_sent(invoice_id, attempt):
                print(f"Already sent for invoice {invoice_id} attempt {attempt} — skipping.")
                return jsonify({"received": True, "skipped": "duplicate"}), 200

            customer_email, customer_name = get_customer_details(invoice)

            if not customer_email:
                print(f"WARNING: no email found for invoice {invoice_id}, customer {invoice.get('customer')} — cannot send.")
                return jsonify({"received": True, "skipped": "no_email"}), 200

            currency = invoice.get("currency", "usd")
            amount_display = format_amount(amount_due_cents, currency)

            email_content = generate_recovery_email(customer_name, amount_display, attempt)
            update_link = invoice.get("hosted_invoice_url") or "https://example.com/update-card"
            html_body = wrap_in_html_email(email_content, update_link)

            sent_ok = send_email(customer_email, email_content["subject"], html_body)

            if sent_ok:
                mark_sent(invoice_id, attempt, customer_email, amount_due_cents / 100, currency)
                print(f"Recovery email sent to {customer_email} (attempt {attempt}, invoice {invoice_id})")
            else:
                # Don't mark as sent — if Stripe retries this webhook
                # later, we'll correctly try again instead of silently
                # having lost this customer's email forever.
                return jsonify({"error": "email send failed"}), 500

        elif event["type"] == "invoice.paid":
            invoice_id = event["data"]["object"].get("id", "unknown")
            mark_recovered(invoice_id)
            print(f"Payment recovered for invoice {invoice_id}")

        return jsonify({"received": True}), 200

    except Exception as e:
        # Catch-all: log clearly, return 500 so Stripe retries later —
        # better than a silent swallow that loses the event entirely.
        print(f"ERROR processing webhook event {event.get('type')}: {e}")
        return jsonify({"error": "internal processing error"}), 500


@app.route("/stats", methods=["GET"])
def stats():
    """Tracking view — protected if STATS_ACCESS_KEY is set.
    Visit as: yoururl.com/stats?key=YOUR_SECRET_KEY"""
    if STATS_ACCESS_KEY:
        provided_key = request.args.get("key", "")
        if provided_key != STATS_ACCESS_KEY:
            return jsonify({"error": "unauthorized"}), 403

    conn = get_db()
    rows = conn.execute("SELECT * FROM sent_emails ORDER BY sent_at DESC").fetchall()
    conn.close()

    total_sent = len(rows)
    total_recovered = sum(1 for r in rows if r[6] == 1)
    total_recovered_amount = sum(r[3] for r in rows if r[6] == 1)

    return jsonify({
        "total_emails_sent": total_sent,
        "total_recovered": total_recovered,
        "conversion_rate": f"{(total_recovered/total_sent*100):.1f}%" if total_sent else "0%",
        "total_recovered_amount": f"${total_recovered_amount:.2f}",
        "your_fee_12pct": f"${total_recovered_amount * 0.12:.2f}",
        "note": "Add ?key=YOUR_SECRET to secure this endpoint (set STATS_ACCESS_KEY env var)." if not STATS_ACCESS_KEY else None,
    })


@app.route("/stats/detail", methods=["GET"])
def stats_detail():
    """Row-by-row view of every email sent — for actual analysis, not
    just the summary numbers. Visit as: yoururl.com/stats/detail?key=YOUR_SECRET_KEY"""
    if STATS_ACCESS_KEY:
        provided_key = request.args.get("key", "")
        if provided_key != STATS_ACCESS_KEY:
            return jsonify({"error": "unauthorized"}), 403

    conn = get_db()
    rows = conn.execute("SELECT * FROM sent_emails ORDER BY sent_at DESC LIMIT 200").fetchall()
    conn.close()

    return jsonify({
        "rows": [
            {
                "invoice_id": r[0],
                "attempt": r[1],
                "customer_email": r[2],
                "amount": r[3],
                "currency": r[4],
                "sent_at": r[5],
                "recovered": bool(r[6]),
            }
            for r in rows
        ]
    })


@app.route("/", methods=["GET"])
def health_check():
    return jsonify({
        "status": "running",
        "business": BUSINESS["name"],
        "category": BUSINESS["category"],
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
