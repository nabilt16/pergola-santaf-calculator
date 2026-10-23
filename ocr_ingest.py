"""Image / scan ingestion: OCR a photo of an order schedule into an orders table.

Pipeline: deskew + upscale -> erase table rules -> Tesseract word boxes -> rebuild
rows around the date column (rows from date/order-id cells, fields by x-position)
-> DataFrame with the canonical column names plus a `check` column that says which
cells deserve a second look. It feeds calculator.process_orders() exactly like an
Excel/CSV import. OCR is never perfect, so the UI shows the rows for review.
"""
from __future__ import annotations

import io
import os
import re
import shutil
from statistics import median

import numpy as np
import pandas as pd

COLUMNS = ["order_id", "confirmation_date", "code", "zone", "original_quantity",
           "original_length"]

# Visual left-to-right column order of the page.
RTL_ORDER = ["original_length", "original_quantity", "zone", "code", "confirmation_date", "order_id"]
LTR_ORDER = ["order_id", "confirmation_date", "code", "zone", "original_quantity", "original_length"]

# Offered in the UI. `None` = decide per image: Hebrew text or an order id to the
# right of the date column means a right-to-left table.
COLUMN_ORDERS = {
    "Auto (Hebrew page = right to left)": None,
    "Right to left (Hebrew): Length, Quantity, Zone, Code, Date, Order ID": RTL_ORDER,
    "Left to right: Order ID, Date, Code, Zone, Quantity, Length": LTR_ORDER,
}

_HEBREW_RE = re.compile("[\u0590-\u05ff]")
_PROJECT_TESSDATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")
_WINDOWS_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"})
_NUMERIC_LIKE = re.compile(r"^[0-9OolI|SB.,]+$")
_DATE_STRICT = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$|^(\d{4})-(\d{1,2})-(\d{1,2})$")
# Forgiving: OCR often turns "/" into | \ l I , or drops a character from the separators.
_DATE_LOOSE = re.compile(r"(\d{1,2})\s*[/\\.\-,|lI!]\s*(\d{1,2})\s*[/\\.\-,|lI!]\s*(\d{4}|\d{2})(?!\d)")
MAX_PLAUSIBLE_LENGTH = 15000      # two 8000 mm panels with 1000 mm overlap is the longest pergola
_ID_RE = re.compile(r"\d{5,8}")


class OcrUnavailable(RuntimeError):
    """Tesseract (or pytesseract / Pillow) is not installed."""


# --------------------------------------------------------------------------
# Token parsing (pure functions, no OCR needed)
# --------------------------------------------------------------------------
def _valid_date(y: int, mo: int, d: int) -> str | None:
    if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def parse_date(token: str) -> str | None:
    """ISO date from dd/mm/yy(yy), dd.mm.yyyy, yyyy-mm-dd; tolerant of OCR damage."""
    text = token.strip().strip("()[]{}:;,'\"")
    m = _DATE_STRICT.match(text)
    if m:
        if m.group(4):
            return _valid_date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        y = int(m.group(3))
        return _valid_date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    m = _DATE_LOOSE.search(text.replace("O", "0").replace("o", "0"))
    if m:
        y = int(m.group(3))
        return _valid_date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    return None


def parse_number(token: str) -> float | None:
    """Parse a numeric token, repairing common OCR confusions (O->0, l->1, ...)."""
    token = token.strip().strip(":;'\"()[]{}")
    if not _NUMERIC_LIKE.match(token) or not any(c.isdigit() or c in "OolI|SB" for c in token):
        return None
    text = token.translate(_DIGIT_FIX).replace(",", ".")
    if text.count(".") > 1 or text in ("", "."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_id(token: str) -> str | None:
    """An order id: a run of 5+ digits (after OCR digit repair), else None."""
    text = str(token).strip().strip(":;'\"()[]{}|").translate(_DIGIT_FIX)
    return text if _ID_RE.fullmatch(text) and not parse_date(str(token)) else None


def _clean_text(token: str) -> str:
    """Keep letters/digits (incl. Hebrew); a token of only grid/checkbox noise becomes ''."""
    return re.sub(r"[^0-9A-Za-z\u0590-\u05ff.\-/]", "", token)


# --------------------------------------------------------------------------
# Table reconstruction from word boxes (pure, testable without Tesseract)
# --------------------------------------------------------------------------
def _cluster_columns(values: list[float], k: int, gap: float, min_tokens: int,
                     origin: float) -> list[float]:
    """Centers of the k columns nearest `origin` (the date column), from cell x positions.

    Tokens are grouped where the x gap exceeds `gap`; groups with fewer than `min_tokens`
    tokens are noise (status words, checkbox glyphs). Columns are contiguous towards the
    date, so stray groups beyond the last column are dropped by taking the k nearest.
    """
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    centers = [median(g) for g in groups if len(g) >= min_tokens]
    return sorted(centers, key=lambda c: abs(c - origin))[:k]


def _fit_rows(ys: list[float], y0: float, pitch: float) -> tuple[float, float]:
    """Least-squares (first row y, pitch) for y positions that sit on a row grid."""
    for _ in range(3):
        ks = [round((y - y0) / pitch) for y in ys]
        if len(set(ks)) < 2:
            break
        mk, my = sum(ks) / len(ks), sum(ys) / len(ys)
        slope = sum((k - mk) * (y - my) for k, y in zip(ks, ys)) / sum((k - mk) ** 2 for k in ks)
        pitch, y0 = slope, my - slope * mk
    return y0, pitch


def _row_positions(ys: list[float], extra: list[float], pitch: float) -> list[float]:
    """Row y positions from observed cell positions of one reliable column.

    Missing rows between consecutive observations are interpolated by counting how many
    pitches fit in the gap, so rows that are not evenly spaced (perspective) still line up.
    `extra` are positions from another column, already shifted onto this column; they only
    extend the ends of the table.
    """
    ys = sorted(ys)
    rows = [ys[0]]
    for y in ys[1:]:
        gap = y - rows[-1]
        n = max(1, round(gap / pitch))
        rows.extend(rows[-1] + gap * i / n for i in range(1, n + 1))
    for y in sorted(extra):
        if y < rows[0] - 0.5 * pitch:
            while y < rows[0] - 0.5 * pitch:
                rows.insert(0, rows[0] - pitch)
        elif y > rows[-1] + 0.5 * pitch:
            while y > rows[-1] + 0.5 * pitch:
                rows.append(rows[-1] + pitch)
    return rows


def rows_from_words(words: list[tuple], order: list[str], cell_reader=None,
                    reach: float = 1.0) -> pd.DataFrame:
    """Rebuild order rows from OCR word boxes using the date column as anchor.

    `words` are (x_center, y_center, height, text, conf). Every column gets its own row
    model (first-row y, row pitch), chained outwards from the date column, because photo
    perspective makes rows tilt and converge. A word is placed by its nearest column and
    that column's model, so a missing or merged cell damages one field, not the whole row.

    `reach` widens the window in which numeric columns are searched for beside the date
    column (1.0 suits noisy OCR; exact PDF text, whose columns are spaced unevenly, uses more).
    `cell_reader(x, y, width, height, field) -> (text, conf)` optionally re-reads a
    single cell that came out missing or low-confidence. Adds a `check` column
    describing cells worth verifying by eye.
    """
    out_cols = [*COLUMNS, "check"]
    pos = order.index("confirmation_date")
    left_fields, right_fields = order[:pos][::-1], order[pos + 1:]   # nearest the date first

    dates = [w for w in words if parse_date(w[3])]
    if len(dates) < 2:
        return pd.DataFrame(columns=out_cols)
    date_x = median(w[0] for w in dates)
    height = median(w[2] for w in dates)

    id_sign = -1 if "order_id" in left_fields else 1
    id_words = [w for w in words if (w[0] - date_x) * id_sign > 0 and parse_id(w[3])]
    spacing = abs(median(w[0] for w in id_words) - date_x) if id_words else 6 * height

    # --- date column model; header / totals dates far from the block are ignored
    ys = sorted(w[1] for w in dates)
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.3 * height]
    pitch = median(gaps) if gaps else 4 * height
    near = [g for g in gaps if 0.7 * pitch <= g <= 1.4 * pitch]
    pitch = median(near) if near else pitch
    while len(ys) > 3 and ys[1] - ys[0] > 1.6 * pitch:
        ys.pop(0)
    while len(ys) > 3 and ys[-1] - ys[-2] > 1.6 * pitch:
        ys.pop()
    date_a, date_p = _fit_rows(ys, ys[0], pitch)
    n_rows = round((ys[-1] - date_a) / date_p) + 1            # refined below

    # --- column x positions on each side of the date (order id: from its own words)
    centers: dict[str, float] = {"confirmation_date": date_x}
    if id_words:
        centers["order_id"] = median(w[0] for w in id_words)
    top, bottom = ys[0] - 2 * pitch, ys[-1] + 3 * pitch
    for fields, sign in ((left_fields, -1), (right_fields, 1)):
        fields = [f for f in fields if f != "order_id"]
        xs = [w[0] for w in words
              if top <= w[1] <= bottom and _clean_text(str(w[3]))
              and 0.4 * spacing < (w[0] - date_x) * sign < (len(fields) + 0.05) * spacing * reach
              and parse_number(str(w[3])) is not None]
        if fields and xs:
            found = _cluster_columns(xs, len(fields), 0.1 * spacing, max(2, int(0.1 * n_rows)), date_x)
            centers.update(zip(fields, found))

    # --- row positions per column, propagated outwards from the date column. Photo perspective
    # makes rows tilt and fan out, so one global grid cannot place far columns reliably. Each
    # column's cells are matched to the nearest cell of its neighbour (never more than a
    # fraction of a row away), and gaps are filled by interpolating the offsets.
    id_shift = 0.0
    if id_words:                       # typical y offset of an order id relative to its date cell
        pairs = [min((w[1] - d[1] for d in dates), key=abs) for w in id_words]
        id_shift = median(p for p in pairs if abs(p) <= 0.5 * date_p) if any(
            abs(p) <= 0.5 * date_p for p in pairs) else 0.0
    merged: list[float] = []              # date and id cells of one row merge into one anchor
    for y in sorted(ys + [w[1] - id_shift for w in id_words
                          if ys[0] - 0.5 * date_p <= w[1] - id_shift <= ys[-1] + 0.5 * date_p]):
        if merged and y - merged[-1] < 0.45 * date_p:
            merged[-1] = (merged[-1] + y) / 2
        else:
            merged.append(y)
    grid_list = _row_positions(merged, [w[1] - id_shift for w in id_words], date_p)
    n_rows = len(grid_list)
    grid = np.array(grid_list)

    def column_tokens(field):
        ok = (lambda w: parse_date(w[3])) if field == "confirmation_date" else              (lambda w: parse_id(w[3])) if field == "order_id" else              (lambda w: _clean_text(str(w[3])))
        return [w for w in words
                if min(centers, key=lambda f: abs(centers[f] - w[0])) == field
                and abs(centers[field] - w[0]) <= 0.6 * spacing and ok(w)]

    def place(parent_ref, field, numeric_only=True):
        """Match a column's words to the parent's rows; return (cells, this column's row ys)."""
        found: dict[int, tuple] = {}
        for w in column_tokens(field):
            k = int(np.argmin(np.abs(parent_ref - w[1])))
            if abs(parent_ref[k] - w[1]) <= 0.5 * date_p:
                if k not in found or w[4] > found[k][4]:
                    found[k] = w
        ks = sorted(found)
        if len(ks) >= 2:
            offs = np.interp(np.arange(n_rows), ks, [found[k][1] - parent_ref[k] for k in ks])
        elif ks:
            offs = np.full(n_rows, found[ks[0]][1] - parent_ref[ks[0]])
        else:
            offs = np.zeros(n_rows)
        return found, parent_ref + offs

    cells: dict[tuple[int, str], list[tuple]] = {}
    refs: dict[str, np.ndarray] = {}
    found, refs["confirmation_date"] = place(grid, "confirmation_date")
    cells.update({(k, "confirmation_date"): [w] for k, w in found.items()})
    for side in (left_fields, right_fields):
        parent = refs["confirmation_date"]
        for field in side:
            if field not in centers:
                continue
            found, ref = place(parent, field)
            refs[field] = ref
            cells.update({(k, field): [w] for k, w in found.items()})
            # one-glyph columns (code) have jittery y: don't let them steer the next column
            if median(len(_clean_text(str(w[3]))) for w in found.values()) >= 2 if found else False:
                parent = ref

    def y_of(k, field):
        return float(refs.get(field, grid)[k])

    xs_all = sorted(centers.values())
    col_width = 0.9 * min((b - a for a, b in zip(xs_all, xs_all[1:])), default=spacing)

    def best(k, field, valid):
        found = [w for w in cells.get((k, field), []) if valid(str(w[3]))]
        return max(found, key=lambda w: w[4]) if found else None

    def is_text(t):
        return bool(_clean_text(t)) and not parse_date(t) and not parse_id(t)

    def reread(k, field, current_conf):
        """Second chance for one cell: a single-line read of just that cell."""
        if cell_reader is None or field not in centers:
            return None
        got = cell_reader(centers[field], y_of(k, field), col_width, 0.9 * date_p, field)
        if not got or got[0] is None:
            return None
        return got

    id_lengths = [len(parse_id(w[3])) for ws in (cells.get((k, "order_id"), []) for k in range(n_rows))
                  for w in ws if parse_id(w[3])]
    id_len = max(set(id_lengths), key=id_lengths.count) if id_lengths else None

    rows = []
    for k in range(n_rows):
        iw = best(k, "order_id", lambda t: parse_id(t) is not None)
        dw = best(k, "confirmation_date", lambda t: parse_date(t) is not None)
        qw = best(k, "original_quantity", lambda t: parse_number(t) is not None)
        lw = best(k, "original_length", lambda t: parse_number(t) is not None)
        cw, zw = best(k, "code", is_text), best(k, "zone", is_text)
        order_id = parse_id(iw[3]) if iw else None
        date = parse_date(dw[3]) if dw else None
        qty = parse_number(str(qw[3])) if qw else None
        length = parse_number(str(lw[3])) if lw else None
        code = _clean_text(str(cw[3])) if cw else ""
        zone = _clean_text(str(zw[3])) if zw else ""
        conf = {"order_id": iw[4] if iw else -1, "confirmation_date": dw[4] if dw else -1,
                "original_quantity": qw[4] if qw else -1, "original_length": lw[4] if lw else -1}

        # Cells that are missing or look wrong get a second, single-cell read
        shaky = {
            "order_id": order_id is None or (id_len is not None and len(order_id) != id_len)
            or conf["order_id"] < 70,
            "confirmation_date": date is None or conf["confirmation_date"] < 70,
            "original_quantity": qty is None or (qty == int(qty) and qty >= 30) or qty >= 100
            or conf["original_quantity"] < 70,
            "original_length": length is None or conf["original_length"] < 70,
            "code": not code, "zone": not zone,
        }
        for field, bad in shaky.items():
            if not bad:
                continue
            got = reread(k, field, conf.get(field, -1) if field in conf else -1)
            if got is None:
                continue
            text, new_conf = got
            if field == "order_id" and parse_id(text) and (id_len is None or len(parse_id(text)) == id_len
                                                            or order_id is None):
                order_id, conf[field] = parse_id(text), new_conf
            elif field == "confirmation_date" and parse_date(text):
                date, conf[field] = parse_date(text), new_conf
            elif field == "original_quantity" and parse_number(text) is not None:
                qty, conf[field] = parse_number(text), new_conf
            elif field == "original_length" and parse_number(text) is not None:
                length, conf[field] = parse_number(text), new_conf
            elif field in ("code", "zone") and text.strip("."):
                if field == "code":
                    code = text.strip(".")
                else:
                    zone = text.strip(".")

        notes = []
        if date is None:
            notes.append("date missing")
        if order_id is None:
            notes.append("order id missing")
        elif id_len is not None and len(order_id) != id_len:
            notes.append(f"order id has {len(order_id)} digits, expected {id_len}")
        if qty is None:
            notes.append("quantity missing")
        elif qty >= 100 or (qty == int(qty) and qty >= 30):
            notes.append("quantity may have lost its decimal point")
        if length is None:
            notes.append("length missing")
        elif length > MAX_PLAUSIBLE_LENGTH:
            notes.append(f"length above {MAX_PLAUSIBLE_LENGTH} mm is implausible")
        for label, field in (("order id", "order_id"), ("quantity", "original_quantity"),
                             ("length", "original_length")):
            if 0 <= conf[field] < 50:
                notes.append(f"{label} low confidence")
        rows.append({"order_id": order_id or "", "confirmation_date": date,
                     "code": code, "zone": zone,
                     "original_quantity": qty, "original_length": length,
                     "check": "; ".join(notes)})

    rows = [r for r in rows if r["order_id"] or r["confirmation_date"] or
            r["original_quantity"] is not None or r["original_length"] is not None]
    _reconcile_dates(rows)
    df = pd.DataFrame(rows, columns=out_cols)
    for col in ("original_quantity", "original_length"):
        df[col] = pd.to_numeric(df[col])
    return df


def _reconcile_dates(rows: list[dict]) -> None:
    """One sheet covers one period: snap stray years to the dominant one, flag outliers.

    Reports are listed in date order, so dates that break the sequence are flagged too.
    """
    dated = [r for r in rows if r["confirmation_date"]]
    if len(dated) < 3:
        return
    years = [pd.Timestamp(r["confirmation_date"]).year for r in dated]
    year = max(set(years), key=years.count)
    for r in dated:
        d = pd.Timestamp(r["confirmation_date"])
        if d.year != year:
            d = d.replace(year=year)
            r["check"] = "; ".join(filter(None, [r["check"], f"year corrected to {year}"]))
        r["confirmation_date"] = d.strftime("%Y-%m-%d")

    # longest non-decreasing run = the dates that are in sequence
    ds = [r["confirmation_date"] for r in dated]
    best = [1] * len(ds)
    prev = [-1] * len(ds)
    for i in range(len(ds)):
        for j in range(i):
            if ds[j] <= ds[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    keep, i = set(), max(range(len(ds)), key=best.__getitem__)
    while i != -1:
        keep.add(i)
        i = prev[i]
    for i, r in enumerate(dated):
        if i not in keep:
            r["check"] = "; ".join(filter(None, [r["check"], "date out of sequence"]))


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
def _load_tesseract():
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise OcrUnavailable("Install the OCR packages: pip install pytesseract pillow") from exc
    if not shutil.which("tesseract"):
        if os.path.exists(_WINDOWS_TESSERACT):
            pytesseract.pytesseract.tesseract_cmd = _WINDOWS_TESSERACT
        else:
            raise OcrUnavailable(
                "The Tesseract OCR engine is not installed. On Windows run: "
                "winget install UB-Mannheim.TesseractOCR  (then restart the app)."
            )
    return pytesseract, Image


def tesseract_language_setup() -> tuple[str, str]:
    """Return (lang, tessdata folder). Prefers heb+eng from a folder holding both."""
    candidates = [_PROJECT_TESSDATA, os.environ.get("TESSDATA_PREFIX", ""),
                  os.path.join(os.path.dirname(_WINDOWS_TESSERACT), "tessdata")]
    for folder in candidates:
        if folder and all(os.path.exists(os.path.join(folder, f"{l}.traineddata"))
                          for l in ("heb", "eng")):
            return "heb+eng", folder
    for folder in candidates:
        if folder and os.path.exists(os.path.join(folder, "eng.traineddata")):
            return "eng", folder
    return "eng", ""


def _estimate_skew(gray) -> float:
    """Angle (degrees) that makes the table's horizontal rules / text rows sharpest."""
    import numpy as np
    w, h = gray.size
    region = gray.crop((int(w * 0.1), int(h * 0.05), int(w * 0.95), int(h * 0.7)))
    region.thumbnail((800, 800))
    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-4, 4.01, 0.1):
        rotated = region.rotate(float(angle), fillcolor=255, resample=2)   # 2 = BILINEAR
        profile = (np.asarray(rotated) < 150).sum(axis=1).astype(float)
        score = float(np.var(np.diff(profile)))
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def preprocess(image, target_width: int = 3072):
    """Fix EXIF rotation, grayscale, contrast, deskew, and upscale small photos."""
    from PIL import Image, ImageOps
    image = ImageOps.autocontrast(ImageOps.exif_transpose(image).convert("L"))
    angle = _estimate_skew(image)
    if abs(angle) >= 0.2:
        image = image.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    if image.width < target_width:
        image = image.resize((target_width, int(image.height * target_width / image.width)),
                             Image.LANCZOS)
    return image


def remove_grid_lines(image):
    """Whiten long horizontal/vertical dark runs (table rules) so digits read cleanly."""
    import numpy as np
    from PIL import Image
    arr = np.asarray(image)
    dark = arr < 150

    def run_lengths(mask):                       # length of the dark run through each pixel, per column
        fwd = np.zeros(mask.shape, np.int32)
        bwd = np.zeros(mask.shape, np.int32)
        r = np.zeros(mask.shape[1], np.int32)
        for i in range(mask.shape[0]):
            r = (r + 1) * mask[i]
            fwd[i] = r
        r = np.zeros(mask.shape[1], np.int32)
        for i in range(mask.shape[0] - 1, -1, -1):
            r = (r + 1) * mask[i]
            bwd[i] = r
        return fwd + bwd - 1

    w = image.width
    kill = (run_lengths(dark) > 0.03 * w) | (run_lengths(dark.T).T > 0.052 * w)
    grown = kill.copy()
    grown[1:] |= kill[:-1]
    grown[:-1] |= kill[1:]
    grown[:, 1:] |= kill[:, :-1]
    grown[:, :-1] |= kill[:, 1:]
    out = arr.copy()
    out[grown] = 255
    return Image.fromarray(out)


def _ocr_words(pytesseract, image, lang: str, psm: int) -> list[tuple]:
    d = pytesseract.image_to_data(image, lang=lang, config=f"--psm {psm}",
                                  output_type=pytesseract.Output.DICT)
    return [(d["left"][i] + d["width"][i] / 2, d["top"][i] + d["height"][i] / 2,
             d["height"][i], str(d["text"][i]).strip(), float(d["conf"][i]))
            for i in range(len(d["text"]))
            if str(d["text"][i]).strip() and float(d["conf"][i]) >= 0]


def ocr_table(image_bytes: bytes, order: list[str] | None) -> pd.DataFrame:
    """OCR a photographed order table into rows (see rows_from_words)."""
    pytesseract, Image = _load_tesseract()
    from PIL import ImageOps
    lang, tessdata = tesseract_language_setup()
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    image = preprocess(Image.open(io.BytesIO(image_bytes)))
    W, H = image.size
    empty = pd.DataFrame(columns=[*COLUMNS, "check"])

    # Pass 1 (digits, English model): whole page, to locate the date / order-id columns.
    # Digits read more reliably without the Hebrew model competing for the same glyphs.
    cleaned = remove_grid_lines(image)
    words = _ocr_words(pytesseract, cleaned, "eng", 4)
    dates = [w for w in words if parse_date(w[3])]
    if len(dates) < 2:
        return empty
    date_x = median(w[0] for w in dates)
    ids = [w for w in words if parse_id(w[3])]

    if order is None:
        # Hebrew headers (heb+eng pass over the top of the page) or an order id to the
        # right of the date column both mean a right-to-left table.
        hebrew = sum(len(_HEBREW_RE.findall(w[3]))
                     for w in _ocr_words(pytesseract, image.crop((0, 0, W, int(H * 0.12))), lang, 6))
        id_right = bool(ids) and median(w[0] for w in ids) > date_x
        order = RTL_ORDER if (hebrew >= 5 or id_right) else LTR_ORDER

    # Pass 2: crop to the data columns around the date column and read again.
    pos = order.index("confirmation_date")
    spacing = abs(median(w[0] for w in ids) - date_x) if ids else 0.08 * W
    left = max(0, int(date_x - (pos + 0.5) * spacing * 1.15))
    right = min(W, int(date_x + (len(order) - pos - 0.5) * spacing * 1.15))
    top = max(0, int(min(w[1] for w in dates) - 0.1 * H))
    bottom = min(H, int(max(w[1] for w in dates) + 0.05 * H))
    crop = cleaned.crop((left, top, right, bottom))
    crop_words = _ocr_words(pytesseract, crop, "eng", 4)

    def read_cell(x, y, width, height, field):
        """Read one cell as a single line, at two magnifications; return (text, conf)."""
        box = (max(0, int(x - width / 2)), max(0, int(y - height / 2)),
               min(crop.width, int(x + width / 2)), min(crop.height, int(y + height / 2)))
        allowed = "0123456789/" if field == "confirmation_date" else "0123456789."
        readings = []
        for scale in ((3, 2) if field == "original_quantity" else (3,)):
            cell_img = crop.crop(box)
            cell_img = cell_img.resize((cell_img.width * scale, cell_img.height * scale), Image.LANCZOS)
            cell_img = ImageOps.expand(cell_img, border=16, fill=255)
            d = pytesseract.image_to_data(
                cell_img, lang="eng", output_type=pytesseract.Output.DICT,
                config=f"--psm 7 -c tessedit_char_whitelist={allowed}")
            parts = [(str(t).strip(), float(c)) for t, c in zip(d["text"], d["conf"])
                     if str(t).strip() and float(c) >= 0]
            if parts:
                readings.append(("".join(t for t, _ in parts), min(c for _, c in parts)))
        if not readings:
            return None
        if field == "original_quantity":
            # The decimal point is the first thing lost at this resolution: when two reads agree
            # on the digits, trust the one that kept the point.
            for text, conf in readings:
                digits = text.replace(".", "")
                if "." in text.strip(".") and all(r[0].replace(".", "") == digits for r in readings):
                    return text.strip("."), conf
        return max(readings, key=lambda r: r[1])

    return rows_from_words(crop_words, order, read_cell)


def extract_orders(images: list[bytes], order: list[str] | None) -> pd.DataFrame:
    """OCR several images and return all parsed order rows (with a `check` column)."""
    frames = [f for f in (ocr_table(b, order) for b in images) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=[*COLUMNS, "check"])
    return pd.concat(frames, ignore_index=True)
