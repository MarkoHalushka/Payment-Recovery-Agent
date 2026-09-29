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
