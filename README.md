# Pergola Santaf Calculator

Streamlit app for processing polycarbonate (Santaf) panel orders for pergolas.

## Structure

- `app.py` – Streamlit UI (file / scan / folder input, prices, reports)
- `pdf_ingest.py` – digital PDF reports (text layer, no OCR; columns recognised by content)
- `ocr_ingest.py` – photo/scan OCR (Tesseract `heb`+`eng`) that rebuilds the order table
- `tessdata/` – Tesseract language data used by the OCR (`eng`, `heb`)
- `calculator.py` – all business rules (rounding, splitting, pricing, summaries)
- `tests/test_calculator.py` – unit tests

## Run locally (VS Code, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt   # runtime + tests (requirements.txt is runtime only)
python -m pytest
streamlit run app.py
```

In VS Code choose the `.venv` interpreter (Ctrl+Shift+P → "Python: Select Interpreter").

## Rules

- Quantity: rounded up to an integer.
- Length: rounded up to a multiple of 500 mm.
- Length >= 8000 mm: two panels summing to length + 1000 mm (1000 mm overlap), each <= 8000 mm.
  The longer panel is half the sum rounded up to 1000 mm (8000 → 5000+4000, 10000 → 6000+5000).
  Lengths above 15000 mm raise an error. Original quantity applies to each part.
- Weeks of the month: days 1–7 = week 1, 8–14 = week 2, ... 29+ = week 5.
- Dates are parsed day-first when ambiguous.

## Scanning photos of order schedules

Needs the Tesseract engine (`winget install UB-Mannheim.TesseractOCR`); the Hebrew and English
language data is already in `tessdata/`. Right-to-left Hebrew tables are detected automatically.
Extracted rows are shown in an editable table with a **Check** column; correct them before
trusting the totals. Photos of 2000 px width or more, taken straight on, read much better.

## PDF reports

Digital PDFs (text can be selected) are read straight from their text layer, so digits and
decimal points are exact. Columns are recognised by their content (dates, order ids, numbers
beside them), not by header text, so Hebrew headers stored as unreadable glyphs are fine. When
the report prints its own totals, the app checks them against the rows it read. Scanned PDFs have
no text layer: use the photo/scan option for those.

## Sending reports by email

The **Send by email** tab builds a right-to-left HTML report (consumption by length, orders split at
8000 mm and above, optionally the weekly matrix and cost per order) and can attach all tables as one
Excel file. It sends through Gmail SMTP. Add to `.streamlit/secrets.toml` (see
`.streamlit/secrets.toml.example`):

```toml
[email]
sender = "you@gmail.com"
password = "xxxx xxxx xxxx xxxx"   # a Gmail *app password*, not your normal password
```

Use `allowed_recipients = [...]` to restrict who can receive reports. Recipient history is kept per
browser session only, and each session can send at most 10 messages.

## Deploying to Streamlit Community Cloud (works on a phone)

1. Create a GitHub repository (private is fine) and push this folder:
   ```powershell
   git add -A
   git status          # check: no secrets.toml, no customer files (.pdf/.xlsx/.csv/photos are ignored)
   git commit -m "Pergola Santaf calculator"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. On https://share.streamlit.io choose **New app**, pick the repo, branch `main`, main file `app.py`.
   Open **Advanced settings**: Python 3.12, and paste your secrets (the same TOML as
   `.streamlit/secrets.toml.example`, with real values).
3. Deploy. The cloud installs `requirements.txt` (Python packages) and `packages.txt` (the Tesseract
   OCR engine; the Hebrew/English language data comes from `tessdata/`).
4. Open the app's URL on your phone. The date range and Order ID search are in the sidebar (the arrow
   at the top left); the camera option in "Scan / photo of a document" opens the phone camera.

Before sharing the URL: set `app_password` in the secrets (the app shows customer orders and can
send email), or make the app private in Streamlit's sharing settings. The "Local folder" source is
hidden on the cloud because it would read the server's disk, not yours. Uploaded files are processed
in memory and are not stored by the app.
