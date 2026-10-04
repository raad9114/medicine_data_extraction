"""
Square Pharmaceuticals PLC - Product Data Scraper
==================================================
Version 3.0 — Playwright browser PDF download + Tesseract OCR

Requirements:
    pip install requests beautifulsoup4 mysql-connector-python lxml pymupdf pytesseract pillow playwright
    python -m playwright install chromium

Tesseract OCR (already installed):
    C:\\Program Files\\Tesseract-OCR\\tesseract.exe

Usage:
    python square_pharma_scraper.py
"""

import io
import re
import time
import logging
import tempfile
import os
import requests
import pytesseract
import fitz  # PyMuPDF
from PIL import Image
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import mysql.connector
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─────────────────────────────────────────────
#  🔧 CONFIGURATION
# ─────────────────────────────────────────────
DB_CONFIG = {
    "host":        "localhost",
    "user":        "root",
    "password":    "",               # XAMPP default
    "database":    "medicine_db",
    "charset":     "utf8mb4",
    "use_unicode": True,
}

BASE_URL     = "https://www.squarepharma.com.bd"
COMPANY_NAME = "Square Pharmaceuticals PLC."

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

PDF_DPI       = 300    # higher = better OCR accuracy
REQUEST_DELAY = 3.0    # ⬆️ increased to 3s — avoids server rate-limiting

# ─────────────────────────────────────────────
#  ⚠️  FRESH START — True = table মুছে নতুন করে শুরু
#  False = নতুন product যোগ করবে, duplicate skip করবে
# ─────────────────────────────────────────────
FRESH_START = False  # ← False: already inserted products skip হবে, নতুনগুলো insert হবে

# ─────────────────────────────────────────────
#  📝 LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  🌐 HTTP SESSION (HTML pages only)
# ─────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    })
    retry = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

session = make_session()

# ─────────────────────────────────────────────
#  🗄️ DATABASE
# ─────────────────────────────────────────────
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS medicines (
    id                          INT AUTO_INCREMENT PRIMARY KEY,
    brand_name                  VARCHAR(255)    DEFAULT NULL,
    generic_name                VARCHAR(255)    DEFAULT NULL,
    strength                    VARCHAR(255)    DEFAULT NULL,
    dosage_form                 VARCHAR(255)    DEFAULT NULL,
    company_name                VARCHAR(255)    DEFAULT NULL,
    unit_price                  VARCHAR(50)     DEFAULT NULL,
    strip_price                 VARCHAR(50)     DEFAULT NULL,
    pack_size                   VARCHAR(100)    DEFAULT NULL,
    image_url                   VARCHAR(500)    DEFAULT NULL,
    source_url                  VARCHAR(500)    DEFAULT NULL,
    indications                 TEXT            DEFAULT NULL,
    pharmacology                TEXT            DEFAULT NULL,
    dosage_and_administration   TEXT            DEFAULT NULL,
    interaction                 TEXT            DEFAULT NULL,
    contraindications           TEXT            DEFAULT NULL,
    side_effects                TEXT            DEFAULT NULL,
    pregnancy_and_lactation     TEXT            DEFAULT NULL,
    precautions_and_warnings    TEXT            DEFAULT NULL,
    use_in_special_populations  TEXT            DEFAULT NULL,
    overdose_effects            TEXT            DEFAULT NULL,
    therapeutic_class           TEXT            DEFAULT NULL,
    storage_conditions          TEXT            DEFAULT NULL,
    common_questions            TEXT            DEFAULT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    UNIQUE KEY uq_source_url (source_url)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

INSERT_SQL = """
INSERT IGNORE INTO medicines (
    brand_name, generic_name, strength, dosage_form, company_name,
    unit_price, strip_price, pack_size, image_url, source_url,
    indications, pharmacology, dosage_and_administration, interaction,
    contraindications, side_effects, pregnancy_and_lactation,
    precautions_and_warnings, use_in_special_populations, overdose_effects,
    therapeutic_class, storage_conditions, common_questions
) VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s)
"""

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def setup_database(conn):
    cur = conn.cursor()
    cur.execute(CREATE_TABLE_SQL)
    # existing table এও UNIQUE key যোগ করো (already থাকলে error হবে না)
    try:
        cur.execute("""
            ALTER TABLE medicines
            ADD UNIQUE KEY uq_source_url (source_url)
        """)
        log.info("   ✅ UNIQUE KEY on source_url added.")
    except mysql.connector.Error:
        pass  # key already exists — fine
    # FRESH_START — সব data মুছে দাও
    if FRESH_START:
        cur.execute("TRUNCATE TABLE medicines")
        log.info("🗑️  FRESH_START=True — Table cleared. All data deleted.")
    conn.commit()
    cur.close()
    log.info("✅ Database table ready (duplicate-safe).")


# ─────────────────────────────────────────────
#  🔍 UTILITIES
# ─────────────────────────────────────────────
def clean_text(el):
    if el is None:
        return None
    t = el.get_text(separator="\n", strip=True)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t if t else None

def fetch_html(url, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=45)  # 45s timeout for slow server
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            log.warning(f"   ⚠️  HTML attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(REQUEST_DELAY * 3 * attempt)  # 4.5s, 9s backoff
    log.error(f"❌ Cannot fetch HTML: {url}")
    return None


# ─────────────────────────────────────────────
#  🌐 PLAYWRIGHT: Browser PDF Downloader
# ─────────────────────────────────────────────
def download_pdf_browser(pdf_url, pw_context):
    """
    Playwright Chromium browser দিয়ে PDF download করে bytes return করে।
    Real browser হওয়ায় server block করতে পারে না।
    """
    tmp_dir  = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "prescribing.pdf")
    pdf_bytes = None
    page = None

    try:
        page = pw_context.new_page()
        with page.expect_download(timeout=30_000) as dl_info:
            try:
                page.goto(pdf_url, wait_until="commit", timeout=30_000)
            except Exception as nav_err:
                # "Download is starting" means browser started the download — this is SUCCESS
                if "Download is starting" not in str(nav_err):
                    raise  # real error, re-raise

        # dl_info.value এ download আছে, save করো
        dl = dl_info.value
        dl.save_as(tmp_path)

        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 1000:
            with open(tmp_path, "rb") as f:
                pdf_bytes = f.read()
            log.info(f"   📥 PDF: {len(pdf_bytes):,} bytes")
        else:
            log.warning("   ⚠️  PDF file too small or missing after download")

    except PWTimeout:
        log.warning("   ⚠️  Browser PDF download timed out")
    except Exception as e:
        log.warning(f"   ⚠️  Browser PDF error: {e}")
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass

    return pdf_bytes


# ─────────────────────────────────────────────
#  🔍 OCR: PDF bytes → plain text
# ─────────────────────────────────────────────
def pdf_to_ocr_text(pdf_bytes):
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            mat = fitz.Matrix(PDF_DPI / 72, PDF_DPI / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            txt = pytesseract.image_to_string(img, lang="eng", config="--psm 6 --oem 3")
            pages.append(txt)
        doc.close()
        return "\n".join(pages)
    except Exception as e:
        log.warning(f"   ⚠️  OCR error: {e}")
        return ""


# ─────────────────────────────────────────────
#  🧩 PARSE OCR TEXT → SECTIONS
# ─────────────────────────────────────────────
PDF_SECTION_PATTERNS = [
    ("composition",               r"COMPOSITION"),
    ("pharmacology",              r"PHARMACOLOG"),
    ("indication",                r"INDICATION"),
    ("dosage_and_administration", r"DOSAGE\s*[&AND]+\s*ADMINISTRATION"),
    ("contraindications",         r"CONTRAINDICATION"),
    ("side_effects",              r"SIDE\s*EFFECT|ADVERSE\s*EFFECT"),
    ("overdose_effects",          r"OVERDOSE"),
    ("pregnancy_and_lactation",   r"PREGNANC|LACTATION"),
    ("precautions_and_warnings",  r"PRECAUTION|WARNING"),
    ("use_in_special_populations",r"SPECIAL\s*POPULATION|PEDIATRIC|RENAL|HEPATIC"),
    ("interaction",               r"(?:DRUG\s*)?INTERACTION"),
    ("storage_conditions",        r"STORAGE|STORE\s*AT"),
    ("pack_size",                 r"HOW\s*SUPPLIED|PREPARATION"),
]

def parse_pdf_sections(ocr_text):
    if not ocr_text.strip():
        return {}
    hits = []
    for key, pattern in PDF_SECTION_PATTERNS:
        for m in re.finditer(pattern, ocr_text, re.IGNORECASE):
            hits.append((m.start(), key))
    hits.sort()
    sections = {}
    for i, (start, key) in enumerate(hits):
        end  = hits[i + 1][0] if i + 1 < len(hits) else len(ocr_text)
        body = ocr_text[start:end].strip()
        lines = [l for l in body.split("\n")[1:] if l.strip()]
        content = "\n".join(lines).strip()
        if content and key not in sections:
            sections[key] = content
    return sections

def extract_strength(comp):
    if not comp:
        return None
    m = re.search(
        r"(\d+[\d.,]*\s*(?:mg|mcg|ml|g|%|IU|mEq|mg/ml|mg/5\s*ml))",
        comp, re.IGNORECASE
    )
    return m.group(1) if m else None

def extract_dosage_form(comp):
    if not comp:
        return None
    forms = ["tablet","capsule","syrup","suspension","injection","infusion",
             "suppository","drops","cream","ointment","gel","solution",
             "inhaler","powder","sachet","patch","eye drop","ear drop"]
    low = comp.lower()
    for f in forms:
        if f in low:
            return f.capitalize()
    return None


# ─────────────────────────────────────────────
#  📋 STEP 1: All Product Links
# ─────────────────────────────────────────────
CHARS = ["1","5","A","B","C","D","E","F","G","H","I","K",
         "L","M","N","O","P","Q","R","S","T","U","V","W","X","Z"]

def collect_all_product_links():
    all_links = {}
    for char in CHARS:
        url  = f"{BASE_URL}/products-by-tradename.php?type=pharma&char={char}"
        log.info(f"📄 Listing char='{char}'")
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=re.compile(r"product-details\.php\?pid=\d+")):
            m = re.search(r"pid=(\d+)", a["href"])
            if m:
                pid = m.group(1)
                all_links[pid] = urljoin(BASE_URL + "/", a["href"])
        log.info(f"   Cumulative: {len(all_links)} products")
        time.sleep(REQUEST_DELAY)
    log.info(f"\n🎯 Total unique products: {len(all_links)}")
    return all_links


# ─────────────────────────────────────────────
#  🔬 STEP 2: Scrape One Product
# ─────────────────────────────────────────────
def scrape_product(pid, url, pw_context):
    html = fetch_html(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # Header
    brand_name = generic_name = therapeutic_class = None
    hdr = soup.find("div", id="toptizerpdetails")
    if hdr:
        h1 = hdr.find("h1")
        if h1:
            brand_name = h1.get_text(strip=True)
        spans = hdr.find_all("span")
        if spans:
            generic_name = spans[0].get_text(strip=True)
        if len(spans) >= 2:
            therapeutic_class = spans[1].get_text(strip=True).strip("()")

    # Image
    image_url = None
    pd_div = soup.find("div", id="productdetails")
    if pd_div:
        img = pd_div.find("img")
        if img and img.get("src"):
            image_url = urljoin(BASE_URL + "/", img["src"])

    # HTML sections
    html_secs = {}
    prodetails = soup.find("div", class_="prodetails")
    if prodetails:
        for p in prodetails.find_all("p", recursive=True):
            sp = p.find("span", class_=re.compile(r"pagtitle"))
            if sp:
                lbl = sp.get_text(strip=True).lower().rstrip(":")
                val = clean_text(p)
                if lbl and val:
                    html_secs[lbl] = val

    def from_html(*keys):
        for k in keys:
            for lbl, val in html_secs.items():
                if k.lower() in lbl:
                    return val
        return None

    # PDF + OCR
    pdf_secs = {}
    btn = soup.find("div", class_="prescribe-btn")
    if btn:
        a = btn.find("a")
        if a and a.get("href"):
            pdf_url = urljoin(BASE_URL + "/", a["href"])
            log.info(f"   📑 PDF: {pdf_url}")
            pdf_bytes = download_pdf_browser(pdf_url, pw_context)
            if pdf_bytes:
                log.info(f"   🔍 OCR ({PDF_DPI} DPI)...")
                ocr = pdf_to_ocr_text(pdf_bytes)
                if ocr.strip():
                    pdf_secs = parse_pdf_sections(ocr)
                    log.info(f"   ✅ Sections: {list(pdf_secs.keys())}")
            time.sleep(REQUEST_DELAY)

    def get(*keys):
        for k in keys:
            if k in pdf_secs and pdf_secs[k]:
                return pdf_secs[k]
        return from_html(*keys)

    # Strength + dosage form
    comp        = pdf_secs.get("composition", "")
    strength    = extract_strength(comp)
    dosage_form = extract_dosage_form(comp)
    if not strength and generic_name:
        m = re.search(r"(\d+[\d.,]*\s*(?:mg|mcg|ml|g|%|IU|mEq))", generic_name, re.I)
        if m:
            strength = m.group(1)

    return {
        "brand_name":                 brand_name,
        "generic_name":               generic_name,
        "strength":                   strength,
        "dosage_form":                dosage_form,
        "company_name":               COMPANY_NAME,
        "unit_price":                 None,
        "strip_price":                None,
        "pack_size":                  get("pack_size") or from_html("preparation","how supplied"),
        "image_url":                  image_url,
        "source_url":                 url,
        "indications":                get("indication"),
        "pharmacology":               get("pharmacology"),
        "dosage_and_administration":  get("dosage_and_administration"),
        "interaction":                get("interaction"),
        "contraindications":          get("contraindications"),
        "side_effects":               get("side_effects"),
        "pregnancy_and_lactation":    get("pregnancy_and_lactation"),
        "precautions_and_warnings":   get("precautions_and_warnings"),
        "use_in_special_populations": get("use_in_special_populations"),
        "overdose_effects":           get("overdose_effects"),
        "therapeutic_class":          therapeutic_class,
        "storage_conditions":         get("storage_conditions"),
        "common_questions":           from_html("question","faq"),
    }


# ─────────────────────────────────────────────
#  💾 STEP 3: Insert
# ─────────────────────────────────────────────
def insert_product(cursor, data):
    cursor.execute(INSERT_SQL, (
        data["brand_name"],         data["generic_name"],
        data["strength"],           data["dosage_form"],
        data["company_name"],       data["unit_price"],
        data["strip_price"],        data["pack_size"],
        data["image_url"],          data["source_url"],
        data["indications"],        data["pharmacology"],
        data["dosage_and_administration"], data["interaction"],
        data["contraindications"],  data["side_effects"],
        data["pregnancy_and_lactation"],   data["precautions_and_warnings"],
        data["use_in_special_populations"],data["overdose_effects"],
        data["therapeutic_class"],  data["storage_conditions"],
        data["common_questions"],
    ))


# ─────────────────────────────────────────────
#  🚀 MAIN
# ─────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("  Square Pharma Scraper v3.0 — Playwright + OCR")
    log.info(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    try:
        conn = get_db_connection()
        setup_database(conn)
    except mysql.connector.Error as e:
        log.error(f"❌ DB connection failed: {e}")
        return
    cursor = conn.cursor()

    product_links = collect_all_product_links()
    total = len(product_links)
    success = failed = no_pdf = skipped = 0

    # One Playwright browser for all PDF downloads
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            accept_downloads=True,
        )
        # Warm up: visit homepage for cookies (optional — failure is OK)
        try:
            warm = context.new_page()
            warm.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
            warm.close()
            log.info("🌐 Browser warmed up.")
        except Exception as e:
            log.warning(f"⚠️  Browser warmup skipped ({e}) — continuing anyway")
            try:
                warm.close()
            except Exception:
                pass

        for i, (pid, url) in enumerate(product_links.items(), start=1):
            # ── DB pre-check: already exists? তাহলে HTML/PDF fetch না করেই skip ──
            cursor.execute("SELECT id FROM medicines WHERE source_url = %s LIMIT 1", (url,))
            if cursor.fetchone():
                skipped += 1
                log.info(f"[{i}/{total}] ⏭️  SKIP (in DB) PID={pid}")
                continue  # HTML বা PDF fetch করো না — সরাসরি পরেরটায় যাও

            log.info(f"\n[{i}/{total}] PID={pid} → {url}")
            try:
                data = scrape_product(pid, url, context)
                if data:
                    insert_product(cursor, data)
                    conn.commit()
                    if cursor.rowcount == 0:
                        # INSERT IGNORE দিয়ে skip হয়েছে — already exists
                        skipped += 1
                        log.info(f"   ⏭️  SKIPPED (already in DB): PID={pid}")
                    else:
                        success += 1
                        has_pdf = bool(data.get("pharmacology") or data.get("indications"))
                        tag = "📑+🌐" if has_pdf else "🌐only"
                        if not has_pdf:
                            no_pdf += 1
                        log.info(
                            f"   ✅ [{tag}] {data.get('brand_name','?')} | "
                            f"{data.get('generic_name','?')} | "
                            f"strength={data.get('strength','?')} | "
                            f"form={data.get('dosage_form','?')}"
                        )
                else:
                    failed += 1
            except mysql.connector.Error as e:
                log.error(f"   ❌ DB: {e}")
                conn.rollback()
                failed += 1
            except Exception as e:
                log.error(f"   ❌ Error: {e}")
                failed += 1
            time.sleep(REQUEST_DELAY)

        context.close()
        browser.close()

    log.info("\n" + "=" * 65)
    log.info("  COMPLETE")
    log.info(f"  Total    : {total}")
    log.info(f"  Inserted : {success}  (PDF+HTML: {success-no_pdf} | HTML-only: {no_pdf})")
    log.info(f"  Skipped  : {skipped}  (⮛️ already in DB — no duplicate)")
    log.info(f"  Failed   : {failed}")
    log.info(f"  Finished : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()
