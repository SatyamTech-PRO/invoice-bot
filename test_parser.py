import json
import re
import requests
from groq import Groq

GROQ_API_KEY = "your_groq_key_here"
ACCESS_TOKEN = "your_meta_token_here"
PHONE_NUMBER_ID = "your_phone_number_id_here"
RECIPIENT = "your_recipient_number_here"

RAW_OCR_TEXT = """
GUJARAT FREIGHT TOOLS
Invoice No. GST-3425-26
Date 23-Jul-2025
Customer: Shiv Engineering
Bosch All-in-One Metal Hand Tool Kit  7 NOS  2535.00
Taparia Universal Tool Kit  1 NOS  1270.00
IGST 18%  684.90
Total  4490.00
"""

EXTRACTION_PROMPT = f"""
Extract these fields from the invoice OCR text and return ONLY a JSON object, nothing else:
- vendor_name
- invoice_number
- date
- customer_name
- total_amount (number)
- gst_amount (number)
- gst_percent (number)
- line_items: array with description, quantity, amount

OCR text:
{RAW_OCR_TEXT}
"""

client = Groq(api_key=GROQ_API_KEY)

print("Sending to Groq for parsing...")

response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": EXTRACTION_PROMPT}],
    max_tokens=1024
)

raw = response.choices[0].message.content
clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)

print("\n== Parsed invoice data ==")
result = None
try:
    result = json.loads(clean)
    print(json.dumps(result, indent=2))
except json.JSONDecodeError as e:
    print(f"JSON error: {e}")
    print("Raw response:", raw)

if result:
    msg = (
        f"✅ Invoice saved!\n"
        f"Vendor: {result['vendor_name']}\n"
        f"Invoice: {result['invoice_number']}\n"
        f"Date: {result['date']}\n"
        f"Amount: ₹{result['total_amount']}\n"
        f"GST: ₹{result['gst_amount']} ({result['gst_percent']}%)\n"
        f"Items: {len(result['line_items'])}"
    )

    print("\n== Sending WhatsApp reply ==")
    wa_response = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": RECIPIENT,
            "type": "text",
            "text": {"body": msg}
        }
    )
    print(wa_response.json())