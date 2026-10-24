"""Streamlit view for the LED lighting module ("לדים"), shown next to the Santaf calculator.

render() draws the whole module. It never touches the Santaf screens: app.py calls it only when the
LED module is selected and stops afterwards.
"""
from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import led
import led_ingest
from pdf_ingest import PdfUnreadable

DEFAULT_FOLDER = Path.home() / "Downloads" / "פרגולות"
STORE = led.TrackingStore()

COLUMN_LABELS = {
    "order_id": "הזמנה", "order_date": "תאריך תיאום", "code": "קוד", "order_type": "סוג הזמנה",
    "zone": "איזור", "region": "אזור גיאוגרפי", "length_mm": "אורך (מ״מ)", "quantity": "כמות",
    "meters": "מטרים", "status_he": "סטטוס מק״ט", "stage": "שלב מעקב", "ordered": "הוזמן",
    "received": "נתקבל", "supplied": "סופק", "note": "הערה", "main_order": "הזמנה ראשית",
    "issue": "דורש תשומת לב", "source_file": "קובץ", "kit_ordered": "קיט הוזמן (חיצוני)",
    "kit_ref": "אסמכתא להזמנת קיט", "tracking_note": "הערת מעקב",
}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
@st.cache_data(show_spinner="קורא את הדוחות...")
def _parse(files: tuple) -> tuple[pd.DataFrame, led_ingest.LedLoadReport]:
    """files: ((name, bytes), ...) -> lines and what happened while reading them."""
    return led_ingest.load_led_pdfs([(name, io.BytesIO(data)) for name, data in files])


def _choose_files(on_cloud: bool) -> tuple:
    """The PDFs to read, as ((name, bytes), ...)."""
    options = ["העלאת קבצי PDF"] if on_cloud else ["העלאת קבצי PDF", "תיקייה במחשב"]
    choice = st.radio("מקור הדוחות", options, horizontal=True, key="led_source")
    if choice == "העלאת קבצי PDF":
        uploaded = st.file_uploader("דוחות לדים (PDF דיגיטלי)", type=["pdf"],
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


def _settings() -> led.LedSettings:
    if "led_settings" not in st.session_state:
        st.session_state["led_settings"] = led.load_settings()
    return st.session_state["led_settings"]


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
def _filters(lines: pd.DataFrame) -> pd.DataFrame:
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
        c5, c6 = st.columns(2)
        search = c5.text_input("חיפוש לפי מספר הזמנה", key="led_search")
        with_cancelled = c6.checkbox("כולל הזמנות שבוטלו", value=False, key="led_cancelled")
    if start > end:
        st.error("תאריך ההתחלה אחרי תאריך הסיום.")
        st.stop()
    dates = lines["order_date"].dt.date
    mask = (dates >= start) & (dates <= end) & lines["order_type"].isin(types) \
        & lines["region"].isin(regions) & lines["stage"].isin(stages) & lines["status_he"].isin(statuses)
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
    if (view["region"] == led.UNASSIGNED).any():
        st.info("חלק מקודי האיזור עדיין לא משויכים לצפון / מרכז / דרום. אפשר לשייך בלשונית ״הגדרות״.")

    st.markdown("**לפי שלב מעקב**")
    stage = view.groupby("stage").agg(שורות=("order_id", "size"), רצועות=("quantity", "sum")).reindex(led.STAGES).fillna(0)
    st.dataframe(stage.astype(int).reset_index().rename(columns={"stage": "שלב"}), hide_index=True, width="stretch")

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
    columns = ["order_id", "order_date", "code", "order_type", "zone", "region", "length_mm", "quantity",
               "status_he", "ordered", "received", "supplied", "stage", "main_order", "note", "issue"]
    _show(view, columns)
    csv = view[columns].rename(columns=COLUMN_LABELS).to_csv(index=False).encode("utf-8-sig")
    st.download_button("הורדת CSV", csv, "led_lines.csv", "text/csv")


def _orders_tab(view: pd.DataFrame) -> None:
    grouped = view.groupby(["order_id", "code"], sort=False).agg(
        order_type=("order_type", "first"), region=("region", "first"),
        order_date=("order_date", "min"), lines=("order_id", "size"), strips=("quantity", "sum"),
        meters=("meters", "sum"), stage=("stage", lambda s: " / ".join(sorted(set(s), key=led.STAGES.index))),
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
        "- כל הרצועות של הזמנה אחת (ושל אותו קוד) מחוברות לשנאי מרכזי אחד לפי סכום האורכים. "
        "רק עומס שגדול מהמגבלה הבטוחה של השנאי הגדול ביותר מתחלק בין כמה שנאים.",
    ]
    st.markdown("\n\n".join(rules))
    st.markdown("**טבלת שנאים ומרווח ביטחון**")
    st.dataframe(led.transformer_table(settings).rename(columns={
        "rating_w": "הספק שנאי (וואט)", "safe_load_w": "עומס בטוח (וואט)",
        "max_meters": "מטרים מרביים", "max_full_strips": "רצועות של 4 מטר מרביות"}),
        hide_index=True, width="stretch")
    kits = led.kits_table(view, settings)
    if kits.empty:
        st.info("אין שורות עם אורך וכמות בסינון הנוכחי.")
        return
    st.dataframe(kits.rename(columns={
        "order_id": "הזמנה", "code": "קוד", "order_type": "סוג הזמנה", "region": "אזור גיאוגרפי",
        "strips": "רצועות", "meters": "מטרים", "watts": "וואט נטו", "load_pct": "% מהמגבלה הבטוחה",
        "pieces": "רצועות לפי אורך", "transformers": "שנאים מומלצים", "notes": "הערות"}),
        hide_index=True, width="stretch")
    demand = led.inventory_demand(view, settings)
    c1, c2 = st.columns(2)
    c1.markdown("**ביקוש לרצועות לפי אורך (אחרי פיצול)**")
    c1.dataframe(pd.DataFrame({"אורך (מ״מ)": list(demand["strips"]), "כמות": list(demand["strips"].values())}),
                 hide_index=True, width="stretch")
    c2.markdown("**ביקוש לשנאים לפי הספק**")
    c2.dataframe(pd.DataFrame({"הספק (W)": list(demand["transformers"]),
                               "כמות": list(demand["transformers"].values())}),
                 hide_index=True, width="stretch")


def _tracking_tab(lines: pd.DataFrame) -> None:
    st.caption("שדות המעקב נשמרים לפי שורה. שלבי ״הוזמן / נתקבל / סופק״ נקראים מהדוח; כאן אפשר לתעד "
               "הזמנת קיט חיצונית, אסמכתא והערה.")
    tracked = STORE.apply(lines)
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


def _settings_tab(all_lines: pd.DataFrame, settings: led.LedSettings) -> None:
    st.markdown("**שיוך קודי איזור לצפון / מרכז / דרום**")
    zones = sorted(all_lines["zone"].unique())
    table = pd.DataFrame({"zone": zones, "region": [settings.zone_regions.get(z) or led.UNASSIGNED for z in zones]})
    edited = st.data_editor(
        table, hide_index=True, width="stretch", key="led_zone_editor", disabled=["zone"],
        column_config={"zone": st.column_config.TextColumn("קוד איזור"),
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
    if st.button("שמירת הגדרות", key="led_save_settings"):
        try:
            ratings = tuple(sorted({int(x) for x in ratings_text.replace(" ", "").split(",") if x}))
            if not ratings or min_strip > max_strip:
                raise ValueError
        except ValueError:
            st.error("בדקו את הערכים: הספקים כמספרים, ואורך מינימלי שאינו גדול מהמקסימלי.")
            return
        new = led.LedSettings(
            zone_regions={r.zone: r.region for r in edited.itertuples() if r.region != led.UNASSIGNED},
            max_strip_mm=int(max_strip), min_strip_mm=int(min_strip), transformer_ratings=ratings,
            safety_factor=float(safety), watts_per_meter=float(watts))
        st.session_state["led_settings"] = new
        saved = led.save_settings(new)
        st.success("ההגדרות עודכנו" + ("" if saved else " (לא נשמרו לדיסק בסביבה הזו)"))
        st.rerun()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def render(on_cloud: bool = False) -> None:
    """Draw the LED module."""
    st.subheader("תאורת לד לפרגולות")
    files = _choose_files(on_cloud)
    if not files:
        st.info("העלו את דוחות הלדים (״דוח פירוט תוספות לפי תאריך תיאום״) כדי להתחיל.")
        return
    try:
        raw, report = _parse(files)
    except PdfUnreadable as exc:
        st.error(str(exc))
        return
    for name, why in report.errors.items():
        st.error(f"דילגתי על {name}: {why}")
    if report.duplicates:
        st.warning(f"{report.duplicates} שורות מופיעות ביותר מדוח אחד (הדוחות חופפים) ונספרות פעם אחת.")
    if report.confirmed and not report.unconfirmed:
        st.success("מספר השורות וסך הכמות תואמים לסיכומים שמודפסים בדוחות.")
    elif report.unconfirmed:
        st.caption("לא ניתן היה לאמת את הסיכומים המודפסים עבור: " + ", ".join(report.unconfirmed))

    settings = _settings()
    lines = led.prepare(raw, settings)
    view = _filters(lines)
    if view.empty:
        st.warning("אין שורות שמתאימות לסינון.")
    tabs = st.tabs(["סיכום", "שורות", "לפי הזמנה", "קיטים (הכנה)", "מעקב", "הגדרות"])
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
        _tracking_tab(view if not view.empty else lines.iloc[0:0])
    with tabs[5]:
        _settings_tab(lines, settings)
