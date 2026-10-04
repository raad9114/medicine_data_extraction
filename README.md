# Square Pharmaceuticals to MySQL

Trade-name list-এর সব letter page থেকে product সংগ্রহ করে product page ও prescribing PDF পড়ে। Scanned PDF হলে OCR ব্যবহার করে। না পাওয়া তথ্য `NULL` থাকে।

## Setup — Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Scanned PDF-এর জন্য Tesseract ও Poppler install করে executable folder PATH-এ রাখুন। এই workspace-এর বর্তমান পরিবেশে দুটোই পাওয়া গেছে। অন্য কম্পিউটারে `tesseract --version` ও `pdftoppm -v` দিয়ে পরীক্ষা করুন। Ubuntu: `sudo apt install tesseract-ocr poppler-utils`।

## Database

MySQL/phpMyAdmin-এ একবার database তৈরি করুন:

```sql
CREATE DATABASE IF NOT EXISTS medicine_db
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

```powershell
$env:DB_HOST = '127.0.0.1'
$env:DB_PORT = '3306'
$env:DB_USER = 'root'
$env:DB_PASSWORD = 'your_password'
$env:DB_NAME = 'medicine_db'
$env:DB_TABLE = 'medicines'
```

টেবিল না থাকলে script তৈরি করে। আপনার `medicine_db.sql` দিয়ে তৈরি বিদ্যমান টেবিলেও চলে; extra column লাগে না। `source_url` মিলে গেলে update করে, না মিললে insert করে। পুনরায় চালানোর সময় না পাওয়া field দিয়ে আগের তথ্য মুছে দেয় না। বিদ্যমান টেবিলে unique index না থাকলে একসঙ্গে একটির বেশি importer চালাবেন না। আগে থেকে duplicate থাকলে সেগুলো সরায় না।

## Run

```powershell
# DB-তে না লিখে preview ও JSONL export
.\.venv\Scripts\python.exe square_scraper.py --pid 147 --dry-run --output preview.jsonl

# একটি product MySQL-এ insert/update
.\.venv\Scripts\python.exe square_scraper.py --pid 147

# প্রথম 10টি
.\.venv\Scripts\python.exe square_scraper.py --limit 10

# সব product
.\.venv\Scripts\python.exe square_scraper.py --output square_products.jsonl

# নির্দিষ্ট product পুনরায় চালানো
.\.venv\Scripts\python.exe square_scraper.py --pid 147 --pid 148

# Regression checks
.\.venv\Scripts\python.exe -m unittest -v
```

## Data behaviour and limits

- প্রতি product page-এ একটি row; Ace page-এর মতো একাধিক formulation থাকলে strength/form-এ একাধিক মান থাকে। আলাদা SKU row নয়।
- Requested 23 data columns ও `created_at` সমর্থিত। `unit_price`, `strip_price`, `common_questions` এই extractor পূরণ করে না; নতুন row-এ `NULL`। অন্য field source-এ পাওয়া গেলে পূরণ হয়।
- PDF section-heading মিলিয়ে তথ্য নেয়। সব PDF-এর layout এক নয়, তাই কিছু section বাদ পড়তে পারে। OCR ভুল করতে পারে; প্রকাশের আগে source-এর সঙ্গে যাচাই করুন।
- Dosage-এর জন্য HTML আগে ব্যবহার করে, কারণ PDF OCR table-এর dose/frequency এলোমেলো করতে পারে। HTML table cells ` | ` দিয়ে রাখা হয়।
- `sample_147.json` বাস্তব Ace HTML ও OCR PDF থেকে তৈরি নমুনা।
- `--output` JSONL-এ অতিরিক্ত `pid`, `pdf_url`, পূর্ণ `pack_details` থাকে; এগুলো MySQL column নয়। VARCHAR সীমা ছাড়ালে DB value ছোট করা হয়।
- PDF/text cache URL hash দিয়ে চিহ্নিত। পুরোনো `pdfs/147.pdf` / `pdf_text/147.txt` আর ব্যবহৃত হয় না: সেগুলো ভুল corporate report হতে পারে। একই URL-এর PDF বদলালে সংশ্লিষ্ট hashed cache সরিয়ে পুনরায় চালান।
- Request delay 1.5 seconds; HTTP retry আছে। `SCRAPE_DELAY`, `PDF_DIR`, `TEXT_DIR`, `OCR_LANG` environment variable দিয়ে configuration বদলানো যায়।
- PDF/OCR ব্যর্থ হলে warning দিয়ে page-এর তথ্য রাখা হয়। Product ব্যর্থ হলে `failed_pids.txt` ও `scraper.log` দেখুন; exit code 1 হয়। Catalog page ব্যর্থ হলে অসম্পূর্ণ catalog নিয়ে এগোয় না।

বাস্তব MySQL server-এ insert পরীক্ষা করা হয়নি; আগে `--pid 147` দিয়ে নিজের connection পরীক্ষা করুন।
