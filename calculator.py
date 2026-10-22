"""Business logic for the Santaf (polycarbonate) pergola panel calculator.

All rounding, splitting, pricing and summary rules live here so the UI
(app.py) stays a thin layer.
"""
from __future__ import annotations

import io
import math
import re
from typing import Mapping

import pandas as pd

LENGTH_STEP = 500          # mm, all lengths are rounded up to this step
MIN_SUMMARY_LENGTH = 1000  # mm, first row of the matrix summary
SPLIT_THRESHOLD = 8000     # mm, lengths >= this are split into two panels
MAX_PANEL_LENGTH = 8000    # mm, no panel may exceed this
MIN_OVERLAP = 1000         # mm, minimum overlap between the two split panels
SPLIT_ROUNDING = 1000      # mm, the longer split part is rounded up to this

REQUIRED_COLUMNS = [
    "order_id",
    "confirmation_date",
    "code",
    "zone",
    "original_quantity",
    "original_length",
]
# Columns a file must have; code and zone are filled with "" when absent.
MANDATORY_COLUMNS = ["order_id", "confirmation_date", "original_quantity", "original_length"]

_ALIASES_BY_COLUMN = {
    "order_id": ["order id", "order", "order no", "order number", "orderid",
                 "מספר הזמנה", "מס הזמנה", "מספר הזמנת", "הזמנה", "מס' הזמנה", "מזהה הזמנה"],
    "confirmation_date": ["confirmation date", "date", "order date", "confirmed",
                          "תאריך", "תאריך אישור", "תאריך אישור הזמנה", "תאריך הזמנה",
                          "תאריך אישור ההזמנה"],
    "code": ["code", "item code", "sku", "קוד", "קוד פריט", "מקט", "מק\"ט", "קוד מוצר"],
    "zone": ["zone", "area", "region", "אזור", "איזור", "אזור אספקה"],
    "original_quantity": ["original quantity", "quantity", "qty", "amount",
                          "כמות", "כמות מקורית", "כמות מקור", "כמות להזמנה"],
    "original_length": ["original length", "length", "מקורי אורך",
                        "אורך", "אורך מקורי", "אורך מקור", "אורך פרגולה", "אורך מ\"מ"],
}


def _norm(value) -> str:
    """Case-insensitive, punctuation/spacing-insensitive form of a header."""
    text = str(value).replace("_", " ").replace("-", " ").lower()
    text = re.sub(r"[\"'`׳״.:,()\[\]*]", "", text)   # quotes, geresh, gershayim, etc.
    return re.sub(r"\s+", " ", text).strip()


COLUMN_ALIASES = {_norm(a): col for col, names in _ALIASES_BY_COLUMN.items()
                  for a in [col, *names]}


# --------------------------------------------------------------------------
# Rounding and splitting
# --------------------------------------------------------------------------
def round_quantity(quantity: float) -> int:
    """Always round up to the nearest integer."""
    return math.ceil(quantity)


def round_length(length: float) -> int:
    """Always round up to the nearest 500 mm."""
    return math.ceil(length / LENGTH_STEP) * LENGTH_STEP


def split_length(length: int) -> list[int]:
    """Return the Santaf panel lengths needed for a (rounded) pergola length.

    Lengths below 8000 mm are a single panel. Lengths >= 8000 mm are covered by
    two panels, each <= 8000 mm, overlapping by at least 1000 mm, so the panels
    sum to length + 1000. The longer panel is half of that rounded up to
    1000 mm, which reproduces 8000 -> [5000, 4000] and 10000 -> [6000, 5000].
    """
    if length < SPLIT_THRESHOLD:
        return [length]
    total = length + MIN_OVERLAP
    longer = math.ceil(total / 2 / SPLIT_ROUNDING) * SPLIT_ROUNDING
    shorter = total - longer
    if longer > MAX_PANEL_LENGTH:
        raise ValueError(
            f"Length {length} mm cannot be covered by two panels of at most "
            f"{MAX_PANEL_LENGTH} mm with {MIN_OVERLAP} mm overlap."
        )
    return [longer, shorter]


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------
def _header_key(value) -> str:
    return _norm(value)


def _find_header_row(raw: pd.DataFrame) -> int:
    """Index of the first row (within the first 30) that holds the required headers."""
    for i in range(min(len(raw), 30)):
        found = {COLUMN_ALIASES.get(_header_key(v)) for v in raw.iloc[i]}
        if set(MANDATORY_COLUMNS) <= found:
            return i
    seen = [str(v) for v in raw.iloc[:10].stack().astype(str)][:15]
    raise ValueError(
        "Could not find a header row with: Order ID, Confirmation Date, Original "
        "Quantity, Original Length (Hebrew names such as מספר הזמנה / תאריך / כמות / "
        f"אורך are also accepted). Text found near the top of the file: {seen}"
    )


_XLSX_MAGIC = bytes.fromhex("504b0304")
_XLS_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def _read_raw(data: bytes, name: str) -> pd.DataFrame:
    """Read bytes as a header-less table, detecting the real format from content."""
    if data[:4] == _XLSX_MAGIC:                        # .xlsx (zip)
        return pd.read_excel(io.BytesIO(data), header=None, engine="openpyxl")
    if data[:8] == _XLS_MAGIC:                         # legacy .xls (OLE2)
        return pd.read_excel(io.BytesIO(data), header=None, engine="xlrd")
    head = data[:2048].lstrip().lower()
    if head.startswith((b"<html", b"<table", b"<!doctype")):  # "xls" that is really HTML
        return pd.read_html(io.BytesIO(data), header=None)[0]
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        raise ValueError(f"'{name}' is not a readable Excel file.")
    for enc in ("utf-8-sig", "cp1255", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.StringIO(text), header=None, sep=None, engine="python", dtype=object)


ORDER_FILE_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".csv")


def list_order_files(directory) -> list[str]:
    """Names of CSV/Excel files in a local directory, newest first."""
    import os
    entries = [e for e in os.scandir(directory)
               if e.is_file() and e.name.lower().endswith(ORDER_FILE_EXTENSIONS)
               and not e.name.startswith("~$")]
    return [e.name for e in sorted(entries, key=lambda e: e.stat().st_mtime, reverse=True)]


def load_orders(source) -> pd.DataFrame:
    """Read a CSV/.xls/.xlsx file (path or uploaded file object) into a DataFrame.

    The format is detected from the file content, not just the extension, and the
    header row is located automatically (production files often have title rows).
    """
    name = str(getattr(source, "name", source)).lower()
    if hasattr(source, "getvalue"):
        data = source.getvalue()
    elif hasattr(source, "read"):
        source.seek(0)
        data = source.read()
    else:
        with open(source, "rb") as fh:
            data = fh.read()
    raw = _read_raw(data, name)
    header = _find_header_row(raw)
    df = raw.iloc[header + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[header]]
    df = df.dropna(how="all").dropna(subset=[c for c in df.columns
                                             if COLUMN_ALIASES.get(_header_key(c)) == "order_id"])
    return df.reset_index(drop=True)


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename headers to canonical names and validate mandatory columns."""
    renamed = df.rename(columns={c: COLUMN_ALIASES.get(_norm(c), c) for c in df.columns})
    missing = [c for c in MANDATORY_COLUMNS if c not in renamed.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    for col in REQUIRED_COLUMNS:
        if col not in renamed.columns:
            renamed[col] = ""
    return renamed[REQUIRED_COLUMNS].copy()


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------
def week_of_month(date: pd.Timestamp) -> int:
    """Calendar week of the month: days 1-7 -> 1, 8-14 -> 2, ... 29+ -> 5."""
    return (date.day - 1) // 7 + 1


def parse_dates(series: pd.Series) -> pd.Series:
    """Parse ISO dates strictly; otherwise fall back to day-first (e.g. 31/03/2025)."""
    try:
        return pd.to_datetime(series, format="ISO8601")
    except (ValueError, TypeError):
        return pd.to_datetime(series, dayfirst=True)


def process_orders(df: pd.DataFrame) -> pd.DataFrame:
    """Apply rounding and split rules.

    Returns one row per Santaf panel part with columns: the original fields,
    rounded_quantity, rounded_length, part (1-based), panel_length, quantity,
    year, month, quarter, week.
    """
    df = normalize_columns(df)
    df["confirmation_date"] = parse_dates(df["confirmation_date"])
    for col in ("original_quantity", "original_length"):
        df[col] = pd.to_numeric(df[col], errors="raise")

    rows = []
    for source_row, rec in enumerate(df.to_dict("records")):
        qty = round_quantity(rec["original_quantity"])
        length = round_length(rec["original_length"])
        for part, panel in enumerate(split_length(length), start=1):
            rows.append(
                {
                    **rec,
                    "rounded_quantity": qty,
                    "rounded_length": length,
                    "part": part,
                    "panel_length": panel,
                    "quantity": qty,
                    "source_row": source_row,
                }
            )
    out = pd.DataFrame(rows, columns=[*df.columns, "rounded_quantity", "rounded_length",
                                      "part", "panel_length", "quantity", "source_row"])
    d = out["confirmation_date"]
    out["year"] = d.dt.year
    out["month"] = d.dt.month
    out["quarter"] = d.dt.quarter
    out["week"] = d.map(week_of_month)
    return out


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
def default_price_table(panels: pd.DataFrame, price_per_meter: float = 0.0) -> pd.DataFrame:
    """One editable price row per distinct panel length."""
    lengths = sorted(panels["panel_length"].unique())
    return pd.DataFrame(
        {"panel_length": lengths, "unit_price": [price_per_meter * l / 1000 for l in lengths]}
    )


def apply_prices(panels: pd.DataFrame, prices: Mapping[int, float]) -> pd.DataFrame:
    """Add unit_price and total_cost (unit price x quantity) to each panel row."""
    out = panels.copy()
    out["unit_price"] = out["panel_length"].map(prices).fillna(0.0)
    out["total_cost"] = out["unit_price"] * out["quantity"]
    return out


def cost_by_order(panels: pd.DataFrame) -> pd.DataFrame:
    return (
        panels.groupby("order_id", as_index=False)
        .agg(panels=("quantity", "sum"), total_cost=("total_cost", "sum"))
    )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def weekly_matrix(panels: pd.DataFrame, length_col: str = "panel_length") -> pd.DataFrame:
    """Rows: lengths from 1000 mm to the maximum in 500 mm steps.

    Columns: calendar weeks of the month plus a total column. For data inside a
    single month the columns are "Week 1".."Week 5" and "Month total". For a
    range spanning several months there is one column per month and week
    ("2026-10 W5", "2026-11 W1", ...) and the total is "Range total".
    """
    max_len = int(panels[length_col].max()) if len(panels) else MIN_SUMMARY_LENGTH
    index = pd.Index(range(MIN_SUMMARY_LENGTH, max_len + LENGTH_STEP, LENGTH_STEP),
                     name="length_mm")
    months = panels[["year", "month"]].drop_duplicates()
    single_month = len(months) <= 1
    if single_month:
        keys = [(0, 0, w) for w in range(1, 6)]
        labels = [f"Week {w}" for w in range(1, 6)]
        period = panels.assign(year=0, month=0)
    else:
        present = (panels[["year", "month", "week"]].drop_duplicates()
                   .sort_values(["year", "month", "week"]))
        keys = list(present.itertuples(index=False, name=None))
        labels = [f"{y}-{m:02d} W{w}" for y, m, w in keys]
        period = panels
    grouped = period.groupby([length_col, "year", "month", "week"])["quantity"].sum()
    matrix = pd.DataFrame(
        {label: [int(grouped.get((length, *key), 0)) for length in index]
         for label, key in zip(labels, keys)},
        index=index,
    )
    matrix["Month total" if single_month else "Range total"] = matrix.sum(axis=1)
    return matrix


def filter_date_range(panels: pd.DataFrame, start, end) -> pd.DataFrame:
    """Keep rows whose confirmation date lies in [start, end], both inclusive."""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if start > end:
        raise ValueError("Start date must not be after end date.")
    dates = panels["confirmation_date"].dt.normalize()
    return panels[(dates >= start) & (dates <= end)]


def filter_month(panels: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    return panels[(panels["year"] == year) & (panels["month"] == month)]


def period_summary(panels: pd.DataFrame, period: str = "quarter") -> pd.DataFrame:
    """Quantity and cost per length for 'month', 'quarter' or 'year'.

    Foundation for supplier-negotiation analytics: consumption per panel
    length per period. Includes total_cost when prices have been applied.
    """
    keys = {"month": ["year", "month"], "quarter": ["year", "quarter"], "year": ["year"]}[period]
    aggs = {"quantity": ("quantity", "sum")}
    if "total_cost" in panels:
        aggs["total_cost"] = ("total_cost", "sum")
    return (
        panels.groupby([*keys, "panel_length"], as_index=False)
        .agg(**aggs)
        .sort_values([*keys, "panel_length"])
        .reset_index(drop=True)
    )


def consumption_by_length(panels: pd.DataFrame) -> pd.DataFrame:
    """Total panels per panel length (after rounding and splitting), shortest first.

    Panels of length 0 (rows with no length in the report) are not real panels and are left out.
    """
    real = panels[panels["panel_length"] > 0]
    return (real.groupby("panel_length", as_index=False)["quantity"].sum()
            .sort_values("panel_length").reset_index(drop=True))


def find_order(panels: pd.DataFrame, text: str) -> pd.DataFrame:
    """All processed rows whose order id contains `text` (empty text matches nothing)."""
    text = str(text).strip()
    if not text:
        return panels.iloc[0:0]
    ids = panels["order_id"].astype(str).str.strip()
    return panels[ids.str.contains(text, regex=False)]


def split_orders(panels: pd.DataFrame) -> pd.DataFrame:
    """One row per original order line that had to be split into two panels.

    Shows the original and rounded length, the two panel lengths and the overlap between them,
    so the cutting/ordering decision can be looked up later.
    """
    cols = ["order_id", "confirmation_date", "original_length", "rounded_length", "quantity",
            "part_1_mm", "part_2_mm", "overlap_mm", "split"]
    split = panels[panels["rounded_length"] >= SPLIT_THRESHOLD]
    rows = []
    for _, g in split.groupby("source_row", sort=False):
        g = g.sort_values("part")
        a, b = int(g["panel_length"].iloc[0]), int(g["panel_length"].iloc[1])
        first = g.iloc[0]
        overlap = a + b - int(first["rounded_length"])
        rows.append({
            "order_id": first["order_id"], "confirmation_date": first["confirmation_date"],
            "original_length": first["original_length"], "rounded_length": int(first["rounded_length"]),
            "quantity": int(first["quantity"]), "part_1_mm": a, "part_2_mm": b,
            "overlap_mm": overlap,
            "split": f"{a} + {b} mm (overlap {overlap} mm)",
        })
    return pd.DataFrame(rows, columns=cols).sort_values(["confirmation_date", "order_id"],
                                                        kind="stable").reset_index(drop=True)
