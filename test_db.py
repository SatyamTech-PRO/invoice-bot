import sqlite3
import json

def init_db():
    conn = sqlite3.connect("invoices.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            vendor TEXT,
            invoice_number TEXT,
            date TEXT,
            total_amount REAL,
            gst_amount REAL,
            gst_percent REAL,
            paid INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def save_invoice(conn, phone, data):
    conn.execute("""
        INSERT INTO invoices (phone, vendor, invoice_number, date, total_amount, gst_amount, gst_percent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (phone, data["vendor_name"], data["invoice_number"], data["date"],
          data["total_amount"], data["gst_amount"], data["gst_percent"]))
    conn.commit()
    print("Invoice saved to database!")

def show_all(conn):
    rows = conn.execute("SELECT * FROM invoices").fetchall()
    print(f"\nTotal invoices in DB: {len(rows)}")
    for row in rows:
        print(row)

sample = {
    "vendor_name": "GUJARAT FREIGHT TOOLS",
    "invoice_number": "GST-3425-26",
    "date": "23-Jul-2025",
    "total_amount": 4490.0,
    "gst_amount": 684.9,
    "gst_percent": 18
}

conn = init_db()
save_invoice(conn, "918527788919", sample)
show_all(conn)
conn.close()