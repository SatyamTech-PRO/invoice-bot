import pytesseract
from PIL import Image

# ------------------------------------------------------------
# Tesseract binary path (Windows)
# ------------------------------------------------------------
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

IMAGE_PATH = "invoice.jpeg"


def main():
    print(f"Opening : {IMAGE_PATH}")
    image = Image.open(IMAGE_PATH)
    print(f"Size    : {image.size[0]}x{image.size[1]} px  mode: {image.mode}")

    print("Running OCR ...")
    text = pytesseract.image_to_string(image)

    print()
    print("=" * 60)
    print("Extracted text:")
    print("=" * 60)
    print(text.strip() if text.strip() else "(No text detected)")
    print("=" * 60)


if __name__ == "__main__":
    main()
