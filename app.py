import json, re, sqlite3, io
import pytesseract
import requests
from flask import Flask, request, jsonify
from PIL import Image
from groq import Groq
from twilio.rest import Client

app = Flask(__name__)

# --- Config ---
TWILIO_SID   = "your_twilio_sid_here"
TWILIO_TOKEN = "your_twilio_token_here"
GROQ_API_KEY = "your_groq_key_here"
ACCESS_TOKEN = "your_meta_token_here"
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# --- Database ---
def init_db():
    conn = sqlite3.connect("invoices.db")
    conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT, vendor TEXT, invoice_number TEXT,
        date TEXT, total_amount REAL, gst_amount REAL,
        gst_percent REAL, paid INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()

def save_invoice(phone, data):
    conn = sqlite3.connect("invoices.db")
    conn.execute("""INSERT INTO invoices
        (phone, vendor, invoice_number, date, total_amount, gst_amount, gst_percent)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (phone, data.get("vendor_name"), data.get("invoice_number"),
         data.get("date"), data.get("total_amount"),
         data.get("gst_amount"), data.get("gst_percent")))
    conn.commit()
    conn.close()

# --- OCR ---
def run_ocr(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    return pytesseract.image_to_string(img)

# --- AI Parser ---
def parse_invoice(ocr_text):
    client = Groq(api_key=GROQ_API_KEY)
    prompt = f"""Extract these fields from invoice OCR text and return ONLY JSON:
- vendor_name, invoice_number, date, customer_name
- total_amount (number), gst_amount (number), gst_percent (number)
- line_items: array with description, quantity, amount

OCR text:
{ocr_text}"""
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024)
    raw = response.choices[0].message.content
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    return json.loads(clean)

# --- WhatsApp Reply via Twilio ---
def send_whatsapp(to, message):
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(
        from_=TWILIO_FROM,
        to=to,
        body=message)
    print(f"[Twilio] Reply sent to {to}")

# --- Webhook GET (Meta verification - kept for compatibility) ---
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Forbidden", 403

# --- Webhook POST (Twilio sends here) ---
@app.route("/webhook", methods=["POST"])
def receive():
    sender   = request.form.get("From", "")
    num_media = int(request.form.get("NumMedia", 0))

    print(f"\n{'='*50}")
    print(f"Message from: {sender}")
    print(f"Images: {num_media}")

    if num_media == 0:
        print("No image received")
        return jsonify({"status": "ok"}), 200

    # Download image
    media_url  = request.form.get("MediaUrl0")
    media_type = request.form.get("MediaContentType0")
    print(f"Image URL: {media_url}")

    img_resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
    print(f"Downloaded: {len(img_resp.content)} bytes")

    # OCR
    ocr_text = run_ocr(img_resp.content)
    print(f"OCR text:\n{ocr_text[:300]}")

    # Parse
    try:
        data = parse_invoice(ocr_text)
        print(f"Parsed: {data}")
    except Exception as e:
        print(f"Parse error: {e}")
        send_whatsapp(sender, "❌ Could not read this invoice. Please send a clearer photo.")
        return jsonify({"status": "ok"}), 200

    # Save
    save_invoice(sender, data)

    # Reply
    reply = (
        f"✅ Invoice saved!\n"
        f"Vendor: {data.get('vendor_name')}\n"
        f"Invoice: {data.get('invoice_number')}\n"
        f"Date: {data.get('date')}\n"
        f"Amount: ₹{data.get('total_amount')}\n"
        f"GST: ₹{data.get('gst_amount')} ({data.get('gst_percent')}%)\n"
        f"Items: {len(data.get('line_items', []))}"
    )
    send_whatsapp(sender, reply)

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)