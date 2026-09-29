"""
WEBHOOK SERVER — production version.

Real Stripe signature verification, real secrets from environment
variables (never hardcoded), real email sending via Resend.
"""

import os
import stripe
import resend
from flask import Flask, request, jsonify
from anthropic import Anthropic

import sqlite3

app = Flask(__name__)

# --- Simple built-in tracking database (survives sleep/wake, resets on redeploy) ---
def init_db():
    conn = sqlite3.connect("tracking.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            invoice_id TEXT,
            attempt INTEGER,
            customer_email TEXT,
            amount REAL,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            recovered INTEGER DEFAULT 0,
            PRIMARY KEY (invoice_id, attempt)
        )
    """)
    conn.commit()
    conn.close()

init_db()


def already_sent(invoice_id: str, attempt: int) -> bool:
    conn = sqlite3.connect("tracking.db")
    row = conn.execute(
        "SELECT 1 FROM sent_emails WHERE invoice_id=? AND attempt=?", (invoice_id, attempt)
    ).fetchone()
    conn.close()
    return row is not None


def mark_sent(invoice_id: str, attempt: int, customer_email: str, amount: float):
    conn = sqlite3.connect("tracking.db")
    conn.execute(
        "INSERT OR IGNORE INTO sent_emails (invoice_id, attempt, customer_email, amount) VALUES (?, ?, ?, ?)",
        (invoice_id, attempt, customer_email, amount),
    )
    conn.commit()
    conn.close()


def mark_recovered(invoice_id: str):
    conn = sqlite3.connect("tracking.db")
    conn.execute("UPDATE sent_emails SET recovered=1 WHERE invoice_id=?", (invoice_id,))
    conn.commit()
    conn.close()

# --- Secrets: ALL read from environment variables, never hardcoded ---
anthropic_client = Anthropic()
stripe.api_key = os.environ["STRIPE_API_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
resend.api_key = os.environ["RESEND_API_KEY"]

MODEL = "claude-sonnet-5"

BUSINESS = {
    "name": os.environ.get("CLIENT_BUSINESS_NAME", "PixelCraft Studio"),
    "type": os.environ.get("CLIENT_BUSINESS_TYPE", "online design tool subscription service"),
    "tone": os.environ.get("CLIENT_BUSINESS_TONE", "friendly, casual, a little playful"),
    "from_email": os.environ.get("CLIENT_FROM_EMAIL", "billing@yourtool.com"),
}


def generate_recovery_email(customer_name: str, amount: float, attempt: int, failure_reason: str) -> dict:
    category = BUSINESS.get("category", "playful")

    if category == "serious":
        tone_rule = (
            "SERIOUS category business (medical, financial, essential "
            "service). Be warm and human, but do NOT joke. Respectful, "
            "calm, not robotic or corporate-cold."
        )
    else:
        tone_rule = (
            "PLAYFUL category business. Use ONE light, clever reference "
            "tied SPECIFICALLY to what this business does — not generic "
            "humor. Witty, not goofy or try-hard."
        )

    if attempt == 1:
        stage_rule = "ATTEMPT 1 — the hook. Short, intriguing, a light challenge or question tied to the business's theme."
    elif attempt == 2:
        stage_rule = "ATTEMPT 2 — the follow-up. Naturally acknowledge this is a repeat nudge, in the business's own voice. Slightly warmer."
    else:
        stage_rule = (
            "ATTEMPT 3+ — memorable and reflective. May include ONE short, "
            "evocative/poetic line if it fits. Then gently, sincerely ask "
            "why they might be leaving — not a formal survey. For serious "
            "category, skip poetry, just ask sincerely."
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
    user_prompt = f"Customer: {customer_name}\nAmount: ${amount:.2f}\nAttempt number: {attempt}"

    response = anthropic_client.messages.create(
        model=MODEL, max_tokens=250, system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")

    result = {"subject": "Payment issue", "header": "Payment Update Needed", "body": "", "button": "Update Payment"}
    for line in raw.strip().split("\n"):
        if line.startswith("Subject:"): result["subject"] = line.replace("Subject:", "").strip()
        elif line.startswith("Header:"): result["header"] = line.replace("Header:", "").strip()
        elif line.startswith("Body:"): result["body"] = line.replace("Body:", "").strip()
        elif line.startswith("Button:"): result["button"] = line.replace("Button:", "").strip()
    return result


def wrap_in_html_email(parsed: dict, cta_link: str) -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,Helvetica,Arial,sans-serif;">
<table width="100%"><tr><td align="center" style="padding:40px 20px;">
<table width="480" style="background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
<tr><td style="padding:40px 32px;text-align:center;">
<h1 style="font-size:22px;font-weight:700;color:#111827;margin:0 0 16px 0;">{parsed['header']}</h1>
<p style="font-size:16px;line-height:1.6;color:#374151;margin:0 0 28px 0;">{parsed['body']}</p>
<a href="{cta_link}" style="display:inline-block;background:#111827;color:#fff;text-decoration:none;font-weight:600;font-size:15px;padding:14px 32px;border-radius:8px;">{parsed['button']}</a>
</td></tr></table></td></tr></table></body></html>"""


def send_email(to_email: str, subject: str, html_body: str):
    resend.Emails.send({
        "from": BUSINESS["from_email"],
        "to": to_email,
        "subject": subject,
        "html": html_body,
    })


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "invalid signature"}), 400

    if event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_email = invoice.get("customer_email")
        customer_name = invoice.get("customer_name") or "there"
        amount = invoice["amount_due"] / 100
        attempt = invoice.get("attempt_count", 1)
        failure_reason = "a payment issue"

        invoice_id = invoice.get("id", "unknown")

        if already_sent(invoice_id, attempt):
            print(f"Already sent for invoice {invoice_id} attempt {attempt} — skipping.")
            return jsonify({"received": True, "skipped": "duplicate"}), 200

        email_content = generate_recovery_email(customer_name, amount, attempt, failure_reason)
        update_link = invoice.get("hosted_invoice_url", "https://example.com/update-card")
        html_body = wrap_in_html_email(email_content, update_link)

        if customer_email:
            send_email(customer_email, email_content["subject"], html_body)
            mark_sent(invoice_id, attempt, customer_email, amount)
            print(f"Recovery email sent to {customer_email} (attempt {attempt}, invoice {invoice_id})")

    elif event["type"] == "invoice.paid":
        invoice_id = event["data"]["object"].get("id", "unknown")
        mark_recovered(invoice_id)
        print(f"Payment recovered for invoice {invoice_id}")

    return jsonify({"received": True}), 200


@app.route("/stats", methods=["GET"])
def stats():
    conn = sqlite3.connect("tracking.db")
    rows = conn.execute("SELECT * FROM sent_emails ORDER BY sent_at DESC").fetchall()
    conn.close()
    total_sent = len(rows)
    total_recovered = sum(1 for r in rows if r[5] == 1)
    total_recovered_amount = sum(r[3] for r in rows if r[5] == 1)
    return jsonify({
        "total_emails_sent": total_sent,
        "total_recovered": total_recovered,
        "conversion_rate": f"{(total_recovered/total_sent*100):.1f}%" if total_sent else "0%",
        "total_recovered_amount": f"${total_recovered_amount:.2f}",
        "your_fee_12pct": f"${total_recovered_amount * 0.12:.2f}",
    })


@app.route("/", methods=["GET"])
def health_check():
    return "Webhook server is running.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
