"""Streamlit view for the Santaf stock plan ("מלאי סנטפים"), shown next to the Santaf calculator and the LED module.

render() draws the whole module; app.py calls it only when this module is selected and stops afterwards, so the
Santaf screens are never touched. The stock file, the order reports and the settings are saved to disk
(stock_store) and loaded on start: a refresh does not ask for the files again.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import streamlit as st

import calculator as calc
import pdf_ingest
import stock_autoload
import stock_plan as sp
import stock_store
from uploads import upload_types

ORDER_TYPES = ["xlsx", "xls", "csv", "pdf"]            # Excel first (the default); the uploader adds the MIME types
LABELS = {
    "date": "תאריך", "order_id": "הזמנה", "need_size": "מידה חסרה", "need_qty": "כמות חסרה", "donor": "מידת תורם",
    "panels_cut": "פנלים לחיתוך", "pieces_per_panel": "חלקים לפנל", "pieces_made": "חלקים שנוצרו",
    "spare_pieces": "חלקים עודפים", "offcut_size": "שארית (מ״מ)", "donor_before": "תורם לפני",
    "donor_after": "תורם אחרי", "donor_future_demand": "ביקוש עתידי של התורם", "donor_margin": "מרווח תורם",
    "instruction": "הוראה", "seq": "סדר", "panels": "פנלים", "demand": "דרישה", "status": "סטטוס",
    "missing": "חסר", "rescued_by_cut": "ניצלה בחיתוך", "size": "מידה", "stock": "מלאי", "net_shortage": "חוסר נטו",
    "surplus": "עודף", "class": "סיווג", "stockout_date": "תאריך סיום מלאי", "days_of_stock": "ימי מלאי",
    "avg_per_day": "ממוצע ליום", "orders_stuck": "הזמנות תקועות", "buffer": "מרווח", "order_qty": "כמות להזמין",
    "priority": "עדיפות",
}


def _dmy(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%d/%m/%Y")


def _show(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = frame[columns].copy()
    for column in columns:
        if column in ("date", "stockout_date"):
            out[column] = out[column].map(_dmy)
    return out.rename(columns=LABELS)


# --------------------------------------------------------------------------
# Data: saved on disk, new uploads are merged in and saved
# --------------------------------------------------------------------------
def _read_orders_upload(upload) -> pd.DataFrame:
    if upload.name.lower().endswith(".pdf") or upload.getvalue()[:5] == b"%PDF-":      # also a PDF without an extension
        frame, report = pdf_ingest.load_many([(upload.name, upload)])
        if report.errors:
            raise ValueError("; ".join(report.errors.values()))
        return frame
    return calc.load_orders(upload)


def _autoload(store: stock_store.StockStore, settings: dict, on_cloud: bool, force: bool = False) -> None:
    """Read the stock file and the order reports found in Downloads / the working folder (local runs only)."""
    if on_cloud or not (force or not st.session_state.get("_autoload_done")):
        return
    st.session_state["_autoload_done"] = True
    try:
        report = stock_autoload.scan(store, zero_sizes=tuple(settings["zero_sizes"]))
    except Exception as exc:                                # a problem here must never stop the module
        st.warning(f"הטעינה האוטומטית נכשלה: {exc}")
        return
    st.session_state["_autoload_msg"] = stock_autoload.describe(report)
    for name, why in report.errors.items():
        st.warning(f"{name}: {why}")


def _data_section(store: stock_store.StockStore, settings: dict, on_cloud: bool = False) -> None:
    saved_stock, saved_orders = store.load_stock(), store.load_orders()
    with st.expander("נתונים שמורים והעלאה", expanded=saved_stock is None or saved_orders is None):
        st.caption("הקבצים נשמרים בדיסק ונטענים אוטומטית בכל פתיחה. העלאה חדשה מתווספת לשמור (בלי כפילויות).")
        if not on_cloud:
            if st.button("סרוק את תיקיית ההורדות עכשיו", key="stock_scan"):
                _autoload(store, settings, on_cloud, force=True)
                st.rerun()
            if st.session_state.get("_autoload_msg"):
                st.info("טעינה אוטומטית: " + st.session_state["_autoload_msg"])
        left, right = st.columns(2)
        stock_file = left.file_uploader("קובץ מלאי (Excel)", type=upload_types("xlsx", "xls"), key="stock_upload")
        order_files = right.file_uploader("דוחות הזמנות סנטפים (PDF / Excel)", type=upload_types(*ORDER_TYPES),
                                          accept_multiple_files=True, key="stock_orders_upload")
        signature = (stock_file.name, stock_file.size) if stock_file else None
        if stock_file and st.session_state.get("_stock_seen") != signature:
            try:
                loaded = sp.load_stock(stock_file, add_zero_sizes=tuple(settings["zero_sizes"]))
                store.save_stock(loaded, stock_file.name)
                st.session_state["_stock_seen"] = signature
                st.success(f"המלאי נשמר ({stock_file.name}, נכון ל-{_dmy(loaded.as_of)})")
            except (ValueError, stock_store.StoreError) as exc:
                st.error(f"קובץ המלאי לא נקרא: {exc}")
        for upload in order_files or []:
            key = (upload.name, upload.size)
            if key in st.session_state.setdefault("_orders_seen", []):
                continue
            try:
                added = store.save_orders(_read_orders_upload(upload), upload.name)
                st.session_state["_orders_seen"].append(key)
                st.success(f"{upload.name}: נשמרו {added} שורות חדשות")
            except Exception as exc:              # a bad file must not stop the module
                st.error(f"{upload.name} לא נקרא: {exc}")
        saved_stock, saved_orders = store.load_stock(), store.load_orders()
        if saved_stock:
            stock, info = saved_stock
            st.write(f"מלאי שמור: **{info['source']}** · נכון ל-{_dmy(stock.as_of)} · {sum(stock.quantities.values())} יח'"
                     + (f" · נוספו ב-0: {', '.join(map(str, stock.zero_added))}" if stock.zero_added else ""))
        if saved_orders is not None:
            files = store.orders_files()
            st.write(f"הזמנות שמורות: **{len(saved_orders)}** שורות מ-{len(files)} קבצים")
    return None


def _backup_section(store: stock_store.StockStore, on_cloud: bool) -> None:
    st.markdown("**גיבוי ושחזור**")
    if on_cloud:
        st.info("בענן הדיסק זמני: הורד גיבוי ושחזר אותו אחרי הפעלה מחדש של האפליקציה.")
    st.download_button("הורד גיבוי (zip)", store.export_zip(), file_name="stock-plan-backup.zip", key="stock_dl")
    restore = st.file_uploader("שחזור מגיבוי", type=upload_types("zip"), key="stock_restore")
    if restore and st.session_state.get("_restore_seen") != (restore.name, restore.size):
        try:
            names = store.import_zip(restore.getvalue())
            st.session_state["_restore_seen"] = (restore.name, restore.size)
            st.success(f"שוחזרו: {', '.join(names)}")
            st.rerun()
        except stock_store.StoreError as exc:
            st.error(str(exc))


# --------------------------------------------------------------------------
# Settings (saved)
# --------------------------------------------------------------------------
def _settings_row(store: stock_store.StockStore) -> dict:
    saved = store.load_settings()
    a, b, c, d, e = st.columns([1.2, 1.2, 1, 1, 1.6])
    start = a.date_input("הזמנות מתאריך", pd.Timestamp(saved["start"]).date(), format="DD/MM/YYYY", key="stock_start")
    end = b.date_input("עד תאריך", pd.Timestamp(saved["end"]).date(), format="DD/MM/YYYY", key="stock_end")
    kerf = c.number_input("רוחב להב (מ״מ)", 0, 50, int(saved["kerf_mm"]), key="stock_kerf")
    buffer = d.number_input("ימי מרווח לרכש", 0, 60, int(saved["buffer_days"]), key="stock_buffer")
    skip = e.toggle("לדלג על הזמנה תקועה", bool(saved["skip_stuck"]), key="stock_skip",
                    help="כבוי = קו ייצור קשיח: אחרי הזמנה תקועה לא משחררים כלום")
    zeros = st.text_input("מידות שנוספו ברשימה עם 0 יח' (מופרדות בפסיק)",
                          ", ".join(map(str, saved["zero_sizes"])), key="stock_zero")
    sizes = [int(t) for t in zeros.replace(",", " ").split() if t.isdigit()]
    settings = {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"), "skip_stuck": bool(skip),
                "kerf_mm": int(kerf), "buffer_days": int(buffer), "zero_sizes": sizes}
    try:
        store.save_settings(settings)
    except stock_store.StoreError:
        pass
    return settings


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------
def _cut_tab(res, cuts, base, orders, stock, problems) -> None:
    f0, f1 = sp.findings(base), sp.findings(res)
    cols = st.columns(4)
    cols[0].metric("הזמנות פתוחות", len(res))
    cols[1].metric("משתחררות בלי חיתוך", f0.released_total)
    cols[2].metric("משתחררות עם חיתוך", f1.released_total, delta=f1.released_total - f0.released_total)
    cols[3].metric("עדיין תקועות", f1.stuck_total)
    if f1.stuck_at:
        st.warning(f"התקיעה הראשונה: הזמנה {f1.stuck_at['order_id']} ב-{_dmy(f1.stuck_at['date'])} (חסר {f1.stuck_at['missing']})")
    else:
        st.success("כל ההזמנות הפתוחות משתחררות.")
    if problems:
        st.error("בדיקת התוכנית נכשלה:\n\n" + "\n".join(f"- {p}" for p in problems))
    else:
        st.caption("✔ נבדק: המלאי לא יורד מתחת ל-0, אף תורם לא נשאר בחסר מול ההזמנות העתידיות שלו, "
                   "ושום הזמנה שיוצאת בלי חיתוך לא נתקעת בגללו.")
    if cuts.empty:
        st.info("אין חיתוכים נדרשים: אין הזמנה תקועה שאפשר להציל מעודף.")
        return
    dates = sorted(cuts["date"].dt.date.unique())
    today = date.today()
    upcoming = [d for d in dates if d >= today]
    options = ["כל התקופה", *dates]
    pick = st.selectbox("הוראות ליום", options, index=options.index(upcoming[0]) if upcoming else 0,
                        format_func=lambda d: d if isinstance(d, str) else d.strftime("%d/%m/%Y"), key="stock_day")
    shown = cuts if isinstance(pick, str) else cuts[cuts["date"].dt.date == pick]
    st.markdown(f"**{len(shown)} חיתוכים · {int(shown['panels_cut'].sum())} פנלים לחיתוך**")
    for row in shown.itertuples():
        st.markdown(f"- **{_dmy(row.date)} · הזמנה {row.order_id}** — {row.instruction}")
    with st.expander("טבלת החיתוכים המלאה"):
        columns = ["date", "order_id", "need_size", "need_qty", "donor", "panels_cut", "pieces_per_panel",
                   "spare_pieces", "offcut_size", "donor_before", "donor_after", "donor_future_demand", "donor_margin"]
        st.dataframe(_show(cuts, columns), hide_index=True, use_container_width=True)
    st.download_button("הורד את תוכנית החיתוך (CSV)", _show(cuts, ["date", "order_id", "instruction"])
                       .to_csv(index=False).encode("utf-8-sig"), file_name="cut-plan.csv", key="stock_cuts_dl")


def _orders_tab(res) -> None:
    status = st.segmented_control("סטטוס", ["הכל", "שוחררה", "שוחררה בחיתוך", "תקועה"], default="הכל",
                                  key="stock_status", label_visibility="collapsed") or "הכל"
    view = res.copy()
    view["status"] = view.apply(lambda r: "שוחררה בחיתוך" if r["rescued_by_cut"] else r["status"], axis=1)
    if status != "הכל":
        view = view[view["status"] == status]
    st.caption(f"{len(view)} הזמנות")
    st.dataframe(_show(view, ["seq", "date", "order_id", "panels", "demand", "status", "missing"]),
                 hide_index=True, use_container_width=True)


def _sizes_tab(sizes, plan) -> None:
    st.markdown("**מידות: חוסרים ועודפים**")
    st.dataframe(_show(sizes, ["size", "stock", "demand", "net_shortage", "surplus", "class", "stockout_date",
                               "days_of_stock", "avg_per_day", "orders_stuck"]), hide_index=True, use_container_width=True)
    st.markdown("**המלצת רכש מול הספק** (חוסר נטו + מרווח ימים בקצב הממוצע)")
    buy = plan[plan["order_qty"] > 0]
    st.dataframe(_show(buy, ["priority", "size", "stock", "net_shortage", "buffer", "order_qty", "stockout_date"]),
                 hide_index=True, use_container_width=True)


def _ids_from_upload(upload) -> str:
    """The text of a list of order numbers: .txt/.csv as is, Excel cell by cell."""
    name = upload.name.lower()
    if name.endswith((".xlsx", ".xls")):
        table = pd.read_excel(upload, header=None, dtype=str)
        return " ".join(table.fillna("").astype(str).values.ravel())
    return upload.getvalue().decode("utf-8-sig", errors="ignore")


def _daily_tab(store, everything, orders, stock, effect) -> None:
    st.markdown("**עדכון יומי: הזמנות שכבר קיבלו סנטפים מהמלאי**")
    st.caption("פרגולה שמועד ההתקנה שלה עוד רחוק אבל כבר ניתנו לה סנטפים: סמן אותה כאן. היא יוצאת מהתור, והחומר "
               "שלה יורד מהמלאי (אם הונפקה אחרי תאריך קובץ המלאי), ולכן ההזמנות הבאות, התקיעות והחיתוכים מחושבים מחדש.")
    known = set(everything["order_id"])
    queue = orders.set_index("order_id")
    pasted = st.text_area("מספרי הזמנות (הדבק, מופרדים ברווח / פסיק / שורה)", key="stock_issue_text", height=90)
    upload = st.file_uploader("או קובץ עם מספרי הזמנות (Excel / CSV)", type=upload_types("xlsx", "xls", "csv"),
                              key="stock_issue_file")
    picks = st.multiselect("או בחר מהתור הפתוח", list(queue.index), key="stock_issue_pick",
                           format_func=lambda o: f"{o} · {_dmy(queue.loc[o, 'date'])} · {int(queue.loc[o, 'panels'])} פנלים")
    text = pasted + (" " + _ids_from_upload(upload) if upload else "")
    ids = list(dict.fromkeys([*sp.parse_order_ids(text), *picks]))
    c1, c2 = st.columns([1, 2])
    day = c1.date_input("הונפקו בתאריך", date.today(), format="DD/MM/YYYY", key="stock_issue_day")
    note = c2.text_input("הערה (לא חובה)", key="stock_issue_note")
    usable = [i for i in ids if i in known]
    unknown = [i for i in ids if i not in known]
    if ids:
        st.write(f"זוהו {len(ids)} הזמנות: {len(usable)} בדוחות"
                 + (f" · לא נמצאו בדוחות: {', '.join(unknown)}" if unknown else ""))
    if st.button("סמן כמונפקו", disabled=not usable, key="stock_issue_go"):
        try:
            added = store.mark_issued(usable, day, note)
            st.session_state["_issue_msg"] = f"סומנו {added} הזמנות חדשות"
            for key in ("stock_issue_text", "stock_issue_pick", "stock_issue_note"):
                st.session_state.pop(key, None)
            st.rerun()
        except stock_store.StoreError as exc:
            st.error(str(exc))
    if st.session_state.get("_issue_msg"):
        st.success(st.session_state.pop("_issue_msg"))

    table = effect.table
    st.markdown(f"**הזמנות שסומנו כמונפקו ({len(table)})**")
    if effect.unknown:
        st.warning("סומנו הזמנות שאינן בדוחות שנטענו (לא ניתן לנכות את החומר שלהן): " + ", ".join(effect.unknown))
    if effect.shortfall:
        st.warning("הניכוי היה גדול מהמלאי במידות: "
                   + ", ".join(f"{s} (חסרו {q})" for s, q in sorted(effect.shortfall.items()))
                   + ". כנראה קובץ המלאי כבר מעודכן; בדוק את תאריך ההנפקה.")
    if len(table):
        shown = table.assign(order_date=table["order_date"].map(_dmy), issued_on=table["issued_on"].map(_dmy),
                             deducted=table["deducted"].map({True: "כן", False: "לא (כבר במלאי)"}))
        st.dataframe(shown.rename(columns={"order_id": "הזמנה", "order_date": "תאריך הזמנה", "issued_on": "הונפקה",
                                           "panels": "פנלים", "demand": "דרישה", "deducted": "נוכתה מהמלאי",
                                           "note": "הערה"}), hide_index=True, use_container_width=True)
        undo = st.multiselect("ביטול סימון", list(table["order_id"]), key="stock_issue_undo")
        if undo and st.button("בטל סימון", key="stock_issue_undo_go"):
            store.unmark_issued(undo)
            st.session_state.pop("stock_issue_undo", None)
            st.rerun()
    before, after = stock.quantities, effect.stock.quantities
    moved = [{"מידה": s, "מלאי בקובץ": before.get(s, 0), "מלאי אחרי ניכוי": after.get(s, 0),
              "ירד": before.get(s, 0) - after.get(s, 0)} for s in sorted(set(before) | set(after))
             if before.get(s, 0) != after.get(s, 0)]
    if moved:
        st.markdown("**השפעה על המלאי**")
        st.dataframe(pd.DataFrame(moved), hide_index=True, use_container_width=True)
    st.info("עדכון יומי של הדוחות: דוח הזמנות חדש (גם מתיקיית ההורדות) מחליף את השורות של ההזמנות שהוא מכיל, "
            "כך שהזמנה ששינתה תאריך או כמות לא נספרת פעמיים. קובץ מלאי חדש מחליף את הקודם, והסימונים שקדמו "
            "לתאריכו מפסיקים לנכות מעצמם.")


# --------------------------------------------------------------------------
store = stock_store.StockStore()


def render(on_cloud: bool = False) -> None:
    """Draw the stock module."""
    global store
    store = stock_store.StockStore()                       # re-read PERGOLA_DATA_DIR on every run
    st.subheader("תכנון מלאי סנטפים וחיתוך חכם")
    settings = _settings_row(store)
    _autoload(store, settings, on_cloud)
    _data_section(store, settings, on_cloud)
    saved_stock, raw = store.load_stock(), store.load_orders()
    if saved_stock is None or raw is None:
        st.info("העלה קובץ מלאי ודוחות הזמנות. הם יישמרו ולא יידרשו שוב.")
        return
    stock = saved_stock[0]
    for size in settings["zero_sizes"]:                    # sizes added to the list with 0 units
        if size not in stock.quantities:
            stock.quantities[size] = 0
            stock.zero_added = [*stock.zero_added, size]
    stock.quantities = dict(sorted(stock.quantities.items()))
    try:
        panels = calc.process_orders(raw)
        everything = sp.order_demand(panels)
    except ValueError as exc:
        st.error(f"הנתונים השמורים לא עובדו: {exc}")
        return
    # orders that already got their panels (daily update) leave the queue; those issued after the stock date
    # also take their material out of the stock
    issued = store.issued()
    effect = sp.apply_issued(everything, stock, issued)
    stock_now = effect.stock
    orders = sp.drop_orders(sp.open_orders(everything, settings["start"], settings["end"]), issued)
    if orders.empty:
        st.warning("אין הזמנות בטווח התאריכים שנבחר.")
        return
    skip, kerf = settings["skip_stuck"], settings["kerf_mm"]
    base = sp.simulate(orders, stock_now, skip_stuck=skip)
    res, cuts = sp.simulate_with_cuts(orders, stock_now, skip_stuck=skip, kerf_mm=kerf)
    problems = sp.verify_cuts(orders, stock_now, res, cuts, base)
    sizes = sp.classify_sizes(sp.size_table(orders, stock_now, base))
    plan = sp.purchase_plan(sizes, settings["buffer_days"])
    f1 = sp.findings(res)
    try:
        store.save_run({"computed_at": datetime.now().isoformat(timespec="seconds"), "from": settings["start"],
                        "to": settings["end"], "orders": int(len(res)), "released": f1.released_total,
                        "stuck": f1.stuck_total, "cuts": int(len(cuts)), "checks_ok": not problems}, cuts)
    except stock_store.StoreError:
        pass
    st.caption(f"מלאי נכון ל-{_dmy(stock.as_of)} · הזמנות מ-{_dmy(settings['start'])} עד {_dmy(settings['end'])} "
               f"(הזמנות קודמות כבר יצאו ולא נספרות)")
    if len(effect.table):
        deducted = int(effect.table.loc[effect.table["deducted"], "panels"].sum())
        st.caption(f"עדכון יומי: {len(effect.table)} הזמנות כבר קיבלו סנטפים והוסרו מהתור; "
                   f"{deducted} פנלים נוכו מהמלאי (הזמנות שהונפקו אחרי תאריך המלאי).")
    tabs = st.tabs(["תוכנית חיתוך חכמה", "עדכון יומי", "הזמנות", "מידות ורכש", "גיבוי"])
    with tabs[0]:
        _cut_tab(res, cuts, base, orders, stock_now, problems)
    with tabs[1]:
        _daily_tab(store, everything, orders, stock, effect)
    with tabs[2]:
        _orders_tab(res)
    with tabs[3]:
        _sizes_tab(sizes, plan)
    with tabs[4]:
        _backup_section(store, on_cloud)
