import io
import json
import re
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import calculator as calc


def test_round_quantity():
    assert calc.round_quantity(2.1) == 3
    assert calc.round_quantity(2) == 2


@pytest.mark.parametrize("raw,expected", [(1, 500), (4200, 4500), (4500, 4500), (7001, 7500)])
def test_round_length(raw, expected):
    assert calc.round_length(raw) == expected


@pytest.mark.parametrize(
    "length,expected",
    [(7500, [7500]), (8000, [5000, 4000]), (10000, [6000, 5000]), (15000, [8000, 8000])],
)
def test_split_length(length, expected):
    assert calc.split_length(length) == expected


def test_split_constraints():
    for length in range(8000, 15001, 500):
        a, b = calc.split_length(length)
        assert max(a, b) <= 8000
        assert a + b - length >= 1000


def test_split_too_long():
    with pytest.raises(ValueError):
        calc.split_length(15500)


def _orders():
    return pd.DataFrame(
        {
            "Order ID": ["A", "B"],
            "Confirmation Date": ["2025-03-02", "2025-03-10"],
            "Code": ["X", "X"],
            "Zone": ["1", "1"],
            "Original Quantity": [1.2, 3],
            "Original Length": [8000, 4200],
        }
    )


def test_process_and_matrix():
    panels = calc.process_orders(_orders())
    assert len(panels) == 3  # A split in two, B single
    assert panels[panels.order_id == "A"]["quantity"].tolist() == [2, 2]
    m = calc.weekly_matrix(panels)
    assert m.index[0] == 1000 and m.index[-1] == 5000
    assert m.loc[5000, "Week 1"] == 2
    assert m.loc[4500, "Week 2"] == 3
    assert m["Month total"].sum() == 2 + 2 + 3


def test_prices_and_summary():
    panels = calc.apply_prices(calc.process_orders(_orders()), {5000: 10, 4000: 8, 4500: 5})
    assert panels["total_cost"].sum() == 2 * 10 + 2 * 8 + 3 * 5
    q = calc.period_summary(panels, "quarter")
    assert q["quantity"].sum() == 7


def _file_frame():
    return pd.DataFrame(
        [["Production report", None, None, None, None, None],
         [None] * 6,
         ["Order ID", "Confirmation Date", "Code", "Zone", "Original Quantity", "Original Length"],
         [501, pd.Timestamp("2026-09-03"), "SF", "A", 2, 8200],
         [502, pd.Timestamp("2026-09-20"), "SF", "B", 1.1, 3100]]
    )


def _write_xls(path):
    xlwt = pytest.importorskip("xlwt")
    book = xlwt.Workbook()
    sheet = book.add_sheet("Sheet1")
    style = xlwt.easyxf(num_format_str="YYYY-MM-DD")
    for r, row in enumerate(_file_frame().itertuples(index=False)):
        for c, v in enumerate(row):
            if pd.isna(v):
                continue
            if isinstance(v, pd.Timestamp):
                sheet.write(r, c, v.to_pydatetime(), style)
            else:
                sheet.write(r, c, v)
    book.save(str(path))


@pytest.mark.parametrize("ext", ["xlsx", "xls"])
def test_load_excel_with_title_rows(tmp_path, ext):
    path = tmp_path / f"orders.{ext}"
    if ext == "xls":
        _write_xls(path)
    else:
        _file_frame().to_excel(path, header=False, index=False)
    panels = calc.process_orders(calc.load_orders(path))
    assert set(panels["year"]) == {2026} and set(panels["month"]) == {9}
    assert panels["order_id"].tolist() == [501, 501, 502]
    assert panels["panel_length"].tolist() == [5000, 4500, 3500]


def test_load_xls_with_wrong_extension_and_csv(tmp_path):
    path = tmp_path / "orders.xls"          # actually an xlsx
    _file_frame().to_excel(path.with_suffix(".xlsx"), header=False, index=False)
    path.write_bytes(path.with_suffix(".xlsx").read_bytes())
    assert len(calc.load_orders(path)) == 2
    csv = tmp_path / "o.csv"
    csv.write_text("Order ID;Confirmation Date;Code;Zone;Original Quantity;Original Length\n"
                   "1;31/03/2026;X;A;1;4000\n", encoding="utf-8")
    panels = calc.process_orders(calc.load_orders(csv))
    assert panels["month"].tolist() == [3]


def test_hebrew_and_messy_headers(tmp_path):
    rows = [["דוח"] + [None] * 5,
            ["מס' הזמנה", "תאריך אישור", "קוד", "אזור", "כמות", "אורך"],
            [7, pd.Timestamp("2026-11-04"), "SF", "צפון", 2, 8000]]
    path = tmp_path / "he.xlsx"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)
    panels = calc.process_orders(calc.load_orders(path))
    assert panels["panel_length"].tolist() == [5000, 4000]
    assert panels["zone"].iloc[0] == "צפון"


def test_case_insensitive_and_optional_code_zone(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text(" ORDER_ID ,Confirmation  DATE,QUANTITY,Length\n1,2026-11-04,1,4000\n",
                   encoding="utf-8")
    panels = calc.process_orders(calc.load_orders(csv))
    assert panels["quantity"].tolist() == [1] and panels["code"].iloc[0] == ""


def test_unknown_headers_error_lists_found_text(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="foo"):
        calc.load_orders(csv)


def test_date_range_filter_and_matrix():
    df = pd.DataFrame({
        "Order ID": [1, 2, 3, 4],
        "Confirmation Date": ["2026-10-30", "2026-11-08", "2026-11-15", "2026-11-16"],
        "Code": "X", "Zone": "A", "Original Quantity": [1, 2, 3, 4],
        "Original Length": [4000, 4000, 8000, 4000],
    })
    panels = calc.process_orders(df)
    sub = calc.filter_date_range(panels, "2026-11-08", "2026-11-15")
    assert sorted(sub["order_id"].unique()) == [2, 3]          # inclusive both ends
    m = calc.weekly_matrix(sub)                                 # single month
    assert list(m.columns)[-1] == "Month total" and m["Month total"].sum() == 2 + 3 + 3
    wide = calc.weekly_matrix(calc.filter_date_range(panels, "2026-10-01", "2026-11-30"))
    assert "2026-10 W5" in wide.columns and wide["Range total"].sum() == 1 + 2 + 3 + 3 + 4
    assert calc.period_summary(sub, "month")["quantity"].sum() == 8
    with pytest.raises(ValueError):
        calc.filter_date_range(panels, "2026-11-15", "2026-11-08")


# ---- OCR ingestion (parsing only; the Tesseract engine itself is not needed) ----
import ocr_ingest as ocr  # noqa: E402

LTR, RTL = ocr.LTR_ORDER, ocr.RTL_ORDER


def test_ocr_parse_helpers():
    assert ocr.parse_date("8/11/26") == "2026-11-08"
    assert ocr.parse_date("15.11.2026") == "2026-11-15"
    assert ocr.parse_date("31/13/2026") is None
    assert ocr.parse_number("2O") == 20 and ocr.parse_number("8OOO") == 8000
    assert ocr.parse_number("1,5") == 1.5 and ocr.parse_number("abc") is None


def test_list_order_files(tmp_path):
    (tmp_path / "a.csv").write_text("x"); (tmp_path / "b.txt").write_text("x")
    (tmp_path / "~$c.xlsx").write_text("x")
    assert calc.list_order_files(tmp_path) == ["a.csv"]


def test_ocr_reports_missing_engine(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    monkeypatch.setattr(ocr.os.path, "exists", lambda _: False)
    with pytest.raises(ocr.OcrUnavailable, match="winget"):
        ocr.ocr_table(b"x", None)


def _table_words(n_rows=8, tilt=0.04, pitch=70.0, drop=()):
    """Synthetic OCR words for an RTL table: length, qty, zone, code, date, id (left to right)."""
    xs = {"len": 300, "qty": 500, "zone": 700, "code": 880, "date": 1060, "id": 1290}
    words = []
    for k in range(n_rows):
        for name, x in xs.items():
            if (k, name) in drop:
                continue
            y = 500 + k * pitch + tilt * (1060 - x)           # rows tilt towards the left
            text = {"len": str(1000 + 100 * k), "qty": f"{k + 1}.5", "zone": "70", "code": "6",
                    "date": f"{23 + k // 4}/11/2026", "id": str(840000 + k)}[name]
            words.append((x, y, 30, text, 90.0))
    words.append((1060, 300, 30, "01/11/2026", 90.0))            # title date above the table
    words.append((200, 120, 30, "7", 40.0))                      # stray noise
    return words


def test_rows_from_words_handles_tilt_and_missing_cells():
    words = _table_words(drop={(2, "qty"), (3, "id"), (4, "date")})
    df = ocr.rows_from_words(words, RTL)
    assert len(df) == 8
    assert df["order_id"].tolist()[:3] == ["840000", "840001", "840002"] and df["order_id"][3] == ""
    assert df["original_length"].tolist() == [1000 + 100 * k for k in range(8)]
    assert df["original_quantity"][1] == 2.5 and pd.isna(df["original_quantity"][2])
    assert "quantity missing" in df["check"][2] and "order id missing" in df["check"][3]
    assert df["code"].eq("6").all() and df["zone"].eq("70").all()


def test_rows_from_words_rereads_missing_cells():
    calls = []

    def reader(x, y, w, h, field):
        calls.append(field)
        return ("2.5", 95.0) if field == "original_quantity" else None

    df = ocr.rows_from_words(_table_words(drop={(2, "qty")}), RTL, reader)
    assert df["original_quantity"][2] == 2.5 and "original_quantity" in calls


def test_rows_from_words_flags_suspect_values():
    words = _table_words()
    words = [(x, y, h, "51" if t == "1.5" else t, c) for x, y, h, t, c in words]
    df = ocr.rows_from_words(words, RTL)
    assert "decimal point" in df["check"][0]
    assert ocr.rows_from_words(words[:2], RTL).empty              # fewer than two dates -> nothing


def test_ocr_rows_feed_engine():
    df = ocr.rows_from_words(_table_words(), RTL)
    panels = calc.process_orders(df)
    assert len(panels) == 8 and panels["panel_length"].max() <= 8000


def test_date_sequence_and_year_checks():
    rows = [{"confirmation_date": d, "check": ""} for d in
            ["2026-11-01", "2026-11-02", "2021-11-03", "2026-11-20", "2026-11-04", "2026-11-05"]]
    ocr._reconcile_dates(rows)
    assert "year corrected" in rows[2]["check"]
    assert "out of sequence" in rows[3]["check"] and rows[4]["check"] == ""


# ---- AI vision engine (fake client, no network) ----
import json  # noqa: E402
import types  # noqa: E402

import vision_ingest as vision  # noqa: E402


def _fake_client(payload, stop_reason="end_turn"):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        block = types.SimpleNamespace(type="text", text=json.dumps(payload))
        return types.SimpleNamespace(content=[block], stop_reason=stop_reason)

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create)), calls


def _png_bytes():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_vision_extract_builds_checked_rows():
    rows = [
        {"order_id": "841575", "confirmation_date": "2026-11-23", "code": "6", "zone": "70",
         "original_quantity": 3.6, "original_length": 1390, "unsure": ""},
        {"order_id": "84157", "confirmation_date": "2026-11-23", "code": "6", "zone": "70",
         "original_quantity": None, "original_length": 40000, "unsure": "length blurred"},
        {"order_id": "843360", "confirmation_date": "2026-11-24", "code": "6", "zone": "20",
         "original_quantity": 2.835, "original_length": 8000, "unsure": ""},
    ]
    client, calls = _fake_client({"rows": rows})
    df = vision.extract_orders([_png_bytes()], client=client)
    assert df["original_quantity"][0] == 3.6 and df["check"][0] == ""
    assert "digits, expected 6" in df["check"][1] and "length blurred" in df["check"][1]
    assert "quantity missing" in df["check"][1] and "implausible" in df["check"][1]
    request = calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    ok = df[df["check"] == ""]
    panels = calc.process_orders(ok)
    assert len(panels) == 1 + 2                      # 1390 single; 8000 splits in two


def test_vision_refusal_and_missing_credentials(monkeypatch):
    client, _ = _fake_client({"rows": []}, stop_reason="refusal")
    with pytest.raises(ocr.OcrUnavailable, match="declined"):
        vision.extract_orders([_png_bytes()], client=client)
    import anthropic
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    try:
        vision.extract_orders([_png_bytes()], api_key="")
    except ocr.OcrUnavailable:
        pass                                         # no credentials -> friendly error
    except anthropic.AnthropicError:
        pytest.fail("raw SDK error leaked to the UI")


# ---- PDF ingestion (PDFs generated on the fly with reportlab) ----
import pdf_ingest  # noqa: E402

DATA_ROWS = [("841575", "23/11/2026", "6", "70", "3.6", "1390"),
             ("841575", "23/11/2026", "6", "70", "2.835", "2945"),
             ("843360", "24/11/2026", "6", "20", "12.4", "8000"),
             ("843372", "29/11/2026", "6", "20", "0.92", "945")]


def _make_pdf(path, grid=True, rtl=True, extra_columns=2, footer=True):
    pytest.importorskip("reportlab")
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    def order(row):                                  # visual left-to-right column order
        cells = list(reversed(row)) if rtl else list(row)
        return ["status", "x"][:extra_columns] + cells if rtl else cells

    rows = [order(r) for r in DATA_ROWS]
    header = order(("id", "date", "code", "zone", "qty", "len"))
    totals = f"{sum(float(r[4]) for r in DATA_ROWS):.2f} {len(DATA_ROWS)}"
    if grid:
        doc = SimpleDocTemplate(str(path), pagesize=A4)
        table = Table([header] + rows)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        doc.build([table])
        if footer:                                    # totals line appended on a second pass
            pass
        return totals
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 780
    for row in [header] + rows:
        for i, text in enumerate(row):
            c.drawString(40 + i * 70, y, text)
        y -= 24
    if footer:
        c.drawString(40, y - 20, totals)
    c.save()
    return totals


@pytest.mark.parametrize("rtl", [True, False])
def test_pdf_ruled_table(tmp_path, rtl):
    path = tmp_path / "r.pdf"
    _make_pdf(path, grid=True, rtl=rtl)
    df = pdf_ingest.load_pdf(path)
    assert df["order_id"].tolist() == ["841575", "841575", "843360", "843372"]
    assert df["original_quantity"].tolist() == [3.6, 2.835, 12.4, 0.92]   # decimals exact
    assert df["original_length"].tolist() == [1390, 2945, 8000, 945]
    assert df["confirmation_date"].iloc[2] == "2026-11-24" and df["zone"].tolist()[:2] == ["70", "70"]
    panels = calc.process_orders(df)
    assert panels["panel_length"].tolist() == [1500, 3000, 5000, 4000, 1000]


def test_pdf_without_grid_uses_word_positions(tmp_path):
    path = tmp_path / "w.pdf"
    totals = _make_pdf(path, grid=False, rtl=True)
    df = pdf_ingest.load_pdf(path)
    assert len(df) == 4 and df["original_quantity"].tolist()[:2] == [3.6, 2.835]
    assert df["original_length"].tolist() == [1390, 2945, 8000, 945]     # lengths must not be lost
    assert df.attrs["footer_matches"] is True and totals.split()[1] == "4"


def test_pdf_errors(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank))
    c.showPage()
    c.save()
    with pytest.raises(pdf_ingest.PdfUnreadable, match="scan"):
        pdf_ingest.load_pdf(blank)
    text = tmp_path / "text.pdf"
    c = canvas.Canvas(str(text))
    c.drawString(100, 700, "hello world")
    c.save()
    with pytest.raises(pdf_ingest.PdfUnreadable, match="No order table"):
        pdf_ingest.load_pdf(text)


def test_pdf_cid_headers_are_ignored():
    table = [["(cid:676)(cid:696)", "(cid:695)"] + ["(cid:1)"] * 5,
             ["x", "1390", "3.6", "70", "6", "23/11/2026", "841575"],
             ["x", "2945", "2.835", "70", "6", "23/11/2026", "841575"]]
    rows = pdf_ingest.table_rows(table)
    assert [r["original_length"] for r in rows] == [1390, 2945]


def test_consumption_by_length_and_find_order():
    df = pd.DataFrame({
        "Order ID": ["841575", "841575", "843360", "843372"],
        "Confirmation Date": ["2026-11-23", "2026-11-23", "2026-11-24", "2026-11-25"],
        "Code": "6", "Zone": "70", "Original Quantity": [3.6, 2.1, 1.2, 4],
        "Original Length": [1390, 8000, 0, 8001],
    })
    panels = calc.process_orders(df)
    # core rules: quantity ceil, length up to 500 mm, >= 8000 mm split in two with 1000 mm overlap
    assert panels[panels.order_id == "841575"]["rounded_quantity"].tolist() == [4, 3, 3]
    assert panels[panels.order_id == "841575"]["rounded_length"].tolist() == [1500, 8000, 8000]
    assert panels[panels.order_id == "843372"]["panel_length"].tolist() == [5000, 4500]   # 8001 -> 8500
    chart = calc.consumption_by_length(panels)
    assert 0 not in chart["panel_length"].tolist()                    # zero-length rows excluded
    assert chart.set_index("panel_length").loc[5000, "quantity"] == 3 + 4     # two split orders
    assert chart["panel_length"].is_monotonic_increasing
    assert chart["quantity"].sum() == panels[panels.panel_length > 0]["quantity"].sum()
    hits = calc.find_order(panels, " 8415 ")
    assert set(hits["order_id"]) == {"841575"} and len(hits) == 3
    assert calc.find_order(panels, "").empty and calc.find_order(panels, "zzz").empty


def test_split_orders_summary():
    df = pd.DataFrame({
        "Order ID": ["A", "A", "B", "C"],
        "Confirmation Date": ["2026-11-02", "2026-11-02", "2026-11-03", "2026-11-04"],
        "Code": "6", "Zone": "70", "Original Quantity": [2, 2, 1.2, 3],
        "Original Length": [8000, 8000, 10000, 4000],        # identical lines must stay separate
    })
    s = calc.split_orders(calc.process_orders(df))
    assert len(s) == 3 and set(s["order_id"]) == {"A", "B"}
    assert s["split"].tolist() == ["5000 + 4000 mm (overlap 1000 mm)"] * 2 + \
        ["6000 + 5000 mm (overlap 1000 mm)"]
    assert (s["overlap_mm"] == 1000).all() and s["quantity"].tolist() == [2, 2, 2]
    assert calc.split_orders(calc.process_orders(df.iloc[3:])).empty


def test_app_survives_a_stale_calculator_module(tmp_path):
    """A running Streamlit server keeps old modules cached; the app must reload them."""
    pytest.importorskip("streamlit")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    (tmp_path / "orders.csv").write_text(
        "Order ID,Confirmation Date,Code,Zone,Original Quantity,Original Length\n"
        "1001,2026-11-02,6,70,2,8000\n1002,2026-11-03,6,20,1.2,4200\n", encoding="utf-8")
    for name in ("consumption_by_length", "find_order", "split_orders"):
        delattr(calc, name)                                    # simulate the stale module
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60).run()
    at.radio[0].set_value("Local folder").run()
    at.text_input[0].set_value(str(tmp_path)).run()
    assert not at.exception, [e.value for e in at.exception]
    assert hasattr(calc, "consumption_by_length") and len(at.tabs) == 7


# ---- Email reports ----
import smtplib  # noqa: E402

import email_report  # noqa: E402

SECRET = "app-password-123"
CFG = email_report.EmailConfig(sender="me@example.com", password=SECRET)


def test_email_config_and_recipients():
    cfg = email_report.load_config({"email": {"sender": "a@b.co", "password": "abcd efgh ijkl mnop",
                                              "allowed_recipients": ["Boss@Example.com"]}}, {})
    assert cfg.password == "abcdefghijklmnop" and "abcd" not in repr(cfg)       # never in repr
    assert email_report.parse_recipients("boss@example.com; ", cfg) == ["boss@example.com"]
    with pytest.raises(ValueError, match="allowed"):
        email_report.parse_recipients("other@example.com", cfg)
    with pytest.raises(ValueError, match="valid"):
        email_report.parse_recipients("not-an-address")
    with pytest.raises(ValueError, match="At most"):
        email_report.parse_recipients(",".join(f"u{i}@x.co" for i in range(6)))
    with pytest.raises(email_report.EmailNotConfigured):
        email_report.load_config({}, {})
    assert email_report.load_config({}, {"EMAIL_SENDER": "e@x.co", "EMAIL_PASSWORD": "p"}).sender == "e@x.co"


def test_email_message_html_escaping_and_attachment():
    df = pd.DataFrame({"הזמנה": ["<script>alert(1)</script>"], "כמות": [3.5]})
    body = email_report.build_report_html("דוח", "תקופה", [("הזמנות", 1)], [("טבלה", df)])
    assert "<script>" not in body and "&lt;script&gt;" in body and 'dir="rtl"' in body
    xlsx = email_report.excel_bytes({"Consumption": df})
    msg = email_report.build_message("נושא", body, "me@example.com", ["x@y.co"], [("r.xlsx", xlsx)])
    kinds = [p.get_content_type() for p in msg.walk()]
    assert "text/html" in kinds and msg.get_payload()[-1].get_filename() == "r.xlsx"
    assert pd.read_excel(io.BytesIO(xlsx)).iloc[0, 0] == "<script>alert(1)</script>"
    with pytest.raises(ValueError):
        email_report.build_message("bad\r\nBcc: x@y.co", body, "me@example.com", ["x@y.co"])
    with pytest.raises(ValueError, match="10 MB"):
        email_report.build_message("s", body, "me@example.com", ["x@y.co"],
                                   [("big.bin", b"0" * (11 * 1024 * 1024))])


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        _FakeSMTP.sent.append((self.host, self.port, self.login_args, msg))


def test_send_message_uses_starttls_and_login(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent.clear()
    msg = email_report.build_message("s", "<p>x</p>", CFG.sender, ["x@y.co"])
    email_report.send_message(CFG, msg)
    host, port, login, sent = _FakeSMTP.sent[0]
    assert (host, port, login) == ("smtp.gmail.com", 587, ("me@example.com", SECRET))


def test_friendly_error_never_leaks_the_password():
    err = smtplib.SMTPAuthenticationError(535, f"bad credentials {SECRET}".encode())
    assert SECRET not in email_report.friendly_error(err)
    assert "app password" in email_report.friendly_error(err)


def test_no_hardcoded_credentials_in_sources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for path in [*root.glob("*.py"), root / "requirements.txt"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"sk-ant-[A-Za-z0-9_-]{20,}", text), path.name
        assert not re.search(r"SENDER_PASSWORD\s*=\s*[\"'][^\"']+[\"']", text), path.name


def test_email_tab_sends_report_through_smtp(monkeypatch, tmp_path):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent.clear()
    (tmp_path / "orders.csv").write_text(
        "Order ID,Confirmation Date,Code,Zone,Original Quantity,Original Length\n"
        "1001,2026-11-02,6,70,2,8000\n1002,2026-11-03,6,20,1.2,4200\n", encoding="utf-8")
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60)
    at.secrets["email"] = {"sender": "me@example.com", "password": SECRET}
    at.run()
    at.radio[0].set_value("Local folder").run()
    at.text_input[0].set_value(str(tmp_path)).run()
    assert not at.exception, [e.value for e in at.exception]
    recipient = [t for t in at.text_input if t.label.startswith("Recipient")][0]
    recipient.set_value("boss@example.com").run()
    [b for b in at.button if "Send report" in b.label][0].click().run()
    assert not at.exception and any("Sent to boss@example.com" in s.value for s in at.success)
    _, _, login, msg = _FakeSMTP.sent[0]
    assert msg["To"] == "boss@example.com" and login[1] == SECRET
    assert any(p.get_filename() == "santaf_report.xlsx" for p in msg.iter_attachments())
    html_part = msg.get_body(("html",)).get_content()
    assert "5000" in html_part and "הזמנות שפוצלו" in html_part       # consumption + split table


def test_app_password_gate(monkeypatch, tmp_path):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60)
    at.secrets["app_password"] = "letmein"
    at.run()
    assert [t.label for t in at.text_input] == ["Password"] and len(at.radio) == 0
    at.text_input[0].set_value("wrong").run()
    assert at.error and len(at.radio) == 0
    at.text_input[0].set_value("letmein").run()
    assert len(at.radio) >= 1


# ---- Several PDFs at once ----
def _frame(rows):
    return pd.DataFrame(rows, columns=["order_id", "confirmation_date", "code", "zone",
                                       "original_quantity", "original_length"])


def test_merge_reports_counts_overlap_once_but_keeps_real_repeats():
    a = _frame([("1", "2026-11-01", "6", "70", 2.0, 1000),
                ("1", "2026-11-01", "6", "70", 2.0, 1000),      # genuine repeat inside one file
                ("2", "2026-11-02", "6", "20", 1.0, 2000)])
    b = _frame([("2", "2026-11-02", "6", "20", 1.0, 2000),      # also in file a (overlap)
                ("3", "2026-11-03", "6", "20", 4.0, 3000)])
    merged, dropped = pdf_ingest.merge_reports([a, b])
    assert dropped == 1 and len(merged) == 4
    assert merged["order_id"].tolist().count("1") == 2           # repeat kept
    assert pdf_ingest.merge_reports([a, _frame([("9", "2026-12-01", "6", "20", 1.0, 500)])])[1] == 0


def test_load_many_skips_bad_files_by_name(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    good, blank = tmp_path / "good.pdf", tmp_path / "blank.pdf"
    _make_pdf(good, grid=True, rtl=True)
    c = canvas.Canvas(str(blank))
    c.showPage()
    c.save()
    df, report = pdf_ingest.load_many([("good.pdf", good), ("copy.pdf", good), ("blank.pdf", blank)])
    assert report.loaded == ["good.pdf", "copy.pdf"] and list(report.errors) == ["blank.pdf"]
    assert "scan" in report.errors["blank.pdf"]
    assert len(df) == 4 and report.duplicates == 4               # the identical copy counted once
    with pytest.raises(pdf_ingest.PdfUnreadable, match="blank.pdf"):
        pdf_ingest.load_many([("blank.pdf", blank)])


def test_app_uploads_several_pdfs(tmp_path):
    pytest.importorskip("reportlab")
    from pathlib import Path

    from streamlit.testing.v1 import AppTest
    rows_a, rows_b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _make_pdf(rows_a, grid=True, rtl=True)
    _make_pdf(rows_b, grid=False, rtl=True)                       # same lines, different layout
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.radio[0].set_value("PDF report").run()
    uploader = at.file_uploader[0]
    assert uploader.accept_multiple_files
    uploader.set_value([("a.pdf", rows_a.read_bytes(), "application/pdf"),
                        ("b.pdf", rows_b.read_bytes(), "application/pdf"),
                        ("scan.pdf", b"%PDF-1.4 not really a pdf", "application/pdf")]).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Skipped scan.pdf" in e.value for e in at.error)
    assert any("appear in more than one" in w.value for w in at.warning)
    assert any("2 PDF reports" in c.value for c in at.caption)
    assert len(at.tabs) == 7


# ---- PWA / installable app ----
import pwa  # noqa: E402


def _manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ["static/manifest.json", "docs/manifest.webmanifest"])
def test_manifest_is_installable(path):
    root = Path(__file__).resolve().parents[1]
    m = _manifest(root / path)
    assert m["name"] == "מחשבון סנטפים" and m["short_name"] and m["display"] == "standalone"
    assert m["lang"] == "he" and m["dir"] == "rtl" and m["start_url"] and m["theme_color"].startswith("#")
    from PIL import Image
    sizes = set()
    for icon in m["icons"]:
        file = (root / path).parent / icon["src"]
        assert file.exists(), icon["src"]                              # every icon is really there
        with Image.open(file) as img:
            assert f"{img.width}x{img.height}" == icon["sizes"] and img.format == "PNG"
        sizes.add((icon["sizes"], icon["purpose"]))
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    assert any(i["src"] == "icon.png" for i in m["icons"]) or path.startswith("docs")


def test_static_serving_is_enabled_and_icon_matches():
    root = Path(__file__).resolve().parents[1]
    config = (root / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert re.search(r"^enableStaticServing\s*=\s*true", config, re.M)
    assert (root / "static" / "icon.png").read_bytes() == (root / "icon.png").read_bytes()


def test_pwa_head_script_links_manifest_and_retouches():
    script = pwa.head_script()
    assert "app/static/manifest.json" in script and "apple-touch-icon" in script
    assert "theme-color" in script and "setTimeout" in script          # re-attach after load
    assert "data-pwa" in script                                        # idempotent across reruns
    html = (Path(__file__).resolve().parents[1] / "docs" / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html and "/~/+/" in html and "serviceWorker" in html
    assert "embed=true" not in html.split("<script>", 1)[1].split("</script>")[0].replace("//", "")


def test_app_renders_the_pwa_helper_without_errors():
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=60).run()
    assert not at.exception
