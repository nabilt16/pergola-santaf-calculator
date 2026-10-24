# ניהול פרגולות חכם (Smart Pergola Management)

Streamlit app with two modules, switched at the top of the page: **סנטפים** (the polycarbonate/Santaf panel
calculator) and **לדים** (LED lighting reports). The Santaf module is unchanged by the LED module.

## Structure

- `app.py` – Streamlit UI (file / scan / folder input, prices, reports)
- `pdf_ingest.py` – digital PDF reports (text layer, no OCR; columns recognised by content)
- `ocr_ingest.py` – photo/scan OCR (Tesseract `heb`+`eng`) that rebuilds the order table
- `tessdata/` – Tesseract language data used by the OCR (`eng`, `heb`)
- `calculator.py` – all Santaf business rules (rounding, splitting, pricing, summaries)
- `led_ingest.py` – reads the LED report PDFs (rows, tick boxes, notes, status)
- `led.py` – LED rules: order types, regions, strip splitting, transformer sizing, tracking
- `led_ui.py` – the LED screens (loaded only when the LED module is selected)
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

## Installing on a phone (PWA)

The app ships a web-app manifest (`static/manifest.json`, name "ניהול פרגולות חכם", standalone display,
icons from `static/`) and `pwa.py` links it into the page, so Chrome can install it. Streamlit only
serves the `static/` folder (at `/app/static/`), which is why the manifest lives there and not in the
project root; `enableStaticServing = true` in `.streamlit/config.toml` turns that on.

**Android (Chrome):**
1. Open the app's address in Chrome and let it finish loading (wait about five seconds).
2. Tap the **⋮** menu at the top right.
3. Tap **Install app** (or **Add to Home screen**, then **Install** in the dialog). Keep or edit the
   name "פרגולות" and confirm.
4. Open it from the new icon on the home screen: it starts full screen without the address bar.
   If the menu only offers **Add shortcut**, the icon opens in a normal Chrome tab; see below.

**If the installed app shows "Streamlit" or the default icon:** Community Cloud wraps every app in
its own page, so it can ignore a manifest added from inside. Use the wrapper in `docs/` instead:
1. Edit `docs/config.js` and put your app's address in `APP_URL`.
2. Commit and push, then on GitHub: *Settings > Pages > Deploy from a branch > `main` / `docs`*.
3. Open the Pages address (`https://<you>.github.io/<repo>/`) in Chrome on the phone and install
   from there (steps above). The wrapper page owns the name and icon and shows your app full screen.
   The app must be public (not "private/login") for this to work, because a login cannot run inside
   the embedded frame.

The app needs a live connection to its server, so it does not work offline.

## LED module (לדים)

Reads the "תאורת לד לפרגולה" PDF reports (several at once; a folder on this computer or uploads).
Each report line gives the order number, coordination date, **code**, zone, LED length (mm), quantity, item
status, the ordered / received / supplied ticks and the note. The printed totals at the end of every
report are checked against what was read.

- **Code 6** (full pergola) and **code 67** (repair / service / add-on) are always kept apart: separate
  totals, a type filter, and a type column everywhere. Notes such as "הלדים על הזמנה ראשית 839413" are linked
  to the main order.
- **Regions:** the reports only carry zone codes (20, 50, 55, 70). The mapping of each code to צפון / מרכז / דרום is
  set in the *הגדרות* tab (it starts unassigned) and saved to `data/led_settings.json`.
- **Status tracking:** the report's own statuses and ticks give the stage (טרם הוזמן, הוזמן, נתקבל, סופק); the
  *מעקב* tab adds manual fields per line (external kit ordered, reference, note), saved to
  `data/led_tracking.json`, with a JSON backup download/restore (a cloud host's disk is temporary).
- **Inventory preparation (information only, kits are still ordered externally):** the *קיטים (הכנה)* tab
  computes what a central stock would need. Specification, all editable in *הגדרות*: COB strips of about
  180 LEDs per meter drawing **12 W per meter** (a 4 m strip is 48 W net); strips of 1000-4000 mm, a longer
  length is cut into pieces of at most 4000 mm with none under 1000 mm (6000 = 4000 + 2000, 4200 = 3200 + 1000);
  a transformer is loaded to at most **80%** of its rating (60 W up to 48 W, so a 4 m strip just fits a 60 W unit
  such as the ENERLED NR-60-24-CD). All strips of one order and code are summed and bundled on **one central
  transformer** (for example 4 + 2.5 + 2 + 1 m = 9.5 m = 114 W gets a 250 W unit); only a load above the safe
  limit of the largest transformer is shared between several. The tab shows the safe load, the strip meters each
  rating carries, and for every kit the load as a percentage of its safe limit.
  *To confirm:* the ratings in stock. The defaults are 60, 100, 250 and 500 W (only 60 W and 250 W were named);
  if, say, a 150 W unit is stocked, 9.5 m would take that instead of 250 W.
