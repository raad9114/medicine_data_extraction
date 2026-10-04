# How the Software Works

This document explains the architecture, execution pipeline, internal logic, and component interactions of the **Square Pharmaceuticals Medicine Data Scraper**.

---

## 1. High-Level Overview

The system is an automated data extraction and processing pipeline designed to scrape all pharmaceutical product details from **Square Pharmaceuticals PLC** (`https://www.squarepharma.com.bd`), process their prescribing information PDFs (including scanned documents via OCR), and store structured clinical and commercial data into a **MySQL** database.

```
┌────────────────────────────────────────────────────────┐
│               Square Pharma Website                    │
│    (products-by-tradename.php?char=A..Z, 1, 5)         │
└───────────────────────────┬────────────────────────────┘
                            │ 1. Collect Links
                            ▼
┌────────────────────────────────────────────────────────┐
│            Product Directory Collector                 │
│         Extracts PID & URLs for all medicines          │
└───────────────────────────┬────────────────────────────┘
                            │ 2. Scrape Each PID
                            ▼
┌────────────────────────────────────────────────────────┐
│              HTML Parser (BeautifulSoup)               │
│  - Brand Name            - Generic Name                │
│  - Therapeutic Class     - Product Image URL           │
│  - Web FAQs / Questions  - Prescribing PDF Link        │
└───────────────┬────────────────────────────────────────┘
                │ 3. Download PDF
                ▼
┌────────────────────────────────────────────────────────┐
│       Playwright Headless Chromium Engine              │
│    Bypasses anti-bot/CDN blocks & triggers download    │
└───────────────┬────────────────────────────────────────┘
                │ 4. PDF Stream
                ▼
┌────────────────────────────────────────────────────────┐
│         PyMuPDF (fitz) + Tesseract OCR Engine          │
│   Renders PDF pages at 300 DPI -> English OCR          │
└───────────────┬────────────────────────────────────────┘
                │ 5. Raw Text Extraction
                ▼
┌────────────────────────────────────────────────────────┐
│             Regex Clinical Section Parser              │
│  Maps headings (INDICATIONS, DOSAGE, SIDE EFFECTS, etc)│
└───────────────┬────────────────────────────────────────┘
                │ 6. Duplicate-safe DB Upsert
                ▼
┌────────────────────────────────────────────────────────┐
│             MySQL Database (`medicines`)               │
│     Indexed by UNIQUE KEY on `source_url`              │
└────────────────────────────────────────────────────────┘
```

---

## 2. Core Components & Scripts

The repository contains two primary scraper implementations:

### A. `square_pharma_scraper.py` (Version 3.0 — Production Pipeline)
- **Primary script** used for complete batch runs.
- Features **Playwright Chromium integration** to reliably download prescribing PDFs directly through browser automation (preventing server drops or rate limits).
- High-resolution **PyMuPDF + Tesseract OCR** (300 DPI, Page Segmentation Mode 6).
- **Fast resume capability**: Checks MySQL first before downloading HTML or PDFs. If `source_url` exists in the database, it skips immediately.

### B. `square_scraper.py` (CLI & Modular Pipeline)
- Provides command-line options (`--pid`, `--limit`, `--dry-run`, `--output`).
- Dual PDF parsing: Attempts direct digital text extraction via `pdfplumber` first, then falls back to `pdf2image` + `pytesseract` for scanned PDFs.
- Local disk caching of PDFs (`pdfs/`) and extracted text files (`pdf_text/`).

---

## 3. Step-by-Step Processing Flow

### Step 1: Product Discovery (Directory Traversal)
- Square Pharma organizes products alphabetically by trade name across alphanumeric pages:
  `'1'`, `'5'`, and `'A'` through `'Z'`.
- The scraper queries:
  ```
  https://www.squarepharma.com.bd/products-by-tradename.php?type=pharma&char={char}
  ```
- It parses every product card anchor link matching `product-details.php?pid=\d+` using BeautifulSoup and regex.
- All unique product IDs (`pid`) and absolute URLs are mapped into an in-memory dictionary.

---

### Step 2: Resume / Duplicate Detection (Pre-flight Check)
- Before querying the network for a product page:
  ```sql
  SELECT id FROM medicines WHERE source_url = %s LIMIT 1
  ```
- If the product already exists:
  - It logs `[SKIP (in DB)]` and moves instantly to the next medicine.
  - This avoids unnecessary server load, network bandwidth usage, and re-running expensive OCR operations.

---

### Step 3: HTML Detail Extraction
For every non-cached product:
1. **Network Request**: Uses a persistent `requests.Session` configured with automatic exponential backoff retries (`HTTPAdapter` with 3 retries on 500, 502, 503, 504 errors).
2. **HTML Parsing**:
   - **Brand Name**: Located in `#toptizerpdetails h1`.
   - **Generic Name**: Extracted from `#toptizerpdetails span:first-child`.
   - **Therapeutic Class**: Extracted from `#toptizerpdetails span:nth-child(2)`.
   - **Product Pack / Preparation**: Extracted from `.prodetails p` containing `pagtitle` labels.
   - **Product Image**: Extracted from `#productdetails img[src]`.
   - **PDF Link**: Extracted from `.prescribe-btn a[href]`.

---

### Step 4: PDF Acquisition via Playwright
Many pharmaceutical web servers block standard automated download scripts (like bare `requests.get` or `urllib`) when fetching large binary files like PDF prescribing leaflets.
- `square_pharma_scraper.py` solves this by launching a **Playwright headless Chromium browser**:
  1. The browser navigates to the PDF link with realistic browser headers and `AutomationControlled` disabled.
  2. Uses `page.expect_download()` to capture the browser download stream.
  3. Saves the file temporarily, reads raw bytes into memory, and cleans up the temporary file.

---

### Step 5: Document Rendering and OCR Engine
Most prescribing inserts provided by the manufacturer are scanned image leaflets rather than digital text PDFs.
1. **Rasterization**: `fitz` (PyMuPDF) converts each PDF page into high-resolution RGB pixmap images at 300 DPI (`mat = fitz.Matrix(300 / 72, 300 / 72)`).
2. **Character Recognition**: PIL and `pytesseract` run optical character recognition on each page using:
   - Config: `--psm 6 --oem 3` (assumes a single uniform block of text, LSTM OCR engine).
   - Language: `eng` (English).
3. The resulting text pages are concatenated into a normalized text document.

---

### Step 6: Clinical Section Extraction (Regex Pattern Matching)
Medical information in leaflets follows standard clinical headings. The system scans the OCR text using regex patterns:

| Target Database Column | Section Headings / Patterns Matched |
| :--- | :--- |
| `indications` | `INDICATION`, `INDICATIONS` |
| `pharmacology` | `PHARMACOLOGY`, `CLINICAL PHARMACOLOGY` |
| `dosage_and_administration` | `DOSAGE & ADMINISTRATION`, `DOSAGE AND ADMINISTRATION` |
| `interaction` | `DRUG INTERACTION`, `INTERACTION` |
| `contraindications` | `CONTRAINDICATION`, `CONTRAINDICATIONS` |
| `side_effects` | `SIDE EFFECT`, `ADVERSE EFFECT`, `SIDE EFFECTS` |
| `pregnancy_and_lactation` | `PREGNANCY`, `LACTATION`, `PREGNANCY AND LACTATION` |
| `precautions_and_warnings` | `PRECAUTION`, `WARNING`, `WARNINGS` |
| `use_in_special_populations`| `SPECIAL POPULATION`, `PEDIATRIC`, `RENAL`, `HEPATIC` |
| `overdose_effects` | `OVERDOSE`, `OVERDOSAGE` |
| `storage_conditions` | `STORAGE`, `STORE AT`, `STORAGE CONDITIONS` |
| `pack_size` | `HOW SUPPLIED`, `PREPARATION`, `PACKAGING` |

In addition, dedicated regex extractors parse:
- **`strength`**: e.g., `500 mg`, `10 mg/ml`, `20 mcg`, `100 IU`.
- **`dosage_form`**: e.g., `Tablet`, `Capsule`, `Syrup`, `Injection`, `Ointment`, `Eye drop`.

---

### Step 7: Database Storage & Schema
The data is inserted into the `medicines` table in MySQL:

- **Deduplication Constraint**: `UNIQUE KEY uq_source_url (source_url)` ensures that duplicate rows cannot be inserted even under concurrent runs.
- **SQL Execution**: Uses `INSERT IGNORE INTO medicines (...)` to cleanly handle conflicts without throwing unhandled exceptions.
- **Transaction Handling**: Performs `conn.commit()` after each successful product so that progress is saved continuously in real time.

---

## 4. Error Handling & Resilience

1. **Polite Crawling & Rate Limiting**: Built-in delay (`REQUEST_DELAY = 3.0` seconds) prevents the scraper from getting banned or stressing the source server.
2. **HTTP Timeouts**: 45-second network request timeouts with backoff intervals.
3. **OCR / PDF Failures**: If a PDF is missing, corrupt, or unreadable, the scraper falls back gracefully to saving all available HTML page data (marked as `🌐only` in logs), rather than halting execution.
4. **Logging**: All events, warnings, network retries, and errors are logged both to standard output (console) and to `scraper.log`.

---

## 5. Technical Stack Summary

- **Programming Language**: Python 3.10+
- **HTTP Engine**: `requests` with retry adapters
- **Browser Automation**: `playwright` (Chromium)
- **HTML Parsing**: `beautifulsoup4`, `lxml`
- **PDF & OCR**: `PyMuPDF` (`fitz`), `Pillow`, `pytesseract`, `Tesseract-OCR`
- **Database**: MySQL / MariaDB via `mysql-connector-python` or `pymysql`
