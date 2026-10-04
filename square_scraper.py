#!/usr/bin/env python3
"""
Square Pharmaceuticals product scraper -> MySQL

Flow:
  1. products-by-tradename.php er sob letter page (A, B, C ...) theke product pid collect
  2. protita product-details.php?pid=X page theke brand, generic, therapeutic class,
     preparation (pack size), image, prescribing PDF link
  3. PDF download -> text (pdfplumber). Text na thakle (scanned PDF) -> OCR (tesseract)
  4. PDF text ke heading dhore section e bhag kore column e map
  5. MySQL e upsert (source_url unique)

Usage:
  python square_scraper.py                 # sob product
  python square_scraper.py --limit 5       # test: prothom 5 ta
  python square_scraper.py --pid 147       # ekta product
  python square_scraper.py --dry-run       # DB te na likhe JSON print
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, quote, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------
# Config (env variable diye override kora jay)
# ----------------------------------------------------------------------------
BASE = "https://www.squarepharma.com.bd/"
LIST_URL = BASE + "products-by-tradename.php"
DETAIL_URL = BASE + "product-details.php?pid={pid}"
COMPANY_NAME = "Square Pharmaceuticals PLC"

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "medicine_db"),
    charset="utf8mb4",
)
TABLE = os.getenv("DB_TABLE", "medicines")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", TABLE):
    raise ValueError("DB_TABLE must be a simple SQL identifier")

DELAY = float(os.getenv("SCRAPE_DELAY", "1.5"))   # request er majhe second
PDF_DIR = Path(os.getenv("PDF_DIR", "pdfs"))       # PDF cache
TEXT_DIR = Path(os.getenv("TEXT_DIR", "pdf_text")) # extract kora text (debug er jonno)
OCR_LANG = os.getenv("OCR_LANG", "eng")

log = logging.getLogger("square")

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    })
    retry = Retry(total=4, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def safe_url(url):
    """URL e space thakle encode kore dey (PDF/image name e space thake)."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, quote(p.path, safe="/%"), p.query, p.fragment))


def get(session, url, **kw):
    time.sleep(DELAY)
    r = session.get(safe_url(url), timeout=40, **kw)
    r.raise_for_status()
    return r


# ----------------------------------------------------------------------------
# Step 1: list page theke pid
# ----------------------------------------------------------------------------
def collect_pids(session):
    first = BeautifulSoup(get(session, LIST_URL).text, "html.parser")

    # letter tab gulo page theke nei (1, 5, A..Z), hardcode na kore
    letter_urls = []
    for a in first.select("a[href*='products-by-tradename.php?']"):
        href = a.get("href", "")
        if "type=pharma" in href and "char=" in href:
            u = urljoin(BASE, href)
            if u not in letter_urls:
                letter_urls.append(u)
    log.info("%d ta letter page pawa gelo", len(letter_urls))

    pids = {}
    pages = [(LIST_URL, first)] + [(u, None) for u in letter_urls]
    for url, soup in pages:
        if soup is None:
            try:
                soup = BeautifulSoup(get(session, url).text, "html.parser")
            except Exception as e:
                raise RuntimeError(f"List page failed; refusing an incomplete catalog: {url}") from e
        for a in soup.select("a[href*='product-details.php?pid=']"):
            m = re.search(r"pid=(\d+)", a["href"])
            if m:
                pids.setdefault(int(m.group(1)), urljoin(BASE, a["href"]))
        log.info("%s -> total %d pid", url.split("?")[-1], len(pids))
    if not pids:
        raise ValueError("No products found; check site markup or connectivity")
    return sorted(pids)


# ----------------------------------------------------------------------------
# Step 2: detail page
# ----------------------------------------------------------------------------
PAGE_LABELS = {
    "indication": "indications",
    "indications": "indications",
    "dosage & administration": "dosage_and_administration",
    "dosage and administration": "dosage_and_administration",
    "preparation": "preparation",
    "preparations": "preparation",
}


def clean_brand(name):
    name = re.sub(r"[®™©]", "", name)
    name = re.sub(r"(?<=[a-z])TM\b", "", name)   # "AdsaltTM" -> "Adsalt"
    return re.sub(r"\s+", " ", name).strip()


def norm_space(t):
    t = re.sub(r"[ \t\xa0]+", " ", t or "")
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def parse_detail(html, pid):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        raise ValueError("h1 (brand) nai")

    # h1 theke upore uthe emon container khuji jekhane product er content ase
    box = h1.parent
    for _ in range(6):
        txt = box.get_text(" ", strip=True).lower()
        if "preparation" in txt or "indication" in txt or box.find("a", href=re.compile(r"\.pdf", re.I)):
            break
        if box.parent is None:
            break
        box = box.parent

    # <sup>®</sup> alada line e bhenge jay, tai age inline kore dei
    for tag in box.find_all(["sup", "sub"]):
        tag.unwrap()
    # Keep table cells together and expand rowspan labels for readable doses.
    for table in box.find_all("table"):
        pending, rows = {}, []
        for tr in table.find_all("tr"):
            cells, index = [], 0
            def carry():
                nonlocal index
                while index in pending:
                    value, remaining = pending[index]
                    cells.append(value)
                    if remaining <= 1:
                        del pending[index]
                    else:
                        pending[index] = (value, remaining - 1)
                    index += 1
            for td in tr.find_all(["td", "th"], recursive=False):
                carry()
                value = td.get_text(" ", strip=True)
                cells.append(value)
                span = int(td.get("rowspan", 1))
                if span > 1:
                    pending[index] = (value, span - 1)
                index += 1
            carry()
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))
    data = {"brand_name": clean_brand(h1.get_text("", strip=True))}

    # therapeutic class: italic text
    em = box.find(["em", "i"])
    data["therapeutic_class"] = norm_space(em.get_text(" ", strip=True)) if em else None
    tc = data["therapeutic_class"]
    if tc and tc.startswith("(") and tc.endswith(")"):
        data["therapeutic_class"] = tc[1:-1].strip()

    # box er text line by line: h1 er porer prothom line = generic
    lines = [l.strip() for l in box.get_text("\n").split("\n") if l.strip()]
    brand_raw = h1.get_text("", strip=True)
    generic = None
    try:
        i = next(k for k, l in enumerate(lines) if l == brand_raw or clean_brand(l) == data["brand_name"])
        for l in lines[i + 1:]:
            if l.rstrip(":").lower() in PAGE_LABELS:
                break
            if data["therapeutic_class"] and data["therapeutic_class"] in l:
                continue
            if not re.search(r"[A-Za-z]", l):
                continue
            generic = l
            break
    except StopIteration:
        pass
    data["generic_name"] = generic

    # labelled sections (Indication:, Dosage & Administration:, Preparation:)
    sections, cur = {}, None
    for l in lines:
        key = l.rstrip(":").strip().lower()
        if key in PAGE_LABELS:
            cur = PAGE_LABELS[key]
            sections.setdefault(cur, [])
            continue
        if l.lower().startswith("view prescribing"):
            cur = None
            continue
        if cur:
            sections[cur].append(l)
    for k, v in sections.items():
        sections[k] = norm_space("\n".join(v)) or None
    data["page_sections"] = sections

    # images
    imgs = []
    for img in box.find_all("img"):
        src = img.get("src") or ""
        if "/products/" in src or src.startswith("products/"):
            u = urljoin(BASE, src)
            if u not in imgs:
                imgs.append(u)
    data["images"] = imgs

    # PDF
    a = next((a for a in box.select("a[href]")
              if re.search(r"\.pdf(?:$|\?)", a["href"], re.I)
              and ("prescribing" in a.get_text(" ", strip=True).lower()
                   or "_pdoc_" in a["href"])), None)
    data["pdf_url"] = urljoin(BASE, a["href"]) if a else None
    return data


# ----------------------------------------------------------------------------
# Step 3: PDF -> text (pdfplumber, na hole OCR)
# ----------------------------------------------------------------------------
def pdf_to_text(session, pdf_url, pid):
    PDF_DIR.mkdir(exist_ok=True)
    TEXT_DIR.mkdir(exist_ok=True)
    # URL-specific cache prevents reusing older, unrelated PDFs for the same pid.
    cache_key = f"{pid}_{hashlib.sha256(pdf_url.encode()).hexdigest()[:16]}"
    pdf_path = PDF_DIR / f"{cache_key}.pdf"
    txt_path = TEXT_DIR / f"{cache_key}.txt"

    if txt_path.exists() and txt_path.stat().st_size > 50:
        return txt_path.read_text(encoding="utf-8")

    if not pdf_path.exists():
        content = get(session, pdf_url).content
        if not content.lstrip().startswith(b"%PDF-"):
            raise ValueError("Prescribing link did not return a PDF")
        pdf_path.write_bytes(content)

    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        log.warning("pid %s: pdfplumber fail: %s", pid, e)

    if len(text.strip()) < 100:
        log.info("pid %s: PDF e text nai, OCR cholche...", pid)
        text = ocr_pdf(pdf_path)

    txt_path.write_text(text, encoding="utf-8")
    return text


def ocr_pdf(pdf_path):
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        log.error("OCR er jonno pdf2image + pytesseract install koro (README dekho)")
        return ""
    out = []
    for img in convert_from_path(str(pdf_path), dpi=300):
        out.append(pytesseract.image_to_string(img, lang=OCR_LANG))
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Step 4: PDF text -> sections
# ----------------------------------------------------------------------------
# heading alias -> column. Upore beshi specific gula age.
PDF_HEADINGS = [
    (r"indications?(?: (?:and|&) usage)?|therapeutic indications?|uses?", "indications"),
    (r"pharmacology|clinical pharmacology|pharmacodynamics?(?: properties)?|pharmacokinetics?(?: properties)?|"
     r"mode of action|mechanism of action|pharmacological properties", "pharmacology"),
    (r"dosage (?:and|&) administration|dosage|posology(?: and method of administration)?|dose|"
     r"direction(?:s)? for use|administration|recommended dosage?", "dosage_and_administration"),
    (r"(?:drug )?interactions?(?: with other (?:medicines|medicinal products|drugs))?", "interaction"),
    (r"contra-?indications?", "contraindications"),
    (r"side[- ]effects?|adverse (?:effects?|reactions?|events?|drug reactions?)|undesirable effects?", "side_effects"),
    (r"(?:use (?:in|during) )?pregnancy(?:,)? (?:and|&) (?:lactation|breast[- ]?feeding|nursing mothers?)|"
     r"(?:use (?:in|during) )?pregnancy|lactation|nursing mothers?|fertility, pregnancy (?:and|&) lactation",
     "pregnancy_and_lactation"),
    (r"warnings? (?:and|&) precautions?|precautions? (?:and|&) warnings?|special warnings?.*|precautions?|warnings?",
     "precautions_and_warnings"),
    (r"use in special populations?|special populations?|(?:paediatric|pediatric|geriatric) use|"
     r"use in (?:children|elderly|paediatrics?|pediatrics?|renal impairment|hepatic impairment)|"
     r"renal impairment|hepatic impairment|elderly", "use_in_special_populations"),
    (r"over-?dos(?:e|age)(?: effects?)?|overdose (?:and|&) treatment", "overdose_effects"),
    (r"storage(?: conditions?)?|storage (?:and|&) handling|store|how to store", "storage_conditions"),
    # extra (DB te sorasori jay na, strength/pack size ber korte lage)
    (r"composition|presentation|description|qualitative (?:and|&) quantitative composition", "_composition"),
    (r"how supplied|packaging|pack(?:ing)? size|commercial pack|package quantities|preparations?", "_supply"),
    (r"therapeutic class|pharmacotherapeutic group", "_class"),
]
_HEADING_RES = [(re.compile(rf"^(?:{p})$", re.I), col) for p, col in PDF_HEADINGS]


def match_heading(text):
    t = re.sub(r"^[\d.\s]+", "", text)          # "4.1 Indications" -> "Indications"
    t = t.strip(" :-–—\t").strip()
    if not t or len(t) > 70:
        return None
    for rx, col in _HEADING_RES:
        if rx.match(t):
            return col
    return None


def split_pdf_sections(text):
    sections, order = {}, []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        col = match_heading(line)
        rest = ""
        if col is None and ":" in line:
            head, rest = line.split(":", 1)
            col = match_heading(head)
            if col is None:
                rest = ""
        if col:
            cur = col
            if col not in sections:
                sections[col] = []
                order.append(col)
            elif col in ("pharmacology", "use_in_special_populations", "precautions_and_warnings"):
                # sub-heading (jemon Pharmacokinetics) label soho rakhi
                sections[col].append(f"\n{line.split(':')[0].strip()}:")
            if rest.strip():
                sections[col].append(rest.strip())
            continue
        if cur:
            sections[cur].append(line)
    return {k: norm_space(fix_wrapping("\n".join(v))) or None for k, v in sections.items()}


def fix_wrapping(t):
    """PDF er line-break e bhanga sentence joda lagay; bullet/notun sentence alada thake."""
    out = []
    for line in t.split("\n"):
        if out and out[-1] and not re.search(r"[.:;!?]$", out[-1]) \
                and not re.match(r"^[•\-–*●▪]|^\d+[.)]\s", line) and line[:1].islower():
            out[-1] += " " + line
        else:
            out.append(line)
    return "\n".join(out)


# ----------------------------------------------------------------------------
# strength / dosage form / pack size
# ----------------------------------------------------------------------------
DOSAGE_FORMS = [
    "Film-coated Tablet", "Chewable Tablet", "Dispersible Tablet", "Effervescent Tablet",
    "Orally Disintegrating Tablet", "Extended Release Tablet", "Sustained Release Tablet", "Tablet",
    "Capsule", "Paediatric Drops", "Pediatric Drops", "Eye Drops", "Ear Drops", "Nasal Drops",
    "Nasal Spray", "Oral Spray", "Syrup", "Suspension", "Suppository", "IV Infusion", "Infusion",
    "Injection", "Pen Cartridge", "Cartridge", "Vial", "Pre-filled Syringe", "Cream", "Ointment",
    "Gel", "Lotion", "Shampoo", "Powder", "Sachet", "Granules", "Inhaler", "Respirator Solution",
    "Oral Solution", "Solution", "Emulsion", "Mouthwash", "Enema", "Patch", "Spray", "Drops",
]
_FORM_RES = [(f, re.compile(rf"\b{re.escape(f)}s?\b|\b{re.escape(f[:-1])}ies\b" if f.endswith("y")
                            else rf"\b{re.escape(f)}s?\b", re.I)) for f in DOSAGE_FORMS]

STRENGTH_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:mg|mcg|µg|μg|gm|g|IU|units?|%)"
    r"(?:\s?(?:/|per)\s?\d*(?:\.\d+)?\s?(?:ml|gm|g|tablet|capsule|dose|actuation|drop|vial))?(?![A-Za-z])", re.I,
)


def find_dosage_forms(*texts):
    blob = " ".join(t for t in texts if t)
    hits = []
    for form, rx in _FORM_RES:
        m = rx.search(blob)
        if m and not any(form.lower() in h[1].lower() for h in hits):
            hits.append((m.start(), form))
    hits.sort()
    return ", ".join(dict.fromkeys(f for _, f in hits)) or None


def find_strengths(*texts):
    found = []
    for t in texts:
        for m in STRENGTH_RE.finditer(t or ""):
            if re.search(r"each\s*$", (t or "")[max(0, m.start() - 8):m.start()], re.I):
                continue  # "Each 5 ml contains" -> 5 ml strength na
            s = re.sub(r"\s+", " ", m.group(0)).strip()
            # Preserve the liquid concentration denominator from composition.
            start = (t or "").rfind("\n", 0, m.start()) + 1
            prefix = (t or "")[start:m.start()]
            volume = re.search(r"\bEach\s+(?:(\d+(?:\.\d+)?)\s*)?(ml|g)\b", prefix, re.I)
            if volume and "/" not in s:
                s += "/" + (volume.group(1) or "1") + " " + volume.group(2).lower()
            if s.lower() not in (x.lower() for x in found):
                found.append(s)
    return ", ".join(found) or None


PACK_RE = re.compile(
    r"\b\d+(?:\s?(?:&|and|,)\s?\d+)*\s?(?:tablets?|capsules?|suppositor(?:y|ies)|sachets?|vials?|"
    r"ampoules?|cartridges?|pens?|strips?|ml|mL|gm|g)\b", re.I)


def find_pack_sizes(text):
    """ "Each box contains 250 & 500 tablets..." -> "250 & 500 tablets, 60 ml, ..." """
    found = []
    for m in PACK_RE.finditer(text or ""):
        s = re.sub(r"\s+", " ", m.group(0)).strip()
        if s.lower() not in (x.lower() for x in found):
            found.append(s)
    return ", ".join(found) or None


def cut(v, n):
    return v[:n] if isinstance(v, str) and len(v) > n else v


# ----------------------------------------------------------------------------
# Row build
# ----------------------------------------------------------------------------
def build_row(session, pid):
    url = DETAIL_URL.format(pid=pid)
    page = parse_detail(get(session, url).text, pid)
    ps = page["page_sections"]

    pdf = {}
    if page["pdf_url"]:
        try:
            pdf = split_pdf_sections(pdf_to_text(session, page["pdf_url"], pid))
        except Exception as e:
            log.warning("pid %s: PDF process fail: %s", pid, e)
    else:
        log.info("pid %s: PDF link nai", pid)

    composition = pdf.get("_composition") or ""
    supply = ps.get("preparation") or pdf.get("_supply") or ""

    # strength: age composition, na pele pack text, tao na pele brand name
    strength = find_strengths(composition) or find_strengths(supply, page["brand_name"])
    dosage_form = find_dosage_forms(page["brand_name"], supply, composition)

    row = {
        "pid": pid,
        "brand_name": cut(page["brand_name"], 255),
        "generic_name": cut(page["generic_name"], 255),
        "strength": cut(strength, 255),
        "dosage_form": cut(dosage_form, 255),
        "company_name": COMPANY_NAME,
        "unit_price": None,       # site e price nai
        "strip_price": None,
        "pack_size": cut(find_pack_sizes(supply), 100),
        "pack_details": supply or None,
        "image_url": cut(page["images"][0], 500) if page["images"] else None,
        "source_url": url,
        "pdf_url": cut(page["pdf_url"], 500),
        # PDF age, na thakle web page
        "indications": pdf.get("indications") or ps.get("indications"),
        "pharmacology": pdf.get("pharmacology"),
        "dosage_and_administration": ps.get("dosage_and_administration") or pdf.get("dosage_and_administration"),
        "interaction": pdf.get("interaction"),
        "contraindications": pdf.get("contraindications"),
        "side_effects": pdf.get("side_effects"),
        "pregnancy_and_lactation": pdf.get("pregnancy_and_lactation"),
        "precautions_and_warnings": pdf.get("precautions_and_warnings"),
        "use_in_special_populations": pdf.get("use_in_special_populations"),
        "overdose_effects": pdf.get("overdose_effects"),
        "therapeutic_class": page["therapeutic_class"] or pdf.get("_class"),
        "storage_conditions": pdf.get("storage_conditions"),
        "common_questions": None,  # site e nai
    }
    return row


# ----------------------------------------------------------------------------
# Step 5: MySQL
# ----------------------------------------------------------------------------
CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS `{TABLE}` (
  `id` int NOT NULL AUTO_INCREMENT,
  `brand_name` varchar(255) DEFAULT NULL,
  `generic_name` varchar(255) DEFAULT NULL,
  `strength` varchar(255) DEFAULT NULL,
  `dosage_form` varchar(255) DEFAULT NULL,
  `company_name` varchar(255) DEFAULT NULL,
  `unit_price` varchar(50) DEFAULT NULL,
  `strip_price` varchar(50) DEFAULT NULL,
  `pack_size` varchar(100) DEFAULT NULL,
  `image_url` varchar(500) DEFAULT NULL,
  `source_url` varchar(500) DEFAULT NULL,
  `indications` text DEFAULT NULL,
  `pharmacology` text DEFAULT NULL,
  `dosage_and_administration` text DEFAULT NULL,
  `interaction` text DEFAULT NULL,
  `contraindications` text DEFAULT NULL,
  `side_effects` text DEFAULT NULL,
  `pregnancy_and_lactation` text DEFAULT NULL,
  `precautions_and_warnings` text DEFAULT NULL,
  `use_in_special_populations` text DEFAULT NULL,
  `overdose_effects` text DEFAULT NULL,
  `therapeutic_class` text DEFAULT NULL,
  `storage_conditions` text DEFAULT NULL,
  `common_questions` text DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_source_url` (`source_url`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

COLUMNS = [
    "pid", "brand_name", "generic_name", "strength", "dosage_form", "company_name",
    "unit_price", "strip_price", "pack_size", "pack_details", "image_url", "source_url", "pdf_url",
    "indications", "pharmacology", "dosage_and_administration", "interaction",
    "contraindications", "side_effects", "pregnancy_and_lactation", "precautions_and_warnings",
    "use_in_special_populations", "overdose_effects", "therapeutic_class",
    "storage_conditions", "common_questions",
]
# Compatible with the user's existing medicine_db.sql, without ALTER TABLE.
COLUMNS = [c for c in COLUMNS if c not in {"pid", "pdf_url", "pack_details"}]


def db_connect():
    import pymysql
    conn = pymysql.connect(**DB, autocommit=False)
    with conn.cursor() as c:
        c.execute(CREATE_SQL)
    conn.commit()
    return conn


def upsert(conn, row):
    cols = ", ".join(f"`{c}`" for c in COLUMNS)
    ph = ", ".join(["%s"] * len(COLUMNS))
    # Existing user tables need not have a UNIQUE index on source_url.
    # Run only one importer at a time for such tables.
    with conn.cursor() as c:
        c.execute(f"SELECT `id` FROM `{TABLE}` WHERE `source_url`=%s LIMIT 1", [row["source_url"]])
        existing = c.fetchone()
        if existing:
            update_cols = [k for k in COLUMNS if k != "source_url" and row.get(k) is not None]
            upd = ", ".join(f"`{k}`=%s" for k in update_cols)
            c.execute(f"UPDATE `{TABLE}` SET {upd} WHERE `id`=%s",
                      [row[k] for k in update_cols] + [existing[0]])
        else:
            c.execute(f"INSERT INTO `{TABLE}` ({cols}) VALUES ({ph})", [row.get(k) for k in COLUMNS])
    conn.commit()


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, action="append", help="shudhu ei pid (bar bar deya jay)")
    ap.add_argument("--limit", type=int, help="prothom N ta product")
    ap.add_argument("--dry-run", action="store_true", help="DB te na likhe JSON print")
    ap.add_argument("--output", help="Save full records as UTF-8 JSONL, including PDF URL and preparation")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("scraper.log", encoding="utf-8")],
    )

    session = make_session()
    pids = args.pid or collect_pids(session)
    if args.limit:
        pids = pids[: args.limit]
    log.info("Mot %d ta product process hobe", len(pids))

    conn = None if args.dry_run else db_connect()
    ok, failed = 0, []
    output = open(args.output, "w", encoding="utf-8") if args.output else None
    for i, pid in enumerate(pids, 1):
        try:
            row = build_row(session, pid)
            if args.dry_run:
                print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                upsert(conn, row)
            if output:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
            filled = sum(1 for k in COLUMNS if row.get(k))
            log.info("[%d/%d] pid %s %s -> OK (%d/%d field)",
                     i, len(pids), pid, row["brand_name"], filled, len(COLUMNS))
            ok += 1
        except Exception as e:
            if conn:
                conn.rollback()
            log.error("[%d/%d] pid %s FAIL: %s", i, len(pids), pid, e)
            failed.append(pid)

    if conn:
        conn.close()
    if output:
        output.close()
    session.close()
    log.info("Shesh: %d OK, %d fail", ok, len(failed))
    if failed:
        Path("failed_pids.txt").write_text("\n".join(map(str, failed)))
        log.info("Fail pid gulo failed_pids.txt e ase")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
