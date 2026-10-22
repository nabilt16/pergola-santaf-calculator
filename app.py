"""Streamlit UI for the Santaf pergola panel calculator.

Run with:  streamlit run app.py
"""
import hmac
import importlib
import os
from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import calculator as calc
import email_report
import ocr_ingest as ocr
import pdf_ingest
import vision_ingest as vision

# Streamlit re-runs this script on every interaction but keeps imported modules cached, so a
# running server would otherwise keep using an old calculator.py after it is edited. Reload the
# project modules (dependencies first) so the functions called below always match the files.
for _module in (ocr, calc, email_report, pdf_ingest, vision):
    importlib.reload(_module)


@st.cache_data(show_spinner=False)
def extract_cached(images: list, layout: str, engine: str, _api_key: str = ""):
    if engine == ENGINE_VISION:
        return vision.extract_orders(images, _api_key or None)
    return ocr.extract_orders(images, ocr.COLUMN_ORDERS[layout])


st.set_page_config(page_title="Pergola Santaf Calculator", page_icon="📐", layout="wide")
st.title("Pergola Santaf Calculator")


def secret(name, default=None):
    """A Streamlit secret (top level), or `default` when there is no secrets file / no such key."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def secret_section(name) -> dict:
    try:
        return dict(st.secrets.get(name, {}) or {})
    except Exception:
        return {}


# Optional access password (set `app_password` in the app's secrets). The app shows customer
# orders and can send email, so protect it when it is published on a public URL.
if secret("app_password") and not st.session_state.get("authenticated"):
    typed = st.text_input("Password", type="password")
    if typed:
        if hmac.compare_digest(typed.encode(), str(secret("app_password")).encode()):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Wrong password.")
    st.stop()

ON_CLOUD = Path(__file__).resolve().as_posix().startswith("/mount/src")   # Streamlit Community Cloud
IMAGE_TYPES = ["png", "jpg", "jpeg", "jfif"]
ENGINE_LOCAL = "Local OCR (Tesseract) - stays on this computer"
ENGINE_VISION = "AI vision (Claude) - most accurate, sends the photo to Anthropic"

SOURCES = ["Excel / CSV file", "PDF report", "Scan / photo of a document"]
if not ON_CLOUD:                       # a server-side folder is meaningless on the cloud
    SOURCES.append("Local folder")
source = st.radio("Data source", SOURCES, horizontal=True)
raw = None          # DataFrame in any accepted header style, fed to calc.process_orders()
source_name = source_id = None

if source == "Excel / CSV file":
    uploaded = st.file_uploader("Upload your production orders file", type=["xls", "xlsx", "csv"])
    if uploaded is not None:
        source_name, source_id = uploaded.name, uploaded.file_id
        try:
            raw = calc.load_orders(uploaded)
        except Exception as exc:
            st.error(f"Could not read '{uploaded.name}': {exc}")
            st.stop()
    else:
        st.info(
            "Upload your production file (.xls, .xlsx or .csv) to begin. It must contain the "
            "columns: Order ID, Confirmation Date, Code, Zone, Original Quantity, Original "
            "Length (Hebrew column names are recognised too)."
        )

elif source == "PDF report":
    pdfs = st.file_uploader("Upload digital PDF order reports", type=["pdf"],
                            accept_multiple_files=True)
    if pdfs:
        try:
            frames = [pdf_ingest.load_pdf(f) for f in pdfs]
        except pdf_ingest.PdfUnreadable as exc:
            st.error(str(exc))
            st.stop()
        raw = pd.concat(frames, ignore_index=True)
        incomplete = raw[["original_quantity", "original_length"]].isna().any(axis=1)
        if incomplete.any():
            st.warning(f"{int(incomplete.sum())} row(s) have no readable quantity or length and "
                       "are left out.")
            raw = raw[~incomplete].reset_index(drop=True)
        source_name = ", ".join(f.name for f in pdfs)
        source_id = "|".join(f"{f.name}:{f.size}" for f in pdfs)
        if len(pdfs) == 1 and frames[0].attrs.get("footer_matches"):
            st.success("The row count and quantity total match the totals printed in the report.")
    else:
        st.info("Upload a digital PDF report (text can be selected in it). Scanned PDFs have no "
                "text layer - use the scan option for those.")

elif source == "Local folder":
    folder = st.text_input("Folder path", placeholder="D:/orders")
    if folder:
        try:
            names = calc.list_order_files(folder)
        except OSError as exc:
            st.error(f"Cannot open folder: {exc}")
            st.stop()
        if not names:
            st.warning("No .xls / .xlsx / .csv files in that folder.")
        else:
            chosen = st.selectbox("File", names)
            path = os.path.join(folder, chosen)
            source_name, source_id = chosen, f"{path}:{os.path.getmtime(path)}"
            try:
                raw = calc.load_orders(path)
            except Exception as exc:
                st.error(f"Could not read '{chosen}': {exc}")
                st.stop()
    else:
        st.info("Enter a folder that contains your order files, then pick one.")

else:
    engine = st.radio("Reading engine", [ENGINE_VISION, ENGINE_LOCAL], index=1)
    api_key = ""
    if engine == ENGINE_VISION:
        api_key = st.text_input("Anthropic API key (optional if ANTHROPIC_API_KEY is set)",
                                type="password") or secret("ANTHROPIC_API_KEY", "")
        st.caption("The photo is uploaded to the Anthropic API for reading. Do not use this "
                   "for documents that must not leave your computer.")
    layout = st.selectbox("Column order as it appears on the page", list(ocr.COLUMN_ORDERS),
                          disabled=engine == ENGINE_VISION)
    files = st.file_uploader("Upload scans / photos", type=IMAGE_TYPES, accept_multiple_files=True)
    photo = st.camera_input("...or take a photo")
    images = [f.getvalue() for f in files] + ([photo.getvalue()] if photo else [])
    if not images:
        st.info("Upload or capture a photo of the order schedule (PNG, JPG, JPEG, JFIF). "
                "Extracted rows are shown for review before anything is calculated.")
    else:
        ocr_key = (hash(tuple(images)), layout, engine)
        try:
            with st.spinner("Reading document..."):
                extracted = extract_cached(images, layout, engine, api_key)
        except ocr.OcrUnavailable as exc:
            st.error(str(exc))
            st.stop()
        if extracted.empty:
            st.warning("No order rows were recognised. Try a sharper, straight-on photo, or "
                       "check the column order above.")
            st.stop()
        st.subheader("Review extracted rows")
        flagged = int((extracted["check"] != "").sum())
        st.caption(
            f"{len(extracted)} rows read, {flagged} flagged in the Check column. OCR on a "
            "photo makes mistakes (a wrong digit, a lost decimal point), so compare with the "
            "page and fix cells, delete bad rows or add missing ones. Sharper, larger photos "
            "(2000 px wide or more, straight on) give better results."
        )
        reviewed = st.data_editor(
            extracted, num_rows="dynamic", key=f"ocr-{ocr_key}", width="stretch",
            column_config={"check": st.column_config.TextColumn("Check", disabled=True)})
        complete = reviewed.dropna(subset=["order_id", "confirmation_date",
                                           "original_quantity", "original_length"])
        if len(complete) < len(reviewed):
            st.warning(f"{len(reviewed) - len(complete)} incomplete row(s) are ignored.")
        implausible = (complete["original_length"] > ocr.MAX_PLAUSIBLE_LENGTH) |             (complete["original_length"] < 0) | (complete["original_quantity"] < 0)
        if implausible.any():
            st.warning(f"{int(implausible.sum())} row(s) have an impossible length or quantity "
                       f"(length above {ocr.MAX_PLAUSIBLE_LENGTH} mm, or negative) and are left "
                       "out until you correct them in the table above.")
        usable = complete[~implausible]
        suspect = usable["check"].fillna("") != ""
        if suspect.any():
            st.warning(f"{int(suspect.sum())} of {len(usable)} rows are flagged in the Check "
                       "column. Their numbers may be wrong (a single misread digit changes "
                       "the totals), so correct them above before relying on the results.")
            if st.checkbox("Leave flagged rows out of the calculation"):
                usable = usable[~suspect]
        raw = usable.reset_index(drop=True)
        source_name, source_id = f"{len(images)} scanned image(s)", str(ocr_key)

if raw is None:
    st.stop()

try:
    panels = calc.process_orders(raw)
except Exception as exc:  # surface input problems to the user
    st.error(f"Could not process '{source_name}': {exc}")
    st.stop()

first, last = panels["confirmation_date"].min(), panels["confirmation_date"].max()
st.caption(
    f"Loaded **{source_name}**: {panels['order_id'].nunique()} orders, "
    f"{len(panels)} panel rows, {first:%Y-%m-%d} to {last:%Y-%m-%d}."
)

data_min, data_max = first.date(), last.date()
st.sidebar.subheader("Date range")
start = st.sidebar.date_input("From", data_min, min_value=data_min, max_value=data_max,
                              format="DD/MM/YYYY", key=f"from-{source_id}")
end = st.sidebar.date_input("To", data_max, min_value=data_min, max_value=data_max,
                            format="DD/MM/YYYY", key=f"to-{source_id}")
if start > end:
    st.sidebar.error("'From' must not be after 'To'.")
    st.stop()
all_panels = panels                    # every loaded row, for the order search below
panels = calc.filter_date_range(panels, start, end)
if panels.empty:
    st.warning(f"No orders between {start:%d/%m/%Y} and {end:%d/%m/%Y}.")
    st.stop()
st.caption(f"Showing {panels['order_id'].nunique()} orders from {start:%d/%m/%Y} to {end:%d/%m/%Y}. "
           "Date range and order search are in the sidebar (the arrow at the top left on a phone).")

search = st.sidebar.text_input("Search by Order ID", placeholder="e.g. 841575")

tab_chart, tab_matrix, tab_orders, tab_periods, tab_detail, tab_prices, tab_email = st.tabs(
    ["Consumption chart", "Weekly matrix", "Cost per order", "Quarterly / annual",
     "Processed rows", "Unit prices", "Send by email"])

# The price table lives in its own tab; it is evaluated first because the numbers below use it.
with tab_prices:
    st.caption("Unit price per panel length (after rounding and splitting).")
    price_per_meter = st.number_input("Default price per meter", min_value=0.0, value=0.0)
    price_df = st.data_editor(
        calc.default_price_table(all_panels, price_per_meter),
        disabled=["panel_length"],
        hide_index=True,
        key=f"prices-{source_id}-{price_per_meter}",
    )
prices = dict(zip(price_df["panel_length"], price_df["unit_price"]))
all_panels = calc.apply_prices(all_panels, prices)
panels = calc.apply_prices(panels, prices)

if search.strip():
    hits = calc.find_order(all_panels, search)
    st.subheader(f"Order {search.strip()}")
    if hits.empty:
        st.info(f"No order id containing '{search.strip()}' in the loaded data "
                "(the search ignores the date range).")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Orders matched", hits["order_id"].nunique())
        m2.metric("Panels", int(hits["quantity"].sum()))
        m3.metric("Cost", f"{hits['total_cost'].sum():,.2f}")
        st.dataframe(
            hits[["order_id", "confirmation_date", "code", "zone", "original_quantity",
                  "original_length", "rounded_quantity", "rounded_length", "part",
                  "panel_length", "quantity", "unit_price", "total_cost"]],
            hide_index=True, width="stretch")
        st.caption("Search covers all loaded orders, not just the selected date range.")

with tab_chart:
    chart_data = calc.consumption_by_length(panels)
    st.markdown(
        f'<h4 dir="rtl" style="text-align:right">גרף צריכת סנטפים מתאריך '
        f'{start:%d/%m/%Y} עד {end:%d/%m/%Y}</h4>', unsafe_allow_html=True)
    if chart_data.empty:
        st.info("No panels with a length in this date range.")
    else:
        chart_data["length_label"] = chart_data["panel_length"].map(lambda v: f"{int(v)}")
        hover = alt.selection_point(on="pointerover", empty=False, fields=["panel_length"])
        bars = alt.Chart(chart_data).mark_bar(
            color="#2a78d6", size=18, cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
        ).encode(
            x=alt.X("length_label:N", sort=list(chart_data["length_label"]),
                    title="אורך פאנל (מ״מ)", axis=alt.Axis(labelAngle=-45)),
            y=alt.Y("quantity:Q", title="כמות פאנלים", axis=alt.Axis(grid=True, tickMinStep=1)),
            opacity=alt.condition(hover, alt.value(1.0), alt.value(0.85)),
            tooltip=[alt.Tooltip("panel_length:Q", title="Length (mm)", format=","),
                     alt.Tooltip("quantity:Q", title="Panels", format=",")],
        ).add_params(hover)
        layers = bars
        if len(chart_data) <= 24:                 # direct labels only while they stay legible
            layers = bars + alt.Chart(chart_data).mark_text(dy=-8, fontSize=11).encode(
                x=alt.X("length_label:N", sort=list(chart_data["length_label"])),
                y="quantity:Q", text=alt.Text("quantity:Q", format=",.0f"))
        st.altair_chart(layers.properties(height=360), width="stretch")
        st.caption(f"{int(chart_data['quantity'].sum()):,} panels in total after rounding and "
                   "splitting. Panels of length 0 are not counted.")
        with st.expander("Show as table"):
            st.dataframe(chart_data[["panel_length", "quantity"]], hide_index=True)

    splits = calc.split_orders(panels)
    if splits.empty:
        st.caption("No order in this date range needs a split (nothing reaches 8000 mm).")
    else:
        st.info(f"{len(splits)} order line(s) reach 8000 mm and were split into two panels with "
                f"a {calc.MIN_OVERLAP} mm overlap ({int(splits['quantity'].sum())} pergolas, "
                f"{2 * int(splits['quantity'].sum())} panels).")
        with st.expander(f"Orders at 8000 mm and above - how they were split ({len(splits)})"):
            st.dataframe(
                splits.rename(columns={
                    "order_id": "Order ID", "confirmation_date": "Date",
                    "original_length": "Original length (mm)", "rounded_length": "Rounded (mm)",
                    "quantity": "Quantity", "part_1_mm": "Part 1 (mm)", "part_2_mm": "Part 2 (mm)",
                    "overlap_mm": "Overlap (mm)", "split": "Split"}),
                hide_index=True, width="stretch")
            st.caption("The two parts add up to the rounded length plus the overlap, and neither "
                       f"exceeds {calc.MAX_PANEL_LENGTH} mm. The quantity applies to each part.")

with tab_matrix:
    st.dataframe(calc.weekly_matrix(panels), width="stretch")
    st.metric("Total cost in selected range", f"{panels['total_cost'].sum():,.2f}")

with tab_orders:
    st.dataframe(calc.cost_by_order(panels), hide_index=True, width="stretch")
    st.metric("Total cost in selected range", f"{panels['total_cost'].sum():,.2f}")

with tab_periods:
    period = st.radio("Period", ["month", "quarter", "year"], index=1, horizontal=True)
    st.dataframe(calc.period_summary(panels, period), hide_index=True, width="stretch")

with tab_detail:
    st.dataframe(panels, hide_index=True, width="stretch")
    st.download_button("Download CSV", panels.to_csv(index=False).encode("utf-8-sig"),
                       "processed_orders.csv", "text/csv")


with tab_email:
    try:
        mail_cfg = email_report.load_config({"email": secret_section("email")})
    except email_report.EmailNotConfigured as exc:
        mail_cfg = None
        st.info(str(exc))
        st.caption("Locally: copy .streamlit/secrets.toml.example to .streamlit/secrets.toml and "
                   "fill it in. On Streamlit Cloud: App settings > Secrets.")

    consumption = calc.consumption_by_length(panels)
    consumption_table = consumption.rename(columns={"panel_length": "אורך פאנל (מ״מ)",
                                                    "quantity": "כמות פאנלים"})
    total_panels = int(consumption["quantity"].sum()) if not consumption.empty else 0
    if total_panels:
        consumption_table["אחוז"] = (consumption_table["כמות פאנלים"] / total_panels * 100).round(1)
    split_table = calc.split_orders(panels).rename(columns={
        "order_id": "הזמנה", "confirmation_date": "תאריך", "original_length": "אורך מקורי",
        "rounded_length": "אורך מעוגל", "quantity": "כמות", "part_1_mm": "חלק 1",
        "part_2_mm": "חלק 2", "overlap_mm": "חפיפה", "split": "פיצול"})
    if not split_table.empty:
        split_table["תאריך"] = split_table["תאריך"].dt.strftime("%d/%m/%Y")
    matrix = calc.weekly_matrix(panels)
    matrix_table = matrix[matrix.iloc[:, -1] > 0].reset_index().rename(columns={"length_mm": "אורך (מ״מ)"})
    cost_table = calc.cost_by_order(panels)
    has_prices = bool(panels["total_cost"].sum())

    st.markdown("**What to include**")
    c1, c2 = st.columns(2)
    inc_consumption = c1.checkbox("Consumption by length", value=True)
    inc_splits = c1.checkbox("Orders split at 8000 mm and above", value=True)
    inc_matrix = c2.checkbox("Weekly matrix", value=False)
    inc_cost = c2.checkbox("Cost per order", value=False, disabled=not has_prices,
                           help=None if has_prices else "Enter unit prices in the Unit prices tab.")
    attach_excel = st.checkbox("Attach all tables as an Excel file", value=True)

    period_text = (f"{start:%d/%m/%Y} – {end:%d/%m/%Y} | {source_name} | "
                   f"{datetime.now():%d/%m/%Y %H:%M}")
    subject = st.text_input("Subject", f"דוח צריכת סנטפים — {start:%d/%m/%Y} עד {end:%d/%m/%Y}")
    tables = []
    if inc_consumption:
        tables.append(("צריכה לפי אורך", consumption_table))
    if inc_splits:
        tables.append(("הזמנות שפוצלו (8000 מ״מ ומעלה)", split_table))
    if inc_matrix:
        tables.append(("מטריצה שבועית", matrix_table))
    if inc_cost and has_prices:
        tables.append(("עלות לפי הזמנה", cost_table))
    metrics = [("הזמנות", int(panels["order_id"].nunique())), ("פאנלים", total_panels),
               ("הזמנות מפוצלות", len(split_table))]
    if has_prices:
        metrics.append(("עלות כוללת", round(float(panels["total_cost"].sum()), 2)))
    report_html = email_report.build_report_html("דוח צריכת סנטפים", period_text, metrics, tables)

    with st.expander("Preview"):
        with st.container(height=520, border=True):
            st.html(report_html)                      # sanitised; the values come from uploaded files

    if mail_cfg is not None:
        history = st.session_state.setdefault("email_history", [])
        NEW = "✏️ New address"
        options = history + [NEW]
        choice = st.selectbox("Recipient", options, key="email_choice") if history else NEW
        if choice == NEW:
            recipient_text = st.text_input("Recipient email (several: separate with commas)",
                                           value=mail_cfg.default_recipient, key="email_new")
        else:
            recipient_text = choice
        sent = st.session_state.get("emails_sent", 0)
        if st.button("📧 Send report", disabled=sent >= 10):
            try:
                recipients = email_report.parse_recipients(recipient_text, mail_cfg)
                attachments = []
                if attach_excel:
                    attachments.append(("santaf_report.xlsx", email_report.excel_bytes({
                        "Consumption": consumption_table, "Splits": split_table,
                        "Weekly matrix": matrix_table, "Cost per order": cost_table,
                        "Processed rows": panels.drop(columns=["source_row"], errors="ignore"),
                    })))
                message = email_report.build_message(subject, report_html, mail_cfg.sender,
                                                     recipients, attachments)
                with st.spinner("Sending..."):
                    email_report.send_message(mail_cfg, message)
                st.session_state["emails_sent"] = sent + 1
                for address in recipients:                       # remember for this session only
                    if address in history:
                        history.remove(address)
                    history.insert(0, address)
                st.success(f"Sent to {', '.join(recipients)}")
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(email_report.friendly_error(exc))
        if sent >= 10:
            st.warning("Send limit reached for this session (10). Reload the page to continue.")
