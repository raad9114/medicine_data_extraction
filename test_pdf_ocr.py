"""
Quick test: PDF download + OCR for one product (Ace, pid=147)
Run: python test_pdf_ocr.py
"""
import io, re, time, requests, fitz, pytesseract
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
BASE_URL = "https://www.squarepharma.com.bd"

# ── Build session ──────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": BASE_URL + "/",
        "Accept": "application/pdf,*/*;q=0.8",
    })
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

# ── Download PDF ───────────────────────────────────────────
pdf_url = "https://www.squarepharma.com.bd/downloads/1717581535_pdoc_Ace%20DS%20copy.pdf"
print(f"Downloading: {pdf_url}")

s = make_session()
try:
    resp = s.get(pdf_url, timeout=60)
    resp.raise_for_status()
    pdf_bytes = resp.content
    print(f"✅ Downloaded: {len(pdf_bytes):,} bytes")
except Exception as e:
    print(f"❌ Download failed: {e}")
    exit(1)

# ── OCR ────────────────────────────────────────────────────
print("Running OCR (300 DPI)...")
doc = fitz.open(stream=pdf_bytes, filetype="pdf")
page = doc[0]
mat = fitz.Matrix(300/72, 300/72)
pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
img = Image.open(io.BytesIO(pix.tobytes("png")))
ocr_text = pytesseract.image_to_string(img, lang="eng", config="--psm 6 --oem 3")
doc.close()

print(f"✅ OCR done. Characters extracted: {len(ocr_text)}")
print("\n=== FULL OCR TEXT ===")
print(ocr_text)
