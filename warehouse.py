"""The warehouse list of LED kits and transformers, and how it is checked against the LED reports.

The list is an Excel sheet with an order number and the transformers allocated to it, written like
`1*100W`, `1*100W+1*150W` or `3*150W` (the file "לדים ושנאים במחסן"). It is a PARTIAL list today and more
rows will follow, so:
  * columns are found by their content, not by header text or position;
  * several lists are merged without double counting (a later file only adds rows);
  * an order that is in a report but not in the list is "not verified", never "missing";
  * the lists are matched to the reports by order number and by the "main order" named in the notes of
    repair orders (code 67), whose LEDs sit on that main order.

The recommended allocation of led.py is compared with the real allocation here (cross_reference and
calibration), so the allocation rule can be tuned as the rest of the list arrives.
"""
from __future__ import annotations

import io
import re
from collections import Counter

import pandas as pd

import led
from pdf_ingest import parse_id

COLUMNS = ["order_id", "allocation", "alloc_raw", "units", "total_w", "unit_count", "note",
           "parse_ok", "source_file", "row"]

_ALLOCATION_CELL = re.compile(r"^\s*\d+\s*[*xX×]\s*\d+(?:\.\d+)?\s*[wW]?(?:\s*\+\s*\d+\s*[*xX×]\s*\d+(?:\.\d+)?\s*[wW]?)*\s*$")


class WarehouseUnreadable(RuntimeError):
    """The file has no column of order numbers."""


def _read_table(source, name: str) -> pd.DataFrame:
    """Read an Excel / CSV file into a header-less table of strings."""
    raw = source.read() if hasattr(source, "read") else open(source, "rb").read()
    lowered = (name or str(getattr(source, "name", source))).lower()
    if lowered.endswith(".csv"):
        for enc in ("utf-8-sig", "cp1255", "latin-1"):
            try:
                return pd.read_csv(io.StringIO(raw.decode(enc)), header=None, dtype=str, sep=None, engine="python")
            except UnicodeDecodeError:
                continue
    try:
        return pd.read_excel(io.BytesIO(raw), header=None, dtype=str)
    except Exception as exc:
        raise WarehouseUnreadable(f"This file could not be read as Excel or CSV ({type(exc).__name__}).") from exc


def _order_text(value) -> str:
    text = str(value).strip()
    return re.sub(r"\.0$", "", text) if text.lower() != "nan" else ""


def normalize_allocation(text) -> tuple[list[float], str]:
    """(units, text): units as a flat list of ratings, text written as 1*100W+1*150W (sorted by size)."""
    units: list[float] = []
    for count, watts in led.parse_allocation(text):
        units += [watts] * count
    return units, "+".join(f"{n}*{w:g}W" for w, n in sorted(Counter(units).items()))


def load_warehouse(source, name: str = "") -> pd.DataFrame:
    """Read one warehouse list (path or file-like object) into one row per list line."""
    table = _read_table(source, name)
    ids = {c: sum(1 for v in table[c].map(_order_text) if parse_id(v)) for c in table.columns}
    allocs = {c: sum(1 for v in table[c].dropna() if _ALLOCATION_CELL.match(str(v))) for c in table.columns}
    id_col = max(ids, key=ids.get)
    if ids[id_col] == 0:
        raise WarehouseUnreadable("No column of 6-digit order numbers was found in this list.")
    alloc_col = max((c for c in table.columns if c != id_col), key=lambda c: allocs[c], default=None)
    extra = [c for c in table.columns if c not in (id_col, alloc_col)]
    rows = []
    for index, record in table.iterrows():
        order_id = parse_id(_order_text(record[id_col]))
        if not order_id:
            continue
        raw = "" if alloc_col is None or pd.isna(record[alloc_col]) else str(record[alloc_col]).strip()
        units, text = normalize_allocation(raw)
        note = " | ".join(str(record[c]).strip() for c in extra if pd.notna(record[c]) and str(record[c]).strip())
        rows.append({
            "order_id": order_id, "allocation": text, "alloc_raw": raw,
            "units": ",".join(f"{u:g}" for u in units), "total_w": float(sum(units)),
            "unit_count": len(units), "note": note, "parse_ok": bool(units),
            "source_file": name or str(getattr(source, "name", source)), "row": int(index) + 1,
        })
    if not rows:
        raise WarehouseUnreadable("No order rows were found in this list.")
    return pd.DataFrame(rows, columns=COLUMNS)


def merge_warehouse(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Union of several (partial) lists: a row found in more files counts once, repeats inside a file stay."""
    key = ["order_id", "allocation", "alloc_raw"]
    numbered = [f.assign(_n=f.groupby(key).cumcount()) for f in frames]
    combined = pd.concat(numbered, ignore_index=True)
    merged = combined.drop_duplicates(subset=[*key, "_n"], keep="last").drop(columns="_n")
    return merged.reset_index(drop=True)


# --------------------------------------------------------------------------
# Matching the list with the reports
# --------------------------------------------------------------------------
IN_LIST = "ברשימת המחסן"
IN_LIST_VIA_MAIN = "ברשימת המחסן (הזמנה ראשית)"
REPORT_ONLY = "הדוח: במחסן, לא ברשימה"


def annotate_lines(lines: pd.DataFrame, warehouse: pd.DataFrame | None) -> pd.DataFrame:
    """Add `warehouse_state`, `wh_alloc`, `wh_alloc_w` to the (prepared) report lines.

    An order counts as being in the warehouse list when its own number is listed, or when its note names a
    main order that is listed. A report line marked "במחסן" that the list does not (yet) contain is shown
    as not verified because the list is partial.
    """
    out = lines.copy()
    by_order: dict[str, list[tuple[str, float]]] = {}
    if warehouse is not None and not warehouse.empty:
        for row in warehouse.itertuples():
            by_order.setdefault(row.order_id, []).append((row.allocation or row.alloc_raw, row.total_w))
    state, alloc, watts = [], [], []
    for row in out.itertuples():
        direct = by_order.get(row.order_id)
        via_main = by_order.get(row.main_order) if row.main_order else None
        hit = direct or via_main
        if direct:
            state.append(IN_LIST)
        elif via_main:
            state.append(IN_LIST_VIA_MAIN)
        elif row.status_key == "in_warehouse":
            state.append(REPORT_ONLY)
        else:
            state.append("")
        alloc.append(" ; ".join(a for a, _ in hit) if hit else "")
        watts.append(float(sum(w for _, w in hit)) if hit else 0.0)
    out["warehouse_state"], out["wh_alloc"], out["wh_alloc_w"] = state, alloc, watts
    return out


def cross_reference(lines: pd.DataFrame, warehouse: pd.DataFrame, settings: led.LedSettings) -> pd.DataFrame:
    """One row per warehouse-list line with what the reports say about the order and a verdict.

    Only the order's own LED lines (direct match) are compared with the allocation; the LEDs of a repair order
    linked through its note are shown separately because they are a different set of strips.
    """
    active = lines[lines["status_key"] != "cancelled"]
    rows = []
    for row in warehouse.itertuples():
        own = active[active["order_id"] == row.order_id]
        linked = active[active["main_order"] == row.order_id]
        record = {
            "order_id": row.order_id, "allocation": row.allocation or row.alloc_raw, "alloc_w": row.total_w,
            "safe_w": round(row.total_w * settings.safety_factor, 1),
            "report_lines": len(own), "report_status": ", ".join(sorted(set(own["status_he"]))) if len(own) else "",
            "stage": ", ".join(sorted(set(own["stage"]))) if len(own) else "",
            "led_meters": round(float(own["meters"].sum()), 2),
            "required_w": round(float(own["meters"].sum()) * settings.watts_per_meter, 1),
            "linked_orders": ", ".join(sorted(set(linked["order_id"]))),
            "linked_meters": round(float(linked["meters"].sum()), 2),
            "recommended": "", "verdict": "",
        }
        if len(own) and record["required_w"] > 0:
            kit = led.kit_for_order(own, settings)
            record["recommended"] = led.format_allocation([t["rating"] for t in kit["transformers"]])
            recommended_w = sum(t["rating"] or 0 for t in kit["transformers"])
            if not row.parse_ok:
                record["verdict"] = "ההקצאה ברשימה לא ברורה"
            elif record["safe_w"] + 1e-9 < record["required_w"]:
                record["verdict"] = "הוקצה פחות מהנדרש"
            elif row.total_w > recommended_w + 1e-9:
                record["verdict"] = "הוקצה יותר מהמומלץ"
            else:
                record["verdict"] = "תואם"
        elif len(own):
            record["verdict"] = "אין אורך בדוח"
        elif len(linked):
            record["verdict"] = "קיים דרך הזמנת תיקון"
        else:
            record["verdict"] = "לא בדוחות שנטענו"
        rows.append(record)
    return pd.DataFrame(rows)


def calibration(lines: pd.DataFrame, warehouse: pd.DataFrame, base: led.LedSettings,
                safety_factors=(0.5, 0.6, 0.7, 0.8, 0.9, 1.0)) -> pd.DataFrame:
    """How often the recommended allocation equals the real one, for several safety factors and both policies.

    Uses only orders found in both the list and the reports with a length. With a handful of orders the
    numbers are an indication, not a verdict; they become meaningful as the rest of the list is added.
    """
    from dataclasses import replace
    active = lines[lines["status_key"] != "cancelled"]
    rows = []
    for policy in led.POLICIES:
        for safety in safety_factors:
            settings = replace(base, safety_factor=safety, allocation_policy=policy)
            compared = matched = 0
            for row in warehouse[warehouse["parse_ok"]].itertuples():
                own = active[active["order_id"] == row.order_id]
                if own["meters"].sum() <= 0:
                    continue
                kit = led.kit_for_order(own, settings)
                compared += 1
                matched += sorted(t["rating"] for t in kit["transformers"]) == sorted(float(u) for u in row.units.split(","))
            rows.append({"policy": led.POLICIES[policy], "safety_factor": safety,
                         "compared": compared, "matches": int(matched)})
    return pd.DataFrame(rows)


def stock_summary(warehouse: pd.DataFrame) -> pd.DataFrame:
    """Transformers listed as being with the kits in the warehouse: units per rating and orders."""
    units: Counter = Counter()
    orders: dict[float, set] = {}
    for row in warehouse[warehouse["parse_ok"]].itertuples():
        for unit in row.units.split(","):
            units[float(unit)] += 1
            orders.setdefault(float(unit), set()).add(row.order_id)
    return pd.DataFrame([{"rating_w": w, "units": n, "orders": len(orders[w])} for w, n in sorted(units.items())],
                        columns=["rating_w", "units", "orders"])
