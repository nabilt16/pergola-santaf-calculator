"""LED lighting reports: read the "תאורת לד לפרגולה" PDF reports into a table of LED order lines.

The reports are printed tables (right to left): order, coordination date, code (6 = full
pergola, 67 = repair / service / add-on), zone, quantity, length (mm), item status, three
tick boxes (ordered / received / supplied) and a note. This module only reads them; the
rules (splitting, transformers, regions) live in led.py. It is independent of the Santaf
modules and does not change them.

Three details of these PDFs:
  * the Hebrew text is real Unicode but stored in visual order (reversed), so it is fixed here;
  * the tick boxes are drawn shapes, not text: a tick is a mark drawn inside a small square;
  * a row can be split over several tables, so rows are matched to notes and boxes by position.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from pdf_ingest import PdfUnreadable, parse_date, parse_id, parse_number

CODE_FULL = 6
CODE_SERVICE = 67

# Item status as printed (logical Hebrew, after fixing the visual order) -> stable key.
STATUS_KEYS = {
    "הועבר למפעל": "transferred",
    "בייצור": "in_production",
    "מיוצר": "produced",
    "במפעל": "at_factory",
    "מידות לא סופיות": "sizes_not_final",
    "במחסן": "in_warehouse",
    "בוטל": "cancelled",
}

LINE_COLUMNS = [
    "order_id", "order_date", "code", "zone", "length_mm", "quantity", "status", "status_key",
    "ordered", "received", "supplied", "note", "main_order", "source_file", "page",
]

_HEBREW = re.compile("[֐-׿]")
_RUN = re.compile(r"[0-9A-Za-z.,:/\-]+")      # digits / latin runs must stay left-to-right


def fix_visual_hebrew(text) -> str:
    """Logical text from a string stored in visual order (right-to-left shown left-to-right).

    The whole line is reversed (letters and word order), then runs of digits / latin letters are
    turned back so numbers read correctly. Multi-line cells are fixed line by line.
    """
    if text is None:
        return ""
    lines = []
    for line in str(text).splitlines():
        if _HEBREW.search(line):
            line = _RUN.sub(lambda m: m.group(0)[::-1], line[::-1])
        lines.append(" ".join(line.split()))
    return " ".join(part for part in lines if part)


def status_key(status: str) -> str:
    """Stable key for a printed status; 'other' for anything not seen before."""
    return STATUS_KEYS.get(" ".join(str(status).split()), "other" if status else "")


def main_order_from_note(note: str, own_order: str = "") -> str:
    """The 6-digit order number a note points to ("... הזמנה ראשית 839413"), if any."""
    for number in re.findall(r"(?<!\d)\d{6}(?!\d)", note or ""):
        if number != own_order:
            return number
    return ""


# --------------------------------------------------------------------------
# One PDF
# --------------------------------------------------------------------------
@dataclass
class LedLoadReport:
    loaded: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    confirmed: list[str] = field(default_factory=list)      # printed totals match what was read
    unconfirmed: list[str] = field(default_factory=list)
    duplicates: int = 0                                      # lines shared by overlapping files
    frames: dict = field(default_factory=dict, repr=False)   # file name -> its own lines (for the archive)


def _data_row(cells: list[str]):
    """Map a table row (visual order: status, length, qty, zone, code, date, order) to a dict."""
    cells = [("" if c is None else str(c).strip()) for c in cells]
    date_i = next((i for i, c in enumerate(cells) if parse_date(c)), None)
    if date_i is None or date_i + 1 >= len(cells) or date_i < 4:
        return None
    order_id = parse_id(cells[date_i + 1])
    code, zone = parse_number(cells[date_i - 1]), cells[date_i - 2]
    qty, length = parse_number(cells[date_i - 3]), parse_number(cells[date_i - 4])
    if not order_id or code is None or qty is None or length is None:
        return None
    return {
        "order_id": order_id, "order_date": parse_date(cells[date_i]), "code": int(code),
        "zone": re.sub(r"\D", "", zone) or zone, "length_mm": int(round(length)),
        "quantity": int(round(qty)), "status": fix_visual_hebrew("".join(cells[:date_i - 4])),
    }


def _overlap(a_top, a_bottom, b_top, b_bottom) -> float:
    return max(0.0, min(a_bottom, b_bottom) - max(a_top, b_top))


def _checkbox_columns(page):
    """Tick boxes of one page as {(column, row_top): ticked}, columns 0=ordered 1=received 2=supplied.

    Boxes are small squares; a tick is a curve/line drawn inside one. Each box is drawn twice (fill
    and stroke), so duplicates are merged. The three box columns are the three most populated x
    positions, ordered right to left as in the report (הוזמן, נתקבל, סופק).
    """
    squares = {}
    for r in page.rects:
        w, h = r["x1"] - r["x0"], r["bottom"] - r["top"]
        if 5 < w < 14 and 5 < h < 14 and abs(w - h) < 2:
            squares[(round(r["x0"] / 3), round(r["top"] / 3))] = r
    if not squares:
        return [], {}
    marks = list(page.curves) + list(page.lines)

    def ticked(b):
        return any(m["x0"] >= b["x0"] - 1 and m["x1"] <= b["x1"] + 1 and
                   m["top"] >= b["top"] - 1 and m["bottom"] <= b["bottom"] + 1 for m in marks)

    columns: list[list[dict]] = []
    for b in sorted(squares.values(), key=lambda r: r["x0"]):
        if columns and abs(columns[-1][0]["x0"] - b["x0"]) < 6:
            columns[-1].append(b)
        else:
            columns.append([b])
    columns = sorted(sorted(columns, key=len, reverse=True)[:3], key=lambda c: -c[0]["x0"])
    return columns, {id(b): ticked(b) for col in columns for b in col}


def _row_notes(page, data_rows: list[dict]) -> None:
    """Fill `note` / `main_order` of each row from the words printed left of the table in its band.

    Done by position (not by table detection), because a lone note cell is not always seen as a
    table. Words are joined per printed line, each line is fixed from visual order, lines top-down.
    """
    if not data_rows:
        return
    table_left = min(row["_x0"] for row in data_rows)
    centers = [(row["_top"] + row["_bottom"]) / 2 for row in data_rows]
    words = [w for w in page.extract_words() if w["x1"] <= table_left - 5]
    owner: dict[int, list[dict]] = {i: [] for i in range(len(data_rows))}
    for w in words:                      # a note cell can be taller than its row: take the nearest row
        middle = (w["top"] + w["bottom"]) / 2
        nearest = min(range(len(centers)), key=lambda i: abs(centers[i] - middle))
        if abs(centers[nearest] - middle) <= 14:
            owner[nearest].append(w)
    for index, row in enumerate(data_rows):
        band = owner[index]
        lines: list[list[dict]] = []
        for w in sorted(band, key=lambda w: (w["top"], w["x0"])):
            if lines and abs(lines[-1][0]["top"] - w["top"]) < 3:
                lines[-1].append(w)
            else:
                lines.append([w])
        row["note"] = " ".join(fix_visual_hebrew(" ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])))
                               for line in lines).strip()
        row["main_order"] = main_order_from_note(row["note"], row["order_id"])


def _page_lines(page) -> list[dict]:
    data_rows = []
    for table in page.find_tables():
        for row, cells in zip(table.rows, table.extract()):
            if len(cells) >= 7:
                parsed = _data_row(cells)
                if parsed:
                    parsed["_x0"], parsed["_top"], parsed["_bottom"] = row.bbox[0], row.bbox[1], row.bbox[3]
                    data_rows.append(parsed)
    _row_notes(page, data_rows)

    columns, ticks = _checkbox_columns(page)
    for index, name in enumerate(("ordered", "received", "supplied")):
        for row in data_rows:
            row[name] = False
            if index < len(columns):
                box = next((b for b in columns[index]
                            if _overlap(row["_top"], row["_bottom"], b["top"], b["bottom"]) > 3), None)
                row[name] = bool(box and ticks.get(id(box)))
    return data_rows


def _footer_matches(df: pd.DataFrame, texts: list[str]) -> bool:
    """The report prints its totals (lines and quantity); true when they equal what was read."""
    tokens = [t for text in texts for t in re.findall(r"\d+(?:\.\d+)?", text)]
    total = float(df["quantity"].sum())
    return str(len(df)) in tokens and any("." in t and abs(float(t) - total) <= 0.01 for t in tokens)


def load_led_pdf(source, name: str = "") -> pd.DataFrame:
    """Read every LED line from one report (path or file-like object)."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfUnreadable("Install the PDF reader: pip install pdfplumber") from exc
    rows, texts, has_text = [], [], False
    try:
        with pdfplumber.open(source) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                has_text = has_text or bool(page.chars)
                texts.append(" ".join(w["text"] for w in page.extract_words()))
                for row in _page_lines(page):
                    row["page"] = number
                    rows.append(row)
    except PdfUnreadable:
        raise
    except Exception as exc:                                # damaged, password-protected, not a PDF
        raise PdfUnreadable("This file could not be read as a PDF "
                            f"({type(exc).__name__}).") from exc
    if not has_text:
        raise PdfUnreadable("This PDF has no text layer (it is a scan).")
    if not rows:
        raise PdfUnreadable("No LED order table (dates and order numbers) was found in this PDF.")
    df = pd.DataFrame(rows).drop(columns=["_x0", "_top", "_bottom"])
    df["source_file"] = name or str(getattr(source, "name", source))
    df["status_key"] = df["status"].map(status_key)
    df["order_date"] = pd.to_datetime(df["order_date"])
    df = df[LINE_COLUMNS].reset_index(drop=True)
    df.attrs["footer_matches"] = _footer_matches(df, texts)
    return df


# --------------------------------------------------------------------------
# Several PDFs
# --------------------------------------------------------------------------
_KEY = ["order_id", "order_date", "code", "zone", "length_mm", "quantity"]


def merge_led_reports(frames: list[pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    """Union of the files' lines without double counting overlapping reports.

    A line found in several files counts once (the version from the latest file is kept, since its
    status and ticks are the most recent); a genuine repeat inside one file keeps its repeats.
    """
    numbered = [f.assign(_n=f.groupby(_KEY).cumcount()) for f in frames]
    combined = pd.concat(numbered, ignore_index=True)
    merged = (combined.drop_duplicates(subset=[*_KEY, "_n"], keep="last").drop(columns="_n")
              .sort_values(["order_date", "order_id"], kind="stable").reset_index(drop=True))
    return merged, len(combined) - len(merged)


def load_led_pdfs(files: list[tuple[str, object]]) -> tuple[pd.DataFrame, LedLoadReport]:
    """Load (name, file) pairs; a bad file is reported by name and skipped, not fatal."""
    report, frames = LedLoadReport(), []
    for name, source in files:
        try:
            frame = load_led_pdf(source, name)
        except PdfUnreadable as exc:
            report.errors[name] = str(exc)
            continue
        frames.append(frame)
        report.loaded.append(name)
        report.frames[name] = frame
        (report.confirmed if frame.attrs.get("footer_matches") else report.unconfirmed).append(name)
    if not frames:
        raise PdfUnreadable("; ".join(f"{n}: {m}" for n, m in report.errors.items())
                            or "No PDF files were provided.")
    df, report.duplicates = merge_led_reports(frames)
    return df, report
