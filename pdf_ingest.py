"""Digital PDF ingestion: read order schedule reports straight from the PDF's text layer.

No OCR is involved, so digits and decimal points are exact. Columns are recognised by their
content (a column of dates, a column of order ids, numeric columns beside them) rather than by
header text, because Hebrew headers in printed PDFs are often stored as unmappable glyphs
(`(cid:676)...`) and different pages can have a different number of columns.

The result has the canonical columns, so it feeds calculator.process_orders() like any other
source.
"""
from __future__ import annotations

import re

import pandas as pd

from ocr_ingest import (COLUMNS, LTR_ORDER, RTL_ORDER, _clean_text, parse_date, parse_id,
                        parse_number, rows_from_words)


class PdfUnreadable(RuntimeError):
    """The PDF has no text layer, or no order table could be found in it."""


_CID = re.compile(r"\(cid:\d+\)")
_MIN_SHARE = 0.5          # a column is "dates" / "ids" / "numbers" when this share of cells fit


def _cell(value) -> str:
    return "" if value is None else _CID.sub("", str(value)).replace("\xa0", " ").strip()


def _share(values: list[str], test) -> float:
    filled = [v for v in values if v]
    return sum(1 for v in filled if test(v)) / len(filled) if filled else 0.0


def table_rows(table: list[list]) -> list[dict]:
    """Extract order rows from one pdfplumber table (list of cell rows), by column content."""
    grid = [[_cell(c) for c in row] for row in table if row]
    width = max((len(r) for r in grid), default=0)
    if width < 4 or len(grid) < 2:
        return []
    cols = [[r[i] if i < len(r) else "" for r in grid] for i in range(width)]

    dates = [i for i, c in enumerate(cols) if _share(c, lambda v: parse_date(v) is not None) >= _MIN_SHARE]
    if not dates:
        return []
    date_i = max(dates, key=lambda i: _share(cols[i], lambda v: parse_date(v) is not None))
    ids = [i for i, c in enumerate(cols)
           if i != date_i and _share(c, lambda v: parse_id(v) is not None) >= _MIN_SHARE]
    if not ids:
        return []
    id_i = min(ids, key=lambda i: abs(i - date_i))
    order = RTL_ORDER if id_i > date_i else LTR_ORDER      # id right of the date = right-to-left

    # numeric columns on the far side of the date, nearest first: code, zone, quantity, length
    step = -1 if id_i > date_i else 1
    numeric = [i for i in range(date_i + step, -1 if step < 0 else width, step)
               if i != id_i and _share(cols[i], lambda v: parse_number(v) is not None) >= _MIN_SHARE]
    fields = [f for f in (order[:order.index("confirmation_date")][::-1] if step < 0
                          else order[order.index("confirmation_date") + 1:]) if f != "order_id"]
    mapping = dict(zip(fields, numeric))
    mapping["confirmation_date"], mapping["order_id"] = date_i, id_i

    rows = []
    for r in grid:
        def get(field):
            i = mapping.get(field)
            return r[i] if i is not None and i < len(r) else ""
        date, order_id = parse_date(get("confirmation_date")), parse_id(get("order_id"))
        if not date or not order_id:                      # header / total / blank rows
            continue
        rows.append({
            "order_id": order_id, "confirmation_date": date,
            "code": get("code"), "zone": get("zone"),
            "original_quantity": parse_number(get("original_quantity")),
            "original_length": parse_number(get("original_length")),
        })
    return rows


def _words_rows(page) -> list[dict]:
    """Fallback for pages without ruled tables: rebuild rows from word positions."""
    words = [((w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2, w["bottom"] - w["top"],
              _CID.sub("", w["text"]), 100.0) for w in page.extract_words()]
    words = [w for w in words if w[3].strip()]
    dates = [w for w in words if parse_date(w[3])]
    ids = [w for w in words if parse_id(w[3])]
    if len(dates) < 2 or not ids:
        return []
    date_x = sum(w[0] for w in dates) / len(dates)
    id_x = sum(w[0] for w in ids) / len(ids)
    order = RTL_ORDER if id_x > date_x else LTR_ORDER
    df = rows_from_words(words, order)
    return df[COLUMNS].to_dict("records") if not df.empty else []


def load_pdf(source) -> pd.DataFrame:
    """Read every order row from a digital PDF (path or file-like object)."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise PdfUnreadable("Install the PDF reader: pip install pdfplumber") from exc

    rows: list[dict] = []
    texts: list[str] = []
    has_text = False
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            has_text = has_text or bool(page.chars)
            texts.append(" ".join(w["text"] for w in page.extract_words()))
            found = [r for t in page.extract_tables() for r in table_rows(t)]
            rows.extend(found or _words_rows(page))
    if not has_text:
        raise PdfUnreadable("This PDF has no text layer (it is a scan). Use the "
                            "\"Scan / photo of a document\" option instead.")
    if not rows:
        raise PdfUnreadable("No order table (dates and order ids) was found in this PDF.")

    df = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("original_quantity", "original_length"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("code", "zone"):
        df[col] = df[col].map(lambda v: _clean_text(str(v)) if v is not None else "")
    df.attrs["incomplete_rows"] = int(df[["original_quantity", "original_length"]].isna().any(axis=1).sum())
    df.attrs["footer_matches"] = footer_matches(df, texts)
    return df


def footer_matches(df: pd.DataFrame, page_texts: list[str]) -> bool:
    """True when the report's own totals line (row count and quantity sum) shows up in the PDF.

    Printed reports end with totals; finding exactly our numbers there is strong evidence that
    every row was read. Returns False when they are absent or different (no claim either way).
    """
    tokens = [t for text in page_texts for t in re.findall(r"\d+(?:\.\d+)?", text)]
    total = float(df["original_quantity"].sum())
    shown = [t for t in tokens if "." in t and abs(float(t) - total) <= 0.01]
    return str(len(df)) in tokens and bool(shown)
