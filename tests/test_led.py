"""Tests for the LED module (led_ingest, led, led_ui and its place in app.py)."""
import io
from pathlib import Path

import pandas as pd
import pytest

import led
import led_ingest

LED_ROWS = [  # id, date, code, zone, qty, length, status, ticks (ordered, received, supplied), note
    ("839561", "01/11/2026", 6, "20", 5, 1500, "OK", (1, 0, 0), ""),
    ("845431", "02/11/2026", 67, "70", 2, 3000, "OK", (1, 1, 0), "see\n839243"),
    ("845992", "02/11/2026", 67, "20", 0, 0, "OK", (0, 0, 0), ""),
    ("817944", "08/11/2026", 6, "50", 1, 12000, "OK", (1, 1, 1), ""),
]


def _led_pdf(path, rows=LED_ROWS, footer=True):
    """A PDF laid out like the real LED report: ruled 7-column table, tick boxes, a notes column."""
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    edges = [250, 330, 370, 410, 450, 480, 540, 590]            # status, length, qty, zone, code, date, id
    top, height = 700, 16
    for i, (oid, date, code, zone, qty, length, status, ticks, note) in enumerate(rows):
        y = top - i * (height + 4)
        cells = [status, str(length), str(qty), zone, str(code), date, oid]
        for k, text in enumerate(cells):
            c.rect(edges[k], y, edges[k + 1] - edges[k], height)
            c.drawString(edges[k] + 3, y + 4, text)
        for x0, ticked in zip((222, 179, 129), ticks):           # ordered, received, supplied boxes
            c.rect(x0, y + 4, 8, 8)
            if ticked:
                c.line(x0 + 1.5, y + 7, x0 + 3.5, y + 5)
                c.line(x0 + 3.5, y + 5, x0 + 6.5, y + 10)
        lines = note.split("\n") if note else []
        tall = len(lines) > 1                                    # a two-line note makes a taller cell
        c.rect(20, y - (8 if tall else 0), 105, height + (16 if tall else 0))
        for k, text in enumerate(lines):
            c.drawString(22, y + (14 if tall else 4) - 12 * k, text)
    if footer:
        c.drawString(540, top - len(rows) * 20 - 10, f"{len(rows)}")
        c.drawString(450, top - len(rows) * 20 - 10, f"{sum(r[4] for r in rows):.2f}")
    c.save()


def test_text_helpers():
    assert led_ingest.fix_visual_hebrew("רוצייב") == "בייצור"
    assert led_ingest.fix_visual_hebrew("לעפמל רבעוה") == "הועבר למפעל"
    assert led_ingest.fix_visual_hebrew("839243 תישאר הנמזה") == "הזמנה ראשית 839243"   # digits stay readable
    assert led_ingest.fix_visual_hebrew("plain 123") == "plain 123" and led_ingest.fix_visual_hebrew(None) == ""
    assert led_ingest.status_key("הועבר למפעל") == "transferred" and led_ingest.status_key("בוטל") == "cancelled"
    assert led_ingest.status_key("משהו חדש") == "other"
    assert led_ingest.main_order_from_note("הלדים על הזמנה ראשית 839413", "845999") == "839413"
    assert led_ingest.main_order_from_note("מספר 845999 בלבד", "845999") == ""


def test_pdf_rows_ticks_and_notes(tmp_path):
    path = tmp_path / "led.pdf"
    _led_pdf(path)
    df = led_ingest.load_led_pdf(path, "led.pdf")
    assert df["order_id"].tolist() == ["839561", "845431", "845992", "817944"]
    assert df["code"].tolist() == [6, 67, 67, 6]
    assert df["length_mm"].tolist() == [1500, 3000, 0, 12000] and df["quantity"].tolist() == [5, 2, 0, 1]
    assert df["zone"].tolist() == ["20", "70", "20", "50"]
    assert df["ordered"].tolist() == [True, True, False, True]
    assert df["received"].tolist() == [False, True, False, True]
    assert df["supplied"].tolist() == [False, False, False, True]
    assert df["note"].tolist() == ["", "see 839243", "", ""] and df["main_order"][1] == "839243"
    assert df.attrs["footer_matches"] is True


def test_multi_file_merge_and_errors(tmp_path):
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    a, b, blank = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "blank.pdf"
    _led_pdf(a)
    _led_pdf(b, rows=LED_ROWS[2:] + [("999999", "09/11/2026", 6, "70", 3, 2000, "OK", (0, 0, 0), "")])
    c = canvas.Canvas(str(blank))
    c.showPage()
    c.save()
    df, report = led_ingest.load_led_pdfs([("a.pdf", a), ("b.pdf", b), ("blank.pdf", blank),
                                           ("junk.pdf", io.BytesIO(b"not a pdf"))])
    assert report.loaded == ["a.pdf", "b.pdf"] and set(report.errors) == {"blank.pdf", "junk.pdf"}
    assert report.duplicates == 2 and len(df) == 5                      # two shared lines counted once
    with pytest.raises(led_ingest.PdfUnreadable):          # the class led_ingest itself raises
        led_ingest.load_led_pdfs([("blank.pdf", blank)])


def test_strip_splitting_rule():
    assert led.split_led_length(3999) == [3999] and led.split_led_length(4000) == [4000]
    assert led.split_led_length(6000) == [4000, 2000] and led.split_led_length(12000) == [4000] * 3
    assert led.split_led_length(4200) == [3200, 1000]                    # remainder topped up to 1000
    assert led.split_led_length(4500) == [3500, 1000] and led.split_led_length(5000) == [4000, 1000]
    assert led.split_led_length(800) == [800] and led.split_led_length(0) == []
    for length in range(1, 20001, 50):
        pieces = led.split_led_length(length)
        assert sum(pieces) == length and max(pieces) <= 4000
        assert length < 1000 or min(pieces) >= 1000, length


def test_transformer_80_percent_rule():
    assert led.usable_watts(60) == 48 and led.usable_watts(500) == 400
    assert led.choose_transformer(48) == 60 and led.choose_transformer(48.1) == 100
    assert led.choose_transformer(80) == 100 and led.choose_transformer(80.1) == 250
    assert led.choose_transformer(200) == 250 and led.choose_transformer(200.1) == 500
    assert led.choose_transformer(400) == 500 and led.choose_transformer(400.1) is None
    assert led.WATTS_PER_METER == 12 and led.LedSettings().watts_per_meter == 12
    assert led.strip_watts(4000, 12) == 48 and led.strip_watts(1000, 12) == 12
    plan = led.plan_transformers([48.0] * 9)                             # 432 W does not fit one 500 W unit
    assert len(plan) == 2 and sum(p["load_w"] for p in plan) == 432
    assert all(p["load_w"] <= led.usable_watts(p["rating"]) for p in plan)
    table = led.transformer_table().set_index("rating_w")                # safety margin per rating
    assert table.loc[60, "safe_load_w"] == 48 and table.loc[60, "max_meters"] == 4.0
    assert table.loc[60, "max_full_strips"] == 1 and table.loc[250, "max_full_strips"] == 4
    assert table.loc[500, "safe_load_w"] == 400


def _order_lines(*lengths):
    """One order's LED lines, one strip per length."""
    return pd.DataFrame({"order_id": "1", "code": 6, "length_mm": list(lengths),
                         "quantity": 1, "status_key": "in_production"})


def test_cob_strip_examples_from_the_specification():
    # a 4 m strip is 48 W net: exactly the 80 % limit of the 60 W transformer
    kit = led.kit_for_order(_order_lines(4000))
    assert kit["watts"] == 48 and kit["meters"] == 4.0 and kit["strips"] == 1
    assert [t["rating"] for t in kit["transformers"]] == [60]
    assert kit["transformers"][0]["load_pct"] == 100.0 and kit["transformers"][0]["headroom_w"] == 0
    # mixed lengths on one order add up to 9.5 m = 114 W on one central 250 W transformer
    kit = led.kit_for_order(_order_lines(4000, 2500, 2000, 1000))
    assert kit["meters"] == 9.5 and kit["watts"] == 114 and kit["strips"] == 4
    assert [t["rating"] for t in kit["transformers"]] == [250]
    assert kit["transformers"][0]["load_w"] == 114 and kit["transformers"][0]["headroom_w"] == 86
    assert kit["transformers"][0]["load_pct"] == 57.0
    # quantities multiply: 3 x 2000 mm = 6 m = 72 W
    many = pd.DataFrame({"order_id": ["1"], "code": [6], "length_mm": [2000], "quantity": [3],
                         "status_key": ["produced"]})
    assert led.kit_for_order(many)["watts"] == 72
    # a very long run is split into 4 m pieces and shared between transformers when it must be
    big = led.kit_for_order(_order_lines(12000, 12000, 12000))          # 36 m = 432 W
    assert big["strips"] == 9 and big["watts"] == 432 and len(big["transformers"]) == 2
    assert all(t["load_w"] <= led.usable_watts(t["rating"]) for t in big["transformers"])


def _table():
    lines = pd.DataFrame([
        ("1", "2026-11-01", 6, "20", 6000, 1, "in_production", True, False, False, "", ""),
        ("1", "2026-11-01", 6, "20", 2000, 3, "in_production", True, False, False, "", ""),
        ("2", "2026-11-02", 67, "70", 3000, 2, "produced", True, True, False, "x 1", "1"),
        ("3", "2026-11-03", 67, "50", 0, 0, "in_warehouse", True, True, True, "", ""),
        ("4", "2026-11-03", 6, "20", 1000, 4, "cancelled", False, False, False, "", ""),
    ], columns=["order_id", "order_date", "code", "zone", "length_mm", "quantity", "status_key",
                "ordered", "received", "supplied", "note", "main_order"])
    lines["order_date"] = pd.to_datetime(lines["order_date"])
    return lines


def test_prepare_kits_and_summary():
    settings = led.LedSettings(zone_regions={"20": "צפון", "70": "מרכז"})
    d = led.prepare(_table(), settings)
    assert d["region"].tolist() == ["צפון", "צפון", "מרכז", led.UNASSIGNED, "צפון"]
    assert d["order_type"].tolist()[0] == "פרגולה מלאה" and "שירות" in d["order_type"].tolist()[2]
    assert d["stage"].tolist() == ["הוזמן", "הוזמן", "נתקבל", "סופק", "טרם הוזמן"]
    assert "יפוצל" in d["issue"][0] and "אין פרטי לד" in d["issue"][3] and "בוטל" in d["issue"][4]
    s = led.summarize(d).set_index("order_type")
    assert s.loc["פרגולה מלאה", "strips"] == 1 + 3 + 4 and s.loc["פרגולה מלאה", "orders"] == 2   # codes never mixed
    assert s.loc["תיקון / שירות / תוספת", "strips"] == 2
    kits = led.kits_table(d, settings).set_index("order_id")
    assert "3" not in kits.index and "4" not in kits.index              # no details / cancelled
    assert kits.loc["1", "pieces"] == "4×2000, 1×4000"                   # 6000 -> 4000 + 2000
    assert kits.loc["1", "strips"] == 5 and kits.loc["1", "meters"] == 12.0
    assert kits.loc["1", "watts"] == 144 and kits.loc["1", "load_pct"] == 72.0     # 12 m at 12 W/m
    assert kits.loc["1", "transformers"] == "250W" and kits.loc["2", "transformers"] == "100W"
    assert kits.loc["2", "watts"] == 72 and kits.loc["2", "load_pct"] == 90.0
    demand = led.inventory_demand(d, settings)
    assert demand["strips"] == {2000: 4, 3000: 2, 4000: 1} and demand["transformers"] == {100: 1, 250: 1}


def test_tracking_store_and_settings_roundtrip(tmp_path):
    store = led.TrackingStore(tmp_path / "t.json")
    lines = _table()
    tracked = store.apply(lines)
    assert not tracked["kit_ordered"].any() and tracked["kit_ref"].eq("").all()
    tracked.loc[0, ["kit_ordered", "kit_ref"]] = [True, "PO-17"]
    assert store.save(led.TrackingStore.collect(tracked)) is True
    again = store.apply(lines)
    assert again.loc[0, "kit_ordered"] and again.loc[0, "kit_ref"] == "PO-17"
    assert not again.loc[1:, "kit_ordered"].any()
    dup = pd.concat([lines.iloc[:1], lines.iloc[:1]], ignore_index=True)                  # identical lines
    assert len(set(led.line_keys(dup))) == 2
    cfg = tmp_path / "s.json"
    custom = led.LedSettings(zone_regions={"20": "דרום"}, transformer_ratings=(60, 120), safety_factor=0.75)
    assert led.save_settings(custom, cfg) is True and led.load_settings(cfg) == custom
    assert led.load_settings(tmp_path / "missing.json") == led.LedSettings()
    old = tmp_path / "old.json"                                            # a file saved by the earlier version
    old.write_text('{"watts_per_meter_low": 25, "watts_per_meter_high": 30, "max_strip_mm": 4000}', encoding="utf-8")
    assert led.load_settings(old).watts_per_meter == 12.0                  # old fields ignored, new default used


def test_app_led_module_end_to_end(tmp_path):
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "led.pdf"
    _led_pdf(path)
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    assert at.segmented_control[0].value == "סנטפים" and len(at.radio) >= 1          # Santaf is the default
    at.segmented_control[0].set_value("לדים").run()
    assert not at.exception, [e.value for e in at.exception]
    at.file_uploader[0].set_value([("led.pdf", path.read_bytes(), "application/pdf")]).run()
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs][-6:] == ["סיכום", "שורות", "לפי הזמנה", "קיטים (הכנה)", "מעקב", "הגדרות"]
    assert any("תואמים" in s.value for s in at.success)                                # footer check
    assert "ניהול פרגולות חכם" in at.title[0].value
    summary = [d.value for d in at.dataframe if "סוג הזמנה" in d.value.columns][0]
    assert set(summary["סוג הזמנה"]) == {"פרגולה מלאה", "תיקון / שירות / תוספת"}        # 6 and 67 apart
    at.segmented_control[0].set_value("סנטפים").run()                                   # back: Santaf unchanged
    assert not at.exception and "Data source" in [r.label for r in at.radio]


def test_a_broken_led_module_never_breaks_santaf(monkeypatch):
    """The LED code is loaded only when selected; if it fails, an error is shown and Santaf still works."""
    import importlib

    from streamlit.testing.v1 import AppTest
    real_reload = importlib.reload

    def reload(module):
        if module.__name__ == "led_ui":
            raise RuntimeError("simulated failure inside the LED module")
        return real_reload(module)

    monkeypatch.setattr(importlib, "reload", reload)
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    assert not at.exception and "Data source" in [r.label for r in at.radio]       # Santaf loads normally
    at.segmented_control[0].set_value("לדים").run()
    assert not at.exception                                                         # no crash ...
    assert any("מודול הלדים נכשל" in e.value and "simulated failure" in e.value for e in at.error)   # ... a message
    at.segmented_control[0].set_value("סנטפים").run()
    assert not at.exception and "Data source" in [r.label for r in at.radio]


def test_hebrew_terminology_uses_shanaim():
    """The LED screens and docs call transformers שנאים / שנאי, never the older "משנה / משנים"."""
    root = Path(__file__).resolve().parents[1]
    for name in ("led_ui.py", "led.py", "README.md"):
        assert "משנ" not in (root / name).read_text(encoding="utf-8"), name
    assert "שנאים" in (root / "led_ui.py").read_text(encoding="utf-8")
