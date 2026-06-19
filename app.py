"""
Invoice Bot – Flask backend
Handles:
  • Meta WhatsApp Cloud API webhook (verification + incoming messages)
  • OCR pipeline: Meta media download → pytesseract
  • Groq (llama-3.3-70b-versatile) parsing: extract structured invoice fields
  • SQLite persistence of parsed invoices
  • REST API for the dashboard (/dashboard, /api/invoices, /api/mark-paid)
  • APScheduler: daily overdue-invoice reminders via Twilio WhatsApp
"""

import io
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytesseract
import requests
from flask import Flask, g, jsonify, render_template, request
from flask_apscheduler import APScheduler
from groq import Groq
from PIL import Image
from twilio.rest import Client as TwilioClient

# ------------------------------------------------------------------ #
# Tesseract binary path (Windows)
# ------------------------------------------------------------------ #
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ------------------------------------------------------------------ #
# Configuration — replace placeholder values before running
# ------------------------------------------------------------------ #
VERIFY_TOKEN  = "invoicebot123"
ACCESS_TOKEN  = "YOUR_META_ACCESS_TOKEN_HERE"

GROQ_API_KEY  = "YOUR_GROQ_API_KEY_HERE"         # <- replace with your key
GROQ_MODEL    = "llama-3.3-70b-versatile"

TWILIO_SID    = "YOUR_TWILIO_ACCOUNT_SID"
TWILIO_TOKEN  = "YOUR_TWILIO_AUTH_TOKEN"
TWILIO_FROM   = "whatsapp:+14155238886"           # Twilio sandbox / your number

DB_PATH       = Path(__file__).parent / "invoices.db"
GRAPH_API_VER = "v19.0"
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_API_VER}"

# ------------------------------------------------------------------ #
# Flask app
# ------------------------------------------------------------------ #
app = Flask(__name__)
app.config["SCHEDULER_API_ENABLED"] = False

# ------------------------------------------------------------------ #
# Database helpers
# ------------------------------------------------------------------ #
SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phone          TEXT    NOT NULL,
    vendor         TEXT,
    invoice_number TEXT,
    date           TEXT,
    total_amount   REAL,
    gst_amount     REAL,
    gst_percent    REAL,
    paid           INTEGER DEFAULT 0,
    created_at     TEXT    DEFAULT (datetime('now'))
)
"""

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(SCHEMA)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None) -> None:
    db = g.pop("db", None)
    if db:
        db.close()


def save_invoice(phone: str, data: dict) -> int:
    """Persist a parsed invoice dict to the DB. Returns the new row id."""
    db = get_db()
    cur = db.execute(
        """INSERT INTO invoices
               (phone, vendor, invoice_number, date, total_amount, gst_amount, gst_percent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            phone,
            data.get("vendor_name"),
            data.get("invoice_number"),
            data.get("date"),
            data.get("total_amount"),
            data.get("gst_amount"),
            data.get("gst_percent"),
        ),
    )
    db.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ #
# Meta Graph API helpers
# ------------------------------------------------------------------ #
def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def fetch_image_download_url(media_id: str) -> str | None:
    """
    Step 1 – Resolve media_id → short-lived download URL.
    GET https://graph.facebook.com/v19.0/{media_id}
    """
    resp = requests.get(f"{GRAPH_BASE}/{media_id}", headers=_auth_headers(), timeout=10)
    if resp.ok:
        return resp.json().get("url")
    print(f"[OCR] Failed to resolve media ID {media_id}: {resp.status_code} {resp.text}")
    return None


def download_image_bytes(download_url: str) -> bytes | None:
    """
    Step 2 – Download the actual image bytes using the short-lived URL.
    The Authorization header is required here too.
    """
    resp = requests.get(download_url, headers=_auth_headers(), timeout=30)
    if resp.ok:
        return resp.content
    print(f"[OCR] Failed to download image: {resp.status_code} {resp.text}")
    return None


# ------------------------------------------------------------------ #
# OCR
# ------------------------------------------------------------------ #
def run_ocr(image_bytes: bytes) -> str:
    image = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(image).strip()


# ------------------------------------------------------------------ #
# Groq invoice parser
# ------------------------------------------------------------------ #
_groq_client: Groq | None = None


def _get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


PARSE_PROMPT_TEMPLATE = """You are an invoice data extraction assistant.

Extract the following fields from the raw OCR text below and return them as
a single valid JSON object. Use null for any field not found in the text.

Fields to extract:
  - vendor_name    : string
  - invoice_number : string
  - date           : string (as it appears in the invoice)
  - customer_name  : string
  - total_amount   : number
  - gst_amount     : number
  - gst_percent    : number (e.g. 18 for 18%)
  - line_items     : array of objects, each with description, quantity, amount

Return ONLY the JSON object. Do not include markdown fences, explanations,
or any other text outside the JSON.

--- RAW OCR TEXT ---
{ocr_text}
--- END OF TEXT ---"""


def parse_invoice_with_groq(ocr_text: str) -> dict:
    client = _get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a precise invoice data extractor. Always respond with valid JSON only.",
            },
            {
                "role": "user",
                "content": PARSE_PROMPT_TEMPLATE.format(ocr_text=ocr_text),
            },
        ],
        temperature=0.1,
    )
    raw = response.choices[0].message.content.strip()
    # Strip optional ```json fences in case the model adds them anyway
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw)


# ------------------------------------------------------------------ #
# Full image processing pipeline
# ------------------------------------------------------------------ #
def process_image(sender: str, media_id: str, mime_type: str, caption: str) -> None:
    print(f"Media ID    : {media_id}")
    print(f"MIME type   : {mime_type}")
    if caption:
        print(f"Caption     : {caption}")

    # Step 1: resolve media_id → download URL
    download_url = fetch_image_download_url(media_id)
    if not download_url:
        print("[Pipeline] Could not obtain download URL.")
        return
    print(f"Download URL: {download_url}")

    # Step 2: download image bytes
    image_bytes = download_image_bytes(download_url)
    if not image_bytes:
        print("[Pipeline] Could not download image.")
        return
    print(f"[Pipeline] Image downloaded: {len(image_bytes):,} bytes")

    # Step 3: OCR
    ocr_text = run_ocr(image_bytes)
    print(f"\n[OCR Result]\n{'-' * 40}\n{ocr_text}\n{'-' * 40}")

    if not ocr_text:
        print("[Pipeline] No text detected by OCR.")
        return

    # Step 4: Groq parsing
    try:
        invoice_data = parse_invoice_with_groq(ocr_text)
        print(f"[Groq] Parsed invoice:\n{json.dumps(invoice_data, indent=2)}")
    except Exception as exc:
        print(f"[Pipeline] Groq parsing failed: {exc}")
        return

    # Step 5: Save to DB
    try:
        row_id = save_invoice(sender, invoice_data)
        print(f"[DB] Invoice saved with id={row_id}")
    except Exception as exc:
        print(f"[Pipeline] DB save failed: {exc}")


# ------------------------------------------------------------------ #
# GET /webhook  — Meta webhook verification challenge
# ------------------------------------------------------------------ #
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Meta sends a one-time GET request to verify the webhook endpoint.
    It passes three query params:
      hub.mode         – always "subscribe"
      hub.verify_token – must match our VERIFY_TOKEN
      hub.challenge    – an arbitrary string we must echo back
    """
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[Webhook] Verification successful. Echoing challenge.")
        return challenge, 200

    print("[Webhook] Verification FAILED – token mismatch or wrong mode.")
    return jsonify({"error": "Forbidden"}), 403


# ------------------------------------------------------------------ #
# POST /webhook  — Incoming WhatsApp messages
# ------------------------------------------------------------------ #
@app.route("/webhook", methods=["POST"])
def receive_message():
    """
    Meta delivers incoming WhatsApp events as a JSON payload.

    Image message path:
      entry[0].changes[0].value.messages[0]
        .from            – sender phone (E.164, no '+')
        .type            – "image"
        .image.id        – media ID used to fetch the download URL
        .image.mime_type
        .image.caption   – optional caption text
    """
    payload = request.get_json(silent=True)

    if not payload:
        return jsonify({"error": "Invalid JSON"}), 400

    print(f"\n{'=' * 55}")
    print("Incoming Meta WhatsApp Cloud API event")
    print(f"{'=' * 55}")
    print(json.dumps(payload, indent=2))
    print(f"{'-' * 55}")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value    = change.get("value", {})
            messages = value.get("messages", [])

            for message in messages:
                sender   = message.get("from", "Unknown")
                msg_type = message.get("type", "unknown")

                print(f"Sender      : +{sender}")
                print(f"Message type: {msg_type}")

                if msg_type == "image":
                    image_info = message.get("image", {})
                    process_image(
                        sender    = f"+{sender}",
                        media_id  = image_info.get("id", ""),
                        mime_type = image_info.get("mime_type", "unknown"),
                        caption   = image_info.get("caption", ""),
                    )
                else:
                    print("(No image in this message)")

    print(f"{'=' * 55}\n")
    # Meta expects a 200 OK immediately; anything else triggers a retry.
    return jsonify({"status": "ok"}), 200


# ------------------------------------------------------------------ #
# GET /dashboard
# ------------------------------------------------------------------ #
@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ------------------------------------------------------------------ #
# GET /api/invoices
# ------------------------------------------------------------------ #
@app.route("/api/invoices")
def api_invoices():
    db   = get_db()
    rows = db.execute("SELECT * FROM invoices ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


# ------------------------------------------------------------------ #
# POST /api/mark-paid/<id>
# ------------------------------------------------------------------ #
@app.route("/api/mark-paid/<int:invoice_id>", methods=["POST"])
def mark_paid(invoice_id: int):
    db = get_db()
    db.execute("UPDATE invoices SET paid = 1 WHERE id = ?", (invoice_id,))
    db.commit()
    return jsonify({"status": "ok", "id": invoice_id})


# ------------------------------------------------------------------ #
# APScheduler — daily overdue reminders
# ------------------------------------------------------------------ #
scheduler = APScheduler()
scheduler.init_app(app)


@scheduler.task("cron", id="overdue_reminders", hour=9, minute=0)
def send_overdue_reminders() -> None:
    """
    Runs daily at 09:00. Finds invoices unpaid for more than 7 days
    and sends a WhatsApp reminder via Twilio to the sender's phone number.
    """
    with app.app_context():
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM invoices WHERE paid = 0 AND created_at <= ?",
                (cutoff,),
            ).fetchall()

        if not rows:
            print("[Scheduler] No overdue invoices found.")
            return

        twilio = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        for row in rows:
            phone  = row["phone"]
            vendor = row["vendor"] or "Unknown Vendor"
            inv    = row["invoice_number"] or "N/A"
            amount = row["total_amount"] or 0

            body = (
                f"Payment Reminder: Invoice {inv} from {vendor} "
                f"amounting to Rs.{amount:,.2f} is overdue (unpaid for over 7 days). "
                f"Please arrange payment at the earliest."
            )

            try:
                twilio.messages.create(body=body, from_=TWILIO_FROM, to=f"whatsapp:{phone}")
                print(f"[Scheduler] Reminder sent to {phone} for invoice {inv}")
            except Exception as exc:
                print(f"[Scheduler] Failed to send reminder to {phone}: {exc}")


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    init_db()
    scheduler.start()
    app.run(debug=True, port=5000)