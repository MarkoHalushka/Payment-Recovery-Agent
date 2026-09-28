"""
WEBHOOK SERVER — production version.

Real Stripe signature verification, real secrets from environment
variables (never hardcoded), real email sending via Resend.

This is the file that actually goes live on Render/Railway.
"""

import os
import stripe
import resend
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)

# --- Secrets: ALL read from environment variables, never hardcoded ---
anthropic_client = Anthropic()  # reads ANTHROPIC_API_KEY automatically
stripe.api_key = os.environ["STRIPE_API_KEY"]  # client's RESTRICTED key
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
resend.api_key = os.environ["RESEND_API_KEY"]

MODEL = "claude-sonnet-5"

# For now: one hardcoded business (your first real client).
# Later: look this up by client ID from a database.
BUSINESS = {
    "name": os.environ.get("CLIENT_BUSINESS_NAME", "PixelCraft Studio"),
    "type": os.environ.get("CLIENT_BUSINESS_TYPE", "online design tool subscription service"),
    "tone": os.environ.get("CLIENT_BUSINESS_TONE", "friendly, casual, a little playful"),
    "from_email": os.environ.get("CLIENT_FROM_EMAIL", "billing@yourtool.com"),
}


def generate_recovery_email(customer_name: str, amount: float, attempt: int, failure_reason: str) -> tuple[str, str]:
    """Returns (subject, body)."""
    system_prompt = (
        f"You are writing a payment recovery email on behalf of "
        f"{BUSINESS['name']}, a {BUSINESS['type']}. Brand voice: "
        f"{BUSINESS['tone']}. Respond with EXACTLY two lines: "
        f"first line starting with 'Subject: ', second line onward is "
        f"the full email body. This is attempt {attempt} — gentle on "
        f"attempt 1, clearly urgent by attempt 3+. Sound like a real "
        f"person from this business, not a generic notice."
    )
    user_prompt = (
        f"Customer: {customer_name}\n"
        f"Amount due: ${amount:.2f}\n"
        f"Failure reason: {failure_reason}\n"
        f"Attempt number: {attempt}"
    )

    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    full_text = ""
    for block in response.content:
        if block.type == "text":
            full_text = block.text
            break

    if full_text.lower().startswith("subject:"):
        subject_line, _, body = full_text.partition("\n")
        subject = subject_line.replace("Subject:", "").strip()
        return subject, body.strip()
    return "A payment update is needed", full_text


def send_email(to_email: str, subject: str, body: str):
    resend.Emails.send({
        "from": BUSINESS["from_email"],
        "to": to_email,
        "subject": subject,
        "text": body,
    })


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    # --- SECURITY: verify this request genuinely came from Stripe ---
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
        failure_reason = "a payment issue"  # Stripe's real payload has more detail here

        subject, body = generate_recovery_email(customer_name, amount, attempt, failure_reason)

        if customer_email:
            send_email(customer_email, subject, body)
            print(f"Recovery email sent to {customer_email} (attempt {attempt})")

    elif event["type"] == "invoice.paid":
        print("Payment recovered — log this for billing.")

    return jsonify({"received": True}), 200


@app.route("/", methods=["GET"])
def health_check():
    return "Webhook server is running.", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
