"""Streamlit view for the LED lighting module ("לדים"), shown next to the Santaf calculator.

render() draws the whole module. It never touches the Santaf screens: app.py calls it only when the
LED module is selected and stops afterwards.

Data comes from the local archive (the most recent reports are loaded on start, earlier months can be
picked), from newly uploaded PDFs (saved to the archive as they are read), or from a folder on this
computer. The warehouse list (partial for now) is merged from every list saved so far.
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import led
import led_archive
import led_ingest
import warehouse
from pdf_ingest import PdfUnreadable

DEFAULT_FOLDER = Path.home() / "Downloads" / "פרגולות"
DEFAULT_WAREHOUSE_FILE = Path.home() / "Downloads" / "לדים ושנאים במחסן.xlsx"
STORE = led.TrackingStore()
NOT_LISTED = "לא ברשימת המחסן"

SRC_SAVED, SRC_NEW, SRC_FOLDER = "נתונים שמורים (היסטוריה)", "העלאת דוחות חדשים", "תיקייה במחשב"

COLUMN_LABELS = {
    "order_id": "הזמנה", "order_date": "תאריך תיאום", "code": "קוד", "order_type": "סוג הזמנה",
    "zone": "קוד איזור", "area": "אזור (עיר)", "region": "אזור גיאוגרפי", "length_mm": "אורך (מ״מ)",
    "quantity": "כמות", "meters": "מטרים", "status_he": "סטטוס מק״ט", "stage": "שלב מעקב",
    "ordered": "הוזמן", "received": "נתקבל", "supplied": "סופק", "note": "הערה",
    "main_order": "הזמנה ראשית", "issue": "דורש תשומת לב", "source_file": "קובץ",
    "warehouse_state": "מצב מחסן", "wh_alloc": "הקצאה במחסן",
    "kit_ordered": "קיט הוזמן (חיצוני)", "kit_ref": "אסמכתא להזמנת קיט", "tracking_note": "הערת מעקב",
}


_ALLOCATION = re.compile(r"\d+\*\d+W(?:\+\d+\*\d+W)*")
LRI, PDI = "\u2066", "\u2069"          # Unicode left-to-right isolate: keeps 1*150W in order inside Hebrew text


def _md(text: str) -> str:
    """Text for st.markdown / st.info with allocations like 1*100W+1*150W shown as code: the * would otherwise
    read as emphasis, and inside Hebrew text the left-to-right pieces get shuffled (so they are isolated)."""
    return _ALLOCATION.sub(lambda m: f"{LRI}`{m.group(0)}`{PDI}", text)


def _dmy(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


# --------------------------------------------------------------------------
# Loading: saved history, new uploads, a folder
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="קורא את הדוחות...")
def _parse(files: tuple) -> tuple[pd.DataFrame, led_ingest.LedLoadReport]:
    """files: ((name, bytes), ...) -> lines and what happened while reading them."""
    return led_ingest.load_led_pdfs([(name, io.BytesIO(data)) for name, data in files])


def _entry_label(entry: dict) -> str:
    period = f"{_dmy(entry.get('period_start', ''))} - {_dmy(entry.get('period_end', ''))}"
    return f"{entry['name']} · {period} · נשמר {entry['saved_at'][:16].replace('T', ' ')}"


def _saved_reports(archive: led_archive.Archive) -> pd.DataFrame | None:
    """The reports picked from the archive; by default the most recent upload."""
    entries = archive.entries("reports")
    if not entries:
        st.info("עדיין אין דוחות שמורים. העלו דוחות חדשים והם יישמרו כאן אוטומטית.")
        return None
    labels = {e["id"]: _entry_label(e) for e in entries}
    if "led_saved_pick" in st.session_state:            # drop ids that no longer exist
        st.session_state["led_saved_pick"] = [i for i in st.session_state["led_saved_pick"] if i in labels]
    # a default and a session-state value together make Streamlit warn: give the default only the first time
    first_time = {} if "led_saved_pick" in st.session_state else \
        {"default": [e["id"] for e in archive.latest_batch("reports")]}
    picked = st.multiselect("דוחות שמורים לתצוגה (ברירת מחדל: ההעלאה האחרונה)", list(labels),
                            format_func=labels.get, key="led_saved_pick", **first_time)
    if not picked:
        st.warning("בחרו לפחות דוח אחד.")
        return None
    frames = []
    for entry in sorted((e for e in entries if e["id"] in picked), key=lambda e: e["saved_at"]):
        try:
            frames.append(archive.load("reports", entry["id"]))
        except (OSError, ValueError, KeyError):
            st.error(f"הדוח השמור {entry['name']} לא נקרא.")
    if not frames:
        return None
    merged, duplicates = led_ingest.merge_led_reports(frames)
    if duplicates:
        st.caption(f"{duplicates} שורות מופיעות ביותר מדוח אחד ונספרות פעם אחת.")
    return merged


def _pick_files(folder_mode: bool) -> tuple:
    """The PDFs to read, as ((name, bytes), ...)."""
    if not folder_mode:
        uploaded = st.file_uploader("דוחות לדים (PDF דיגיטלי)", type=["pdf", "PDF"],
                                    accept_multiple_files=True, key="led_upload")
        return tuple((f.name, f.getvalue()) for f in uploaded or [])
    folder = Path(st.text_input("נתיב התיקייה", str(DEFAULT_FOLDER), key="led_folder"))
    try:
        names = sorted(p.name for p in folder.glob("*.pdf"))
    except OSError:
        names = []
    if not names:
        st.warning("לא נמצאו קבצי PDF בתיקייה הזו.")
        return ()
    picked = st.multiselect("קבצים לטעינה", names, default=names, key="led_picked")
    return tuple((name, (folder / name).read_bytes()) for name in picked)


def _new_reports(archive: led_archive.Archive, folder_mode: bool) -> pd.DataFrame | None:
    """Read uploaded / folder PDFs and save each one to the archive (once, by file hash)."""
    files = _pick_files(folder_mode)
    if not files:
        st.info("העלו את דוחות הלדים (״דוח פירוט תוספות לפי תאריך תיאום״) כדי להתחיל.")
        return None
    try:
        raw, report = _parse(files)
    except PdfUnreadable as exc:
        st.error(str(exc))
        return None
    for name, why in report.errors.items():
        st.error(f"דילגתי על {name}: {why}")
    if report.duplicates:
        st.warning(f"{report.duplicates} שורות מופיעות ביותר מדוח אחד (הדוחות חופפים) ונספרות פעם אחת.")
    if report.confirmed and not report.unconfirmed:
        st.success("מספר השורות וסך הכמות תואמים לסיכומים שמודפסים בדוחות.")
    elif report.unconfirmed:
        st.caption("לא ניתן היה לאמת את הסיכומים המודפסים עבור: " + ", ".join(report.unconfirmed))
    digests = {name: led_archive.file_hash(data) for name, data in files}
    batch = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    new_ids = []
    try:
        for name, frame in report.frames.items():
            entry, is_new = archive.save("reports", name, frame, digests[name], batch)
            if is_new:
                new_ids.append(entry["id"])
    except led_archive.ArchiveError as exc:
        st.warning(f"הדוחות לא נשמרו בהיסטוריה: {exc}")
        return raw
    if new_ids:
        st.session_state["led_saved_pick"] = new_ids     # the saved view then opens on this upload
        st.success(f"נשמרו בהיסטוריה {len(new_ids)} דוחות חדשים.")
    else:
        st.caption("הדוחות האלה כבר שמורים בהיסטוריה.")
    return raw


def _load_reports(archive: led_archive.Archive, on_cloud: bool) -> pd.DataFrame | None:
    options = [SRC_SAVED, SRC_NEW] + ([] if on_cloud else [SRC_FOLDER])
    source = st.radio("מקור הדוחות", options, index=0 if not archive.is_empty() else 1,
                      horizontal=True, key="led_source")
    if source == SRC_SAVED:
        return _saved_reports(archive)
    return _new_reports(archive, folder_mode=source == SRC_FOLDER)


def _warehouse_list(archive: led_archive.Archive) -> pd.DataFrame | None:
    """Every warehouse list saved so far, merged (a later list only adds rows)."""
    frames = []
    for entry in sorted(archive.entries("warehouse"), key=lambda e: e["saved_at"]):
        try:
            frames.append(archive.load("warehouse", entry["id"]))
        except (OSError, ValueError, KeyError):
            st.error(f"רשימת המחסן השמורה {entry['name']} לא נקראה.")
    return warehouse.merge_warehouse(frames) if frames else None


def _search_elsewhere(order: str, archive: led_archive.Archive, wh: pd.DataFrame | None) -> None:
    """The searched order is not in the loaded reports: say where it is, if anywhere."""
    st.warning(f"ההזמנה {order} לא נמצאת בדוחות שנטענו כרגע.")
    if wh is not None:
        hits = wh[wh["order_id"].str.contains(order, regex=False)]
        if not hits.empty:
            st.info(_md("ברשימת המחסן: " + "; ".join(f"{r.order_id} - {r.allocation or r.alloc_raw}"
                                                      for r in hits.itertuples())))
    found = []
    for entry in archive.entries("reports"):
        try:
            frame = archive.load("reports", entry["id"])
        except (OSError, ValueError, KeyError):
            continue
        count = int(frame["order_id"].str.contains(order, regex=False).sum())
        if count:
            found.append(f"{entry['name']} ({count} שורות)")
    if found:
        st.info("ההזמנה נמצאת בדוחות שמורים: " + ", ".join(found) +
                ". בחרו אותם ברשימת הדוחות השמורים (מקור הדוחות: נתונים שמורים).")


def _settings() -> led.LedSettings:
    if "led_settings" not in st.session_state:
        st.session_state["led_settings"] = led.load_settings()
    return st.session_state["led_settings"]


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
FILTER_KEYS = ("led_from", "led_to", "led_types", "led_regions", "led_stages", "led_statuses",
               "led_wh_states", "led_search", "led_cancelled")


def _reset_filters_for_new_data(lines: pd.DataFrame) -> None:
    """Forget the filter choices when the data changes (another month, new regions): a stale date range or
    selection would silently hide the new rows."""
    signature = (len(lines), str(lines["order_date"].min()), str(lines["order_date"].max()),
                 int(lines["quantity"].sum()), tuple(sorted(set(lines["region"]))),
                 tuple(sorted(set(lines["warehouse_label"]))))
    if st.session_state.get("led_data_signature") != signature:
        for key in FILTER_KEYS:
            st.session_state.pop(key, None)
        st.session_state["led_data_signature"] = signature


def _filters(lines: pd.DataFrame) -> pd.DataFrame:
    _reset_filters_for_new_data(lines)
    with st.expander("סינון", expanded=True):
        first, last = lines["order_date"].min().date(), lines["order_date"].max().date()
        c1, c2 = st.columns(2)
        start = c1.date_input("מתאריך", first, min_value=first, max_value=last,
                              format="DD/MM/YYYY", key="led_from")
        end = c2.date_input("עד תאריך", last, min_value=first, max_value=last,
                            format="DD/MM/YYYY", key="led_to")
        types = st.multiselect("סוג הזמנה (קוד 6 / קוד 67)", sorted(lines["order_type"].unique()),
                               default=sorted(lines["order_type"].unique()), key="led_types")
        c3, c4 = st.columns(2)
        regions = c3.multiselect("אזור גיאוגרפי", sorted(lines["region"].unique()),
                                 default=sorted(lines["region"].unique()), key="led_regions")
        stages = c4.multiselect("שלב מעקב", led.STAGES, default=list(led.STAGES), key="led_stages")
        statuses = st.multiselect("סטטוס מק״ט", sorted(lines["status_he"].unique()),
                                  default=sorted(lines["status_he"].unique()), key="led_statuses")
        states = st.multiselect("מצב מחסן", sorted(lines["warehouse_label"].unique()),
                                default=sorted(lines["warehouse_label"].unique()), key="led_wh_states")
        c5, c6 = st.columns(2)
        search = c5.text_input("חיפוש לפי מספר הזמנה", key="led_search")
        with_cancelled = c6.checkbox("כולל הזמנות שבוטלו", value=False, key="led_cancelled")
    if search.strip():
        # An order number overrides every other filter (date, type, region, stage, status, warehouse,
        # cancelled): the order is shown wherever it is. A main order also brings its repair orders.
        st.caption("חיפוש לפי מספר הזמנה פעיל: מוצגת ההזמנה בלי קשר לשאר המסננים "
                   "(וגם הזמנות תיקון שהערתן מצביעה עליה).")
        needle = search.strip()
        return lines[lines["order_id"].str.contains(needle, regex=False)
                     | lines["main_order"].str.contains(needle, regex=False)]
    if start > end:
        st.error("תאריך ההתחלה אחרי תאריך הסיום.")
        st.stop()
    dates = lines["order_date"].dt.date
    mask = (dates >= start) & (dates <= end) & lines["order_type"].isin(types) \
        & lines["region"].isin(regions) & lines["stage"].isin(stages) & lines["status_he"].isin(statuses) \
        & lines["warehouse_label"].isin(states)
    if not with_cancelled:
        mask &= lines["status_key"] != "cancelled"
    if search.strip():
        mask &= lines["order_id"].str.contains(search.strip(), regex=False)
    return lines[mask]


def _show(df: pd.DataFrame, columns: list[str], **kwargs) -> None:
    table = df[columns].copy()
    if "order_date" in table:
        table["order_date"] = table["order_date"].dt.strftime("%d/%m/%Y")
    st.dataframe(table.rename(columns=COLUMN_LABELS), hide_index=True, width="stretch", **kwargs)


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
def _summary(view: pd.DataFrame) -> None:
    st.caption("קוד 6 (פרגולה מלאה) וקוד 67 (תיקון / שירות / תוספת) מוצגים תמיד בנפרד.")
    totals = led.summarize(view).rename(columns={"order_type": "סוג הזמנה", "orders": "הזמנות",
                                                  "lines": "שורות", "strips": "רצועות", "meters": "מטרים"})
    st.dataframe(totals, hide_index=True, width="stretch")
    c1, c2, c3 = st.columns(3)
    c1.metric("הזמנות", view["order_id"].nunique())
    c2.metric("רצועות לד", int(view["quantity"].sum()))
    c3.metric("מטרים", f"{view['meters'].sum():,.1f}")

    st.markdown("**לפי אזור גיאוגרפי וסוג הזמנה (רצועות)**")
    by_region = view.pivot_table(index="region", columns="order_type", values="quantity",
                                 aggfunc="sum", fill_value=0).reset_index()
    st.dataframe(by_region.rename(columns={"region": "אזור גיאוגרפי"}), hide_index=True, width="stretch")
    by_area = view.groupby(["region", "area"], as_index=False).agg(
        הזמנות=("order_id", "nunique"), רצועות=("quantity", "sum"), מטרים=("meters", "sum"))
    by_area["מטרים"] = by_area["מטרים"].round(1)
    st.dataframe(by_area.rename(columns={"region": "אזור גיאוגרפי", "area": "אזור (עיר)"}),
                 hide_index=True, width="stretch")
    if (view["region"] == led.UNASSIGNED).any():
        st.info("חלק מקודי האיזור לא משויכים לצפון / מרכז / דרום. אפשר לשייך בלשונית ״הגדרות״.")

    st.markdown("**לפי שלב מעקב**")
    stage = view.groupby("stage").agg(שורות=("order_id", "size"), רצועות=("quantity", "sum")).reindex(led.STAGES).fillna(0)
    st.dataframe(stage.astype(int).reset_index().rename(columns={"stage": "שלב"}), hide_index=True, width="stretch")

    st.markdown("**לפי מצב מחסן**")
    stock = view.groupby("warehouse_label").agg(הזמנות=("order_id", "nunique"), רצועות=("quantity", "sum"))
    st.dataframe(stock.reset_index().rename(columns={"warehouse_label": "מצב"}), hide_index=True, width="stretch")

    per_length = view[view["length_mm"] > 0].groupby("length_mm", as_index=False)["quantity"].sum()
    if not per_length.empty:
        st.markdown('<h4 dir="rtl" style="text-align:right">רצועות לד לפי אורך מוזמן</h4>', unsafe_allow_html=True)
        per_length["label"] = per_length["length_mm"].astype(str)
        chart = alt.Chart(per_length).mark_bar(color="#2a78d6", size=16, cornerRadiusTopLeft=4,
                                               cornerRadiusTopRight=4).encode(
            x=alt.X("label:N", sort=list(per_length["label"]), title="אורך (מ״מ)", axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("quantity:Q", title="רצועות", axis=alt.Axis(tickMinStep=1)),
            tooltip=[alt.Tooltip("length_mm:Q", title="אורך (מ״מ)"), alt.Tooltip("quantity:Q", title="רצועות")])
        st.altair_chart(chart.properties(height=320), width="stretch")


def _lines_tab(view: pd.DataFrame) -> None:
    columns = ["order_id", "order_date", "code", "order_type", "zone", "area", "region", "length_mm", "quantity",
               "status_he", "ordered", "received", "supplied", "stage", "warehouse_state", "wh_alloc",
               "main_order", "note", "issue"]
    _show(view, columns)
    csv = view[columns].rename(columns=COLUMN_LABELS).to_csv(index=False).encode("utf-8-sig")
    st.download_button("הורדת CSV", csv, "led_lines.csv", "text/csv")


def _orders_tab(view: pd.DataFrame) -> None:
    grouped = view.groupby(["order_id", "code"], sort=False).agg(
        order_type=("order_type", "first"), area=("area", "first"), region=("region", "first"),
        order_date=("order_date", "min"), lines=("order_id", "size"), strips=("quantity", "sum"),
        meters=("meters", "sum"), stage=("stage", lambda s: " / ".join(sorted(set(s), key=led.STAGES.index))),
        warehouse_state=("warehouse_state", "first"), wh_alloc=("wh_alloc", "first"),
        main_order=("main_order", "first"), issue=("issue", lambda s: "; ".join(sorted({i for i in s if i}))),
    ).reset_index()
    grouped["meters"] = grouped["meters"].round(1)
    labels = {**COLUMN_LABELS, "lines": "שורות", "strips": "רצועות"}
    shown = grouped.copy()
    shown["order_date"] = shown["order_date"].dt.strftime("%d/%m/%Y")
    st.dataframe(shown.rename(columns=labels), hide_index=True, width="stretch")
    st.caption("הזמנת שירות (קוד 67) שבהערה שלה מופיעה הזמנה ראשית מקושרת אליה בעמודה ״הזמנה ראשית״.")


def _kits_tab(view: pd.DataFrame, settings: led.LedSettings) -> None:
    st.info("הכנה למלאי מרכזי: כרגע הקיטים עדיין מוזמנים חיצונית לכל הזמנה. כאן מחושב רק מה שמלאי "
            "מרכזי היה צריך להכיל.")
    rules = [
        "**כללים בתוקף**",
        f"- אורך רצועה במלאי: {settings.min_strip_mm} עד {settings.max_strip_mm} מ״מ. אורך ארוך יותר מפוצל.",
        f"- רצועות COB בצפיפות גבוהה (כ-180 לדים למטר): {settings.watts_per_meter:g} וואט למטר. "
        f"רצועה של {settings.max_strip_mm} מ״מ צורכת {led.strip_watts(settings.max_strip_mm, settings.watts_per_meter):g} וואט.",
        f"- שנאי נטען עד {settings.safety_factor:.0%} מההספק שלו. "
        f"למשל שנאי של 60 וואט נטען עד {led.usable_watts(60, settings.safety_factor):g} וואט.",
        f"- הקצאה: {led.POLICIES.get(settings.allocation_policy, settings.allocation_policy)}. "
        "ההקצאה נכתבת כמו ברשימת המחסן, למשל 1*150W או 1*100W+1*150W או 3*150W.",
    ]
    st.markdown(_md("\n\n".join(rules)))
    st.markdown("**טבלת שנאים ומרווח ביטחון**")
    st.dataframe(led.transformer_table(settings).rename(columns={
        "rating_w": "הספק שנאי (וואט)", "safe_load_w": "עומס בטוח (וואט)",
        "max_meters": "מטרים מרביים", "max_full_strips": "רצועות של 4 מטר מרביות"}),
        hide_index=True, width="stretch")
    kits = led.kits_table(view, settings)
    if kits.empty:
        st.info("אין שורות עם אורך וכמות בסינון הנוכחי.")
        return
    kits["wh_alloc"] = kits["order_id"].map(view.groupby("order_id")["wh_alloc"].first()).fillna("")
    st.dataframe(kits.rename(columns={
        "order_id": "הזמנה", "code": "קוד", "order_type": "סוג הזמנה", "region": "אזור גיאוגרפי",
        "strips": "רצועות", "meters": "מטרים", "watts": "וואט נטו", "load_pct": "% מהמגבלה הבטוחה",
        "pieces": "רצועות לפי אורך", "transformers": "שנאים מומלצים", "wh_alloc": "הקצאה במחסן",
        "notes": "הערות"}), hide_index=True, width="stretch")
    demand = led.inventory_demand(view, settings)
    c1, c2 = st.columns(2)
    c1.markdown("**ביקוש לרצועות לפי אורך (אחרי פיצול)**")
    c1.dataframe(pd.DataFrame({"אורך (מ״מ)": list(demand["strips"]), "כמות": list(demand["strips"].values())}),
                 hide_index=True, width="stretch")
    c2.markdown("**ביקוש לשנאים לפי הספק**")
    c2.dataframe(pd.DataFrame({"הספק (W)": list(demand["transformers"]),
                               "כמות": list(demand["transformers"].values())}),
                 hide_index=True, width="stretch")


def _warehouse_upload(archive: led_archive.Archive, on_cloud: bool) -> None:
    """Add a warehouse list (the file may be partial; later lists add rows, nothing is replaced)."""
    added = 0

    def ingest(name: str, data: bytes) -> int:
        try:
            frame = warehouse.load_warehouse(io.BytesIO(data), name)
        except warehouse.WarehouseUnreadable as exc:
            st.error(f"{name}: {exc}")
            return 0
        try:
            return int(archive.save("warehouse", name, frame, led_archive.file_hash(data))[1])
        except led_archive.ArchiveError as exc:
            st.warning(f"{name} נקרא אבל לא נשמר: {exc}")
            return 0

    for upload in st.file_uploader("הוספת רשימת מחסן (Excel / CSV)", type=["xlsx", "xls", "csv"],
                                   accept_multiple_files=True, key="led_wh_upload") or []:
        added += ingest(upload.name, upload.getvalue())
    if not on_cloud:
        path = Path(st.text_input("או נתיב לקובץ במחשב", str(DEFAULT_WAREHOUSE_FILE), key="led_wh_path"))
        if st.button("טעינה מהנתיב", key="led_wh_load_path"):
            if path.is_file():
                added += ingest(path.name, path.read_bytes())
                if not added:
                    st.info("הקובץ הזה כבר שמור (בלי שינוי).")
            else:
                st.error("הקובץ לא נמצא בנתיב הזה.")
    if added:
        st.rerun()


def _warehouse_tab(lines: pd.DataFrame, wh: pd.DataFrame | None, settings: led.LedSettings,
                   archive: led_archive.Archive, on_cloud: bool) -> None:
    st.caption("רשימת המחסן היא הרשומה האמיתית של הקיטים והשנאים. היא חלקית כרגע: רשימות נוספות יתווספו "
               "לקיימות, ולא יוחלף דבר.")
    _warehouse_upload(archive, on_cloud)
    saved = archive.entries("warehouse")
    if wh is None:
        st.info("עדיין לא נטענה רשימת מחסן.")
        return
    st.caption("רשימות שמורות: " + " · ".join(f"{e['name']} ({e['rows']} שורות, נשמר {e['saved_at'][:10]})" for e in saved))
    cross = warehouse.cross_reference(lines, wh, settings)
    direct = int((cross["report_lines"] > 0).sum())
    linked = int((cross["verdict"] == "קיים דרך הזמנת תיקון").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("שורות ברשימה", len(wh))
    c2.metric("נמצאו בדוחות", direct)
    c3.metric("דרך הזמנת תיקון", linked)
    st.info(f"{int((cross['verdict'] == 'לא בדוחות שנטענו').sum())} מתוך {len(cross)} שורות ברשימה אינן בדוחות "
            "שנטענו (כנראה הזמנות מחודשים קודמים; אפשר לטעון אותם מההיסטוריה).")

    st.markdown("**שנאים ברשימת המחסן**")
    st.dataframe(warehouse.stock_summary(wh).rename(columns={"rating_w": "הספק (וואט)", "units": "יחידות",
                                                              "orders": "הזמנות"}), hide_index=True, width="stretch")

    st.markdown("**הרשימה מול הדוחות**")
    show_all = st.checkbox("הצג גם הזמנות שאינן בדוחות שנטענו", value=False, key="led_wh_show_all")
    table = cross if show_all else cross[cross["verdict"] != "לא בדוחות שנטענו"]
    st.dataframe(table.drop(columns=["safe_w"]).rename(columns={
        "order_id": "הזמנה", "allocation": "הקצאה במחסן", "alloc_w": "הספק מוקצה (וואט)",
        "report_lines": "שורות בדוח", "report_status": "סטטוס בדוח", "stage": "שלב",
        "led_meters": "מטרי לד", "required_w": "וואט נדרש", "linked_orders": "הזמנות תיקון מקושרות",
        "linked_meters": "מטרי לד בתיקון", "recommended": "הקצאה מומלצת", "verdict": "בדיקה"}),
        hide_index=True, width="stretch")

    unverified = lines[lines["warehouse_state"] == warehouse.REPORT_ONLY]
    if not unverified.empty:
        st.markdown("**בדוח מסומנות ״במחסן״ אבל אינן ברשימה (לא אומתו)**")
        st.caption("הרשימה חלקית, ולכן זה לא אומר שהן חסרות.")
        _show(unverified, ["order_id", "order_date", "code", "length_mm", "quantity", "status_he", "stage"])

    st.markdown("**כיול: כמה פעמים ההקצאה המומלצת זהה להקצאה האמיתית**")
    result = warehouse.calibration(lines, wh, settings)
    compared = int(result["compared"].max()) if len(result) else 0
    st.caption(f"מבוסס על {'הזמנה אחת' if compared == 1 else str(compared) + ' הזמנות'} שנמצאות גם ברשימה וגם בדוחות (עם אורך). עם מעט הזמנות זו אינדיקציה "
               "בלבד; ככל שתתווסף הרשימה השלמה והדוחות של החודשים הקודמים זה יהיה משמעותי יותר.")
    st.dataframe(result.rename(columns={"policy": "מדיניות הקצאה", "safety_factor": "אחוז העמסה מרבי",
                                        "compared": "הזמנות שהושוו", "matches": "זהות להקצאה האמיתית"}),
                 hide_index=True, width="stretch")


def _tracking_tab(lines: pd.DataFrame) -> None:
    if lines.empty:
        st.info("אין שורות להצגה בסינון הנוכחי.")
        return
    st.caption("שדות המעקב נשמרים לפי שורה. שלבי ״הוזמן / נתקבל / סופק״ נקראים מהדוח; כאן אפשר לתעד "
               "הזמנת קיט חיצונית, אסמכתא והערה.")
    tracked = STORE.apply(lines)
    tracked["kit_ordered"] = tracked["kit_ordered"].astype(bool)         # the checkbox column must be boolean
    columns = ["line_key", "order_id", "code", "length_mm", "quantity", "stage", "kit_ordered", "kit_ref",
               "tracking_note"]
    edited = st.data_editor(
        tracked[columns], hide_index=True, width="stretch", key="led_tracking_editor",
        disabled=["line_key", "order_id", "code", "length_mm", "quantity", "stage"],
        column_config={
            "line_key": None,
            "order_id": st.column_config.TextColumn(COLUMN_LABELS["order_id"]),
            "code": st.column_config.NumberColumn(COLUMN_LABELS["code"]),
            "length_mm": st.column_config.NumberColumn(COLUMN_LABELS["length_mm"]),
            "quantity": st.column_config.NumberColumn(COLUMN_LABELS["quantity"]),
            "stage": st.column_config.TextColumn(COLUMN_LABELS["stage"]),
            "kit_ordered": st.column_config.CheckboxColumn(COLUMN_LABELS["kit_ordered"]),
            "kit_ref": st.column_config.TextColumn(COLUMN_LABELS["kit_ref"]),
            "tracking_note": st.column_config.TextColumn(COLUMN_LABELS["tracking_note"]),
        })
    records = led.TrackingStore.collect(edited)
    c1, c2 = st.columns(2)
    if c1.button("שמירת מעקב", key="led_save_tracking"):
        merged = {**STORE.load(), **records}
        shown = set(tracked["line_key"])
        merged = {k: v for k, v in merged.items() if k not in shown or k in records}
        if STORE.save(merged):
            st.success(f"נשמרו {len(records)} שורות מעקב.")
        else:
            st.warning("אי אפשר לכתוב לדיסק בסביבה הזו. השתמשו בהורדת הגיבוי.")
    c2.download_button("הורדת גיבוי מעקב (JSON)", json.dumps({**STORE.load(), **records}, ensure_ascii=False, indent=1),
                       "led_tracking.json", "application/json")
    backup = st.file_uploader("שחזור מגיבוי", type=["json"], key="led_restore")
    if backup is not None:
        try:
            restored = json.loads(backup.getvalue().decode("utf-8"))
            if isinstance(restored, dict) and st.button("שחזור", key="led_restore_go"):
                STORE.save({**STORE.load(), **restored})
                st.rerun()
        except ValueError:
            st.error("הקובץ אינו גיבוי מעקב תקין.")
    if not STORE.path.parent.exists():
        st.caption("בסביבת ענן הדיסק זמני: שמרו גיבוי בהורדה.")


def _backup_section(archive: led_archive.Archive) -> None:
    st.markdown("**גיבוי ושחזור של ההיסטוריה**")
    st.caption(f"הנתונים נשמרים בתיקייה {archive.root}. בענן הדיסק זמני, ולכן כדאי להוריד גיבוי מדי פעם.")
    st.download_button("הורדת גיבוי (ZIP)", archive.export_zip(),
                       f"pergola_archive_{datetime.now():%Y%m%d}.zip", "application/zip")
    restore = st.file_uploader("שחזור מגיבוי ZIP", type=["zip"], key="led_archive_restore")
    if restore is not None and st.button("שחזור ההיסטוריה", key="led_archive_restore_go"):
        try:
            counts = archive.import_zip(restore.getvalue())
        except led_archive.ArchiveError as exc:
            st.error(str(exc))
        else:
            st.success(f"שוחזרו {counts['added']} פריטים. הקיימים נשארו כמות שהיו.")


def _settings_tab(all_lines: pd.DataFrame, settings: led.LedSettings, archive: led_archive.Archive) -> None:
    st.markdown("**אזורים: שיוך קודי איזור לצפון / מרכז / דרום**")
    regions, areas = led.zone_regions(settings), led.zone_areas(settings)
    zones = sorted(set(all_lines["zone"]) | set(regions))
    table = pd.DataFrame({"zone": zones, "area": [areas.get(z, "") for z in zones],
                          "region": [regions.get(z) or led.UNASSIGNED for z in zones]})
    edited = st.data_editor(
        table, hide_index=True, width="stretch", key="led_zone_editor", disabled=["zone"],
        column_config={"zone": st.column_config.TextColumn("קוד איזור"),
                       "area": st.column_config.TextColumn("אזור (עיר)"),
                       "region": st.column_config.SelectboxColumn(
                           "אזור גיאוגרפי", options=[led.UNASSIGNED, *led.REGIONS], required=True)})
    st.markdown("**כללי רצועות ושנאים**")
    c1, c2, c3 = st.columns(3)
    max_strip = c1.number_input("אורך רצועה מקסימלי (מ״מ)", 1000, 10000, settings.max_strip_mm, 500)
    min_strip = c2.number_input("אורך רצועה מינימלי (מ״מ)", 100, 4000, settings.min_strip_mm, 100)
    safety = c3.number_input("אחוז העמסה מרבי של שנאי", 0.5, 1.0, settings.safety_factor, 0.05)
    watts = st.number_input("צריכת רצועה (וואט למטר, COB בצפיפות גבוהה)", 1.0, 60.0,
                            settings.watts_per_meter, 1.0)
    ratings_text = st.text_input("הספקי שנאים זמינים (וואט, מופרדים בפסיק)",
                                 ", ".join(str(r) for r in settings.transformer_ratings))
    policy = st.selectbox("מדיניות הקצאת שנאים", list(led.POLICIES), format_func=led.POLICIES.get,
                          index=list(led.POLICIES).index(settings.allocation_policy)
                          if settings.allocation_policy in led.POLICIES else 0)
    if st.button("שמירת הגדרות", key="led_save_settings"):
        try:
            ratings = tuple(sorted({int(x) for x in ratings_text.replace(" ", "").split(",") if x}))
            if not ratings or min_strip > max_strip:
                raise ValueError
        except ValueError:
            st.error("בדקו את הערכים: הספקים כמספרים, ואורך מינימלי שאינו גדול מהמקסימלי.")
            return
        new = led.LedSettings(
            zone_regions={r.zone: r.region for r in edited.itertuples()},
            zone_areas={r.zone: r.area for r in edited.itertuples() if r.area},
            max_strip_mm=int(max_strip), min_strip_mm=int(min_strip), transformer_ratings=ratings,
            safety_factor=float(safety), watts_per_meter=float(watts), allocation_policy=policy)
        st.session_state["led_settings"] = new
        saved = led.save_settings(new)
        st.success("ההגדרות עודכנו" + ("" if saved else " (לא נשמרו לדיסק בסביבה הזו)"))
        st.rerun()
    st.divider()
    _backup_section(archive)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def render(on_cloud: bool = False) -> None:
    """Draw the LED module."""
    st.subheader("תאורת לד לפרגולות")
    archive = led_archive.Archive()
    raw = _load_reports(archive, on_cloud)
    if raw is None:
        return

    settings = _settings()
    wh = _warehouse_list(archive)
    lines = warehouse.annotate_lines(led.prepare(raw, settings), wh)
    lines["warehouse_label"] = lines["warehouse_state"].replace("", NOT_LISTED)
    view = _filters(lines)
    searched = str(st.session_state.get("led_search", "")).strip()
    if view.empty and searched:
        _search_elsewhere(searched, archive, wh)
    elif view.empty:
        st.warning("אין שורות שמתאימות לסינון.")
    tabs = st.tabs(["סיכום", "שורות", "לפי הזמנה", "קיטים (הכנה)", "מחסן", "מעקב", "הגדרות"])
    with tabs[0]:
        if not view.empty:
            _summary(view)
    with tabs[1]:
        if not view.empty:
            _lines_tab(view)
    with tabs[2]:
        if not view.empty:
            _orders_tab(view)
    with tabs[3]:
        if not view.empty:
            _kits_tab(view, settings)
    with tabs[4]:
        _warehouse_tab(lines, wh, settings, archive, on_cloud)
    with tabs[5]:
        _tracking_tab(view if not view.empty else lines.iloc[0:0])
    with tabs[6]:
        _settings_tab(lines, settings, archive)
