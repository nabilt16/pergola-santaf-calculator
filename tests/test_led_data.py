"""Tests for the warehouse list, the transformer allocation, the zone mapping and the local archive."""
import io
import zipfile
from pathlib import Path

import pandas as pd
import pytest

import led
import led_archive
import led_ingest
import warehouse

from test_led import LED_ROWS, _led_pdf


# --------------------------------------------------------------------------- regions
def test_zone_mapping_from_the_specification():
    assert led.DEFAULT_ZONE_REGIONS == {"20": "צפון", "50": "דרום", "55": "דרום", "70": "מרכז", "80": "דרום"}
    assert led.DEFAULT_ZONE_AREAS == {"20": "חיפה", "50": "שדרות", "55": "באר שבע", "70": "מרכז", "80": "אילת"}
    lines = pd.DataFrame({"order_id": list("abcdef"), "order_date": pd.Timestamp("2026-11-01"),
                          "code": 6, "zone": ["20", "50", "55", "70", "80", "99"], "length_mm": 1000,
                          "quantity": 1, "status_key": "produced", "ordered": False, "received": False,
                          "supplied": False})
    d = led.prepare(lines, led.LedSettings())
    assert d["region"].tolist() == ["צפון", "דרום", "דרום", "מרכז", "דרום", led.UNASSIGNED]   # unknown zone
    assert d["area"].tolist() == ["חיפה", "שדרות", "באר שבע", "מרכז", "אילת", ""]
    custom = led.LedSettings(zone_regions={"50": "מרכז", "55": led.UNASSIGNED}, zone_areas={"50": "אשקלון"})
    d = led.prepare(lines, custom)
    assert d["region"].tolist()[1:3] == ["מרכז", led.UNASSIGNED] and d["area"][1] == "אשקלון"
    assert d["region"][0] == "צפון"                                                    # the rest keeps the default


# --------------------------------------------------------------------------- allocation
def test_allocation_notation_and_policies():
    assert led.parse_allocation("1*100W+1*150W") == [(1, 100.0), (1, 150.0)]
    assert led.parse_allocation("3*150W") == [(3, 150.0)] and led.parse_allocation("2x100W") == [(2, 100.0)]
    assert led.parse_allocation("???") == [] and led.parse_allocation(None) == []
    assert led.format_allocation([150, 100]) == "1*100W+1*150W" and led.format_allocation([150] * 3) == "3*150W"
    assert led.format_allocation([60, None]) == "1*60W+מעל הטווח" and led.format_allocation([]) == ""
    one_strip = led.plan_transformers([12.0] * 3)                                     # 36 W
    assert led.format_allocation([t["rating"] for t in one_strip]) == "1*60W"
    loads = [24.0] * 6                                                                # 144 W
    fewest = led.plan_transformers(loads, policy="fewest_units")
    cheapest = led.plan_transformers(loads, policy="least_total_rating")
    assert led.format_allocation([t["rating"] for t in fewest]) == "1*200W"           # one central unit
    assert led.format_allocation([t["rating"] for t in cheapest]) == "3*60W"          # smaller total rating
    for plan in (fewest, cheapest):
        assert all(t["load_w"] <= led.usable_watts(t["rating"]) for t in plan)
        assert sum(t["load_w"] for t in plan) == 144


# --------------------------------------------------------------------------- warehouse list
def _xlsx(path, rows, header=True):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append([None, None, None])
    if header:
        sheet.append([None, "מס' הזמנה", "שנאי "])               # like the real file: empty first column
    for order, allocation in rows:
        sheet.append([None, order, allocation])
    book.save(path)


def test_warehouse_list_is_read_by_content_and_partial_lists_merge(tmp_path):
    first = tmp_path / "wh1.xlsx"
    _xlsx(first, [(837907, "1*100W"), (831931, "1*150W"), (836272, "1*100W+1*150W"), (837907, "1*100W"),
                  (825148, "3*150W"), (840403, "1*200W"), (999001, "???"), ("not an order", "1*60W"), (None, None)])
    wh = warehouse.load_warehouse(first, "wh1.xlsx")
    assert len(wh) == 7 and wh["order_id"].tolist().count("837907") == 2                 # a repeat inside a file stays
    row = wh.set_index("order_id")
    assert row.loc["836272", "allocation"] == "1*100W+1*150W" and row.loc["836272", "total_w"] == 250
    assert row.loc["836272", "unit_count"] == 2 and row.loc["825148", "units"] == "150,150,150"
    assert row.loc["825148", "total_w"] == 450 and row.loc["840403", "total_w"] == 200
    assert not row.loc["999001", "parse_ok"] and row.loc["999001", "total_w"] == 0       # unclear, kept, flagged
    assert wh["parse_ok"].sum() == 6 and wh["source_file"].eq("wh1.xlsx").all()

    second = tmp_path / "wh2.xlsx"                                                        # tomorrow's longer list
    _xlsx(second, [(837907, "1*100W"), (831931, "1*150W"), (837907, "1*100W"), (841484, "1*150W"),
                   (842238, "1*200W")], header=False)
    merged = warehouse.merge_warehouse([wh, warehouse.load_warehouse(second, "wh2.xlsx")])
    assert len(merged) == 7 + 2                                                           # only the new rows are added
    assert merged["order_id"].tolist().count("837907") == 2 and {"841484", "842238"} <= set(merged["order_id"])
    stock = warehouse.stock_summary(merged).set_index("rating_w")
    assert stock.loc[150.0, "units"] == 1 + 3 + 1 + 1 + 0 and stock.loc[200.0, "units"] == 2
    csv = tmp_path / "wh3.csv"
    csv.write_text("הזמנה;שנאי\n111111;1*60W\n222222;2*100W\n", encoding="utf-8")
    assert warehouse.load_warehouse(csv, "wh3.csv")["total_w"].tolist() == [60, 200]
    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"not a spreadsheet")
    with pytest.raises(warehouse.WarehouseUnreadable):
        warehouse.load_warehouse(junk, "junk.xlsx")
    empty = tmp_path / "empty.xlsx"
    _xlsx(empty, [("no digits", "x")])
    with pytest.raises(warehouse.WarehouseUnreadable):
        warehouse.load_warehouse(empty, "empty.xlsx")


def _report_lines(settings=None):
    rows = [
        ("838781", 6, "20", 1000, 3, "produced", ""),                    # 3 m = 36 W, listed with 100 W
        ("845431", 67, "70", 3000, 2, "produced", "839243"),             # repair of the listed main order 839243
        ("845431", 67, "70", 3000, 2, "produced", "839243"),
        ("826169", 6, "20", 2000, 1, "in_warehouse", ""),                # the report says warehouse, list does not
        ("700001", 6, "70", 1250, 4, "in_production", ""),               # 5 m = 60 W, listed with only 60 W
        ("700002", 6, "70", 2000, 1, "cancelled", ""),
    ]
    df = pd.DataFrame(rows, columns=["order_id", "code", "zone", "length_mm", "quantity", "status_key", "main_order"])
    df["order_date"] = pd.Timestamp("2026-11-05")
    df["ordered"], df["received"], df["supplied"] = True, False, False
    return led.prepare(df, settings or led.LedSettings())


def _warehouse_rows(*pairs):
    frames = []
    for order, allocation in pairs:
        units, text = warehouse.normalize_allocation(allocation)
        frames.append({"order_id": order, "allocation": text, "alloc_raw": allocation,
                       "units": ",".join(f"{u:g}" for u in units), "total_w": float(sum(units)),
                       "unit_count": len(units), "note": "", "parse_ok": bool(units), "source_file": "t", "row": 1})
    return pd.DataFrame(frames, columns=warehouse.COLUMNS)


def test_warehouse_matches_reports_directly_and_through_the_main_order():
    wh = _warehouse_rows(("838781", "1*100W"), ("839243", "1*150W"), ("700001", "1*60W"), ("111111", "1*60W"))
    lines = warehouse.annotate_lines(_report_lines(), wh)
    state = lines.groupby("order_id")["warehouse_state"].first().to_dict()
    assert state["838781"] == warehouse.IN_LIST and state["845431"] == warehouse.IN_LIST_VIA_MAIN
    assert state["826169"] == warehouse.REPORT_ONLY and state["700002"] == ""
    assert lines[lines.order_id == "845431"]["wh_alloc"].eq("1*150W").all()
    assert lines[lines.order_id == "838781"]["wh_alloc_w"].iloc[0] == 100
    cross = warehouse.cross_reference(lines, wh, led.LedSettings()).set_index("order_id")
    assert cross.loc["838781", "verdict"] == "הוקצה יותר מהמומלץ" and cross.loc["838781", "recommended"] == "1*60W"
    assert cross.loc["839243", "verdict"] == "קיים דרך הזמנת תיקון" and cross.loc["839243", "linked_meters"] == 12
    assert cross.loc["839243", "linked_orders"] == "845431"
    assert cross.loc["700001", "required_w"] == 60 and cross.loc["700001", "verdict"] == "הוקצה פחות מהנדרש"
    assert cross.loc["111111", "verdict"] == "לא בדוחות שנטענו"                       # partial list: not an error
    no_list = warehouse.annotate_lines(_report_lines(), None)                          # works without a list yet
    assert set(no_list["warehouse_state"]) == {"", warehouse.REPORT_ONLY}


def test_calibration_compares_the_recommendation_with_the_real_allocation():
    wh = _warehouse_rows(("838781", "1*100W"))
    lines = warehouse.annotate_lines(_report_lines(), wh)
    result = warehouse.calibration(lines, wh, led.LedSettings())
    assert set(result["compared"]) == {1} and len(result) == 12                       # 6 factors x 2 policies
    at = lambda factor: result[(result["safety_factor"] == factor)]["matches"].tolist()   # noqa: E731
    assert at(0.5) == [1, 1] and at(0.8) == [0, 0]                    # 100 W for 36 W only fits a 50 % margin
    assert warehouse.calibration(lines, wh.iloc[0:0], led.LedSettings())["compared"].sum() == 0


# --------------------------------------------------------------------------- archive
def _report_frame(tmp_path, name="a.pdf", rows=LED_ROWS):
    path = tmp_path / name
    _led_pdf(path, rows=rows)
    return led_ingest.load_led_pdf(path, name)


def test_archive_saves_once_and_restores_exactly(tmp_path):
    archive = led_archive.Archive(tmp_path / "archive")
    assert archive.is_empty() and archive.entries("reports") == []
    frame = _report_frame(tmp_path)
    entry, is_new = archive.save("reports", "a.pdf", frame, led_archive.file_hash(b"a"), batch="b1")
    again, repeated = archive.save("reports", "a.pdf", frame, led_archive.file_hash(b"a"), batch="b9")
    assert is_new and not repeated and again["id"] == entry["id"] and len(archive.entries("reports")) == 1
    assert entry["rows"] == 4 and entry["period_start"] == "2026-11-01" and entry["period_end"] == "2026-11-08"
    loaded = archive.load("reports", entry["id"])
    pd.testing.assert_frame_equal(loaded, frame, check_dtype=False)
    assert loaded["order_date"].dtype.kind == "M" and loaded["ordered"].dtype == bool
    assert loaded["length_mm"].tolist() == [1500, 3000, 0, 12000] and loaded["order_id"][0] == "839561"
    later_frame = _report_frame(tmp_path, "b.pdf", rows=LED_ROWS[:2])
    later, _ = archive.save("reports", "b.pdf", later_frame, led_archive.file_hash(b"b"), batch="b2")
    assert [e["name"] for e in archive.entries("reports")] == ["b.pdf", "a.pdf"]          # newest first
    assert [e["id"] for e in archive.latest_batch("reports")] == [later["id"]]
    wh = _warehouse_rows(("838781", "1*100W"), ("839243", "1*150W"))
    saved, _ = archive.save("warehouse", "wh.xlsx", wh, led_archive.file_hash(b"w"))
    back = archive.load("warehouse", saved["id"])
    assert back["total_w"].tolist() == [100.0, 150.0] and back["parse_ok"].all() and back["unit_count"].tolist() == [1, 1]
    pd.testing.assert_frame_equal(back, wh, check_dtype=False)
    assert not archive.is_empty()


def test_archive_backup_restore_and_safety(tmp_path):
    source = led_archive.Archive(tmp_path / "src" / "archive")
    source.save("reports", "a.pdf", _report_frame(tmp_path), led_archive.file_hash(b"a"))
    source.save("warehouse", "wh.xlsx", _warehouse_rows(("838781", "1*100W")), led_archive.file_hash(b"w"))
    (tmp_path / "src" / "led_tracking.json").write_text("{}", encoding="utf-8")
    data = source.export_zip()
    target = led_archive.Archive(tmp_path / "dst" / "archive")
    assert target.import_zip(data) == {"added": 2, "files": 1}
    assert len(target.entries("reports")) == 1 and len(target.entries("warehouse")) == 1
    assert target.load("reports", target.entries("reports")[0]["id"])["order_id"].tolist()[0] == "839561"
    assert target.import_zip(data) == {"added": 0, "files": 0}                             # nothing duplicated
    for bad in ({"archive/../evil.csv": b"x"}, {"evil.py": b"x"}, {"archive/reports/a b.csv": b"x"}):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, payload in bad.items():
                archive.writestr(name, payload)
        with pytest.raises(led_archive.ArchiveError):
            target.import_zip(buffer.getvalue())
    with pytest.raises(led_archive.ArchiveError):
        target.import_zip(b"not a zip")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed")
    with pytest.raises(led_archive.ArchiveError):                                          # read-only host
        led_archive.Archive(blocker / "archive").save("reports", "a.pdf", _report_frame(tmp_path), "h")


# --------------------------------------------------------------------------- through the app
def test_warehouse_tab_stores_and_merges_lists(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    report = tmp_path / "led.pdf"
    _led_pdf(report)
    first, second = tmp_path / "wh1.xlsx", tmp_path / "wh2.xlsx"
    _xlsx(first, [(839561, "1*100W"), (831931, "1*150W")])
    _xlsx(second, [(839561, "1*100W"), (831931, "1*150W"), (825148, "3*150W")])           # the longer list
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.segmented_control[0].set_value("לדים").run()
    at.file_uploader[0].set_value([("led.pdf", report.read_bytes(), "application/pdf")]).run()
    assert not at.exception, [e.value for e in at.exception]
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    upload = at.get("file_uploader")[-1] if False else [u for u in at.file_uploader if u.key == "led_wh_upload"][0]
    upload.set_value([("wh1.xlsx", first.read_bytes(), mime)]).run()
    assert not at.exception, [e.value for e in at.exception]
    archive = led_archive.Archive(tmp_path / "data" / "archive")
    assert len(archive.entries("warehouse")) == 1
    labels = [m.label for m in at.metric]
    assert "שורות ברשימה" in labels and at.metric[labels.index("שורות ברשימה")].value == "2"
    upload = [u for u in at.file_uploader if u.key == "led_wh_upload"][0]
    upload.set_value([("wh1.xlsx", first.read_bytes(), mime), ("wh2.xlsx", second.read_bytes(), mime)]).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(archive.entries("warehouse")) == 2                                         # same file not saved twice
    labels = [m.label for m in at.metric]
    assert at.metric[labels.index("שורות ברשימה")].value == "3"                           # merged, not summed
    assert at.metric[labels.index("נמצאו בדוחות")].value == "1"                           # order 839561 is in both


# --------------------------------------------------------------------------- saved settings
def test_settings_saved_before_the_specification_do_not_override_it(tmp_path):
    old = tmp_path / "old.json"                  # what the earlier version wrote: no version, 8 sizes, 50 = מרכז
    old.write_text('{"zone_regions": {"20": "צפון", "50": "מרכז"}, "transformer_ratings": [60, 100, 150, 200, 250,'
                   ' 300, 400, 500], "safety_factor": 0.7, "max_strip_mm": 4500, "watts_per_meter": 12.0}',
                   encoding="utf-8")
    loaded = led.load_settings(old)
    assert loaded.transformer_ratings == (60, 100, 150, 200, 250, 500)           # back to the warehouse sizes
    assert led.zone_regions(loaded)["50"] == "דרום" and loaded.zone_regions == {}
    assert loaded.safety_factor == 0.7 and loaded.max_strip_mm == 4500             # the user's own numbers stay
    new = tmp_path / "new.json"                  # saved by this version: the user's later choices are kept
    custom = led.LedSettings(zone_regions={"50": "מרכז"}, transformer_ratings=(60, 100), zone_areas={"50": "אשקלון"})
    assert led.save_settings(custom, new) and '"version": 2' in new.read_text(encoding="utf-8")
    again = led.load_settings(new)
    assert again == custom and led.zone_regions(again)["50"] == "מרכז" and again.transformer_ratings == (60, 100)


# --------------------------------------------------------------------------- tracking table types
def test_tracking_columns_have_the_types_the_editor_needs(tmp_path):
    """st.data_editor refuses a checkbox column that is not boolean, e.g. the empty table of a search with no hit."""
    from test_led import _table
    store = led.TrackingStore(tmp_path / "t.json")
    for lines in (_table(), _table().iloc[0:0]):
        tracked = store.apply(lines)
        assert tracked["kit_ordered"].dtype == bool
        assert tracked["kit_ref"].map(type).eq(str).all() and tracked["tracking_note"].map(type).eq(str).all()
        assert tracked["line_key"].map(type).eq(str).all()
    messy = {"1|6|6000|1|0": {"kit_ordered": None, "kit_ref": None, "tracking_note": float("nan")}}
    tracked = store.apply(_table(), messy)                                    # missing values in a saved file
    assert tracked["kit_ordered"].dtype == bool and not tracked["kit_ordered"].any()
    assert tracked["kit_ref"].eq("").all() and tracked["tracking_note"].eq("").all()


# --------------------------------------------------------------------------- order search
def _seed_archive(tmp_path):
    """An archive with an older report, the latest report (several stages) and a partial warehouse list."""
    archive = led_archive.Archive(tmp_path / "data" / "archive")
    older = tmp_path / "older.pdf"
    _led_pdf(older, rows=[("555555", "05/10/2026", 6, "70", 2, 2000, "OK", (0, 0, 0), "")])
    archive.save("reports", "older.pdf", led_ingest.load_led_pdf(older, "older.pdf"), led_archive.file_hash(b"o"))
    rows = [("839561", "01/11/2026", 6, "20", 5, 1500, "OK", (1, 0, 0), ""),       # stage: הוזמן
            ("845431", "02/11/2026", 67, "70", 2, 3000, "OK", (1, 1, 0), "see\n839243"),   # stage: נתקבל
            ("817944", "08/11/2026", 6, "50", 1, 12000, "OK", (1, 1, 1), "")]       # stage: סופק
    pdf = tmp_path / "report.pdf"
    _led_pdf(pdf, rows=rows)
    archive.save("reports", "report.pdf", led_ingest.load_led_pdf(pdf, "report.pdf"), led_archive.file_hash(b"r"))
    wh = _warehouse_rows(("837907", "1*100W"), ("837907", "1*100W"), ("839243", "1*150W"))
    archive.save("warehouse", "wh.xlsx", wh, led_archive.file_hash(b"w"))
    return archive


def _led_session(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.segmented_control[0].set_value("לדים").run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _shown_orders(at):
    tables = [d.value for d in at.dataframe if "הזמנה" in d.value.columns and "קוד" in d.value.columns]
    return set().union(*[set(t["הזמנה"]) for t in tables]) if tables else set()


def test_order_search_ignores_the_other_filters(tmp_path, monkeypatch):
    _seed_archive(tmp_path)
    at = _led_session(tmp_path, monkeypatch)
    at.multiselect(key="led_stages").set_value(["סופק"]).run()                       # only supplied lines
    at.multiselect(key="led_wh_states").set_value([]).run()                          # and no warehouse state at all
    assert _shown_orders(at) == set()                                                # the filters hide everything
    at.text_input(key="led_search").set_value("839561").run()                        # stage הוזמן, other filters on
    assert not at.exception, [e.value for e in at.exception]
    assert "839561" in _shown_orders(at)
    assert any("בלי קשר לשאר המסננים" in c.value for c in at.caption)
    at.text_input(key="led_search").set_value("839243").run()                        # a main order: its repair order
    assert "845431" in _shown_orders(at)
    at.text_input(key="led_search").set_value("").run()
    assert _shown_orders(at) == set()                                                # clearing it restores the filters


def test_search_for_an_order_that_is_not_loaded_explains_where_it_is(tmp_path, monkeypatch):
    _seed_archive(tmp_path)
    at = _led_session(tmp_path, monkeypatch)
    at.text_input(key="led_search").set_value("837907").run()                         # only in the warehouse list
    assert not at.exception, [e.value for e in at.exception]                          # (this used to crash the editor)
    text = " ".join(w.value for w in at.warning) + " ".join(i.value for i in at.info)
    shown = text.replace(chr(96), "").replace(chr(0x2066), "").replace(chr(0x2069), "")   # code + isolate
    assert "837907" in shown and "1*100W" in shown                                    # the allocation from the list
    at.text_input(key="led_search").set_value("555555").run()                         # only in an unselected report
    assert not at.exception, [e.value for e in at.exception]
    text = " ".join(w.value for w in at.warning) + " ".join(i.value for i in at.info)
    assert "older.pdf" in text and "שמורים" in text
    at.text_input(key="led_search").set_value("000000").run()                         # nowhere
    assert not at.exception and any("000000" in w.value for w in at.warning)


def test_allocation_text_survives_markdown():
    """"1*100W" in a markdown text is shown as code inside a left-to-right isolate: no emphasis eating the *,
    no shuffling of its digits inside Hebrew text."""
    import led_ui
    tick, lri, pdi = chr(96), chr(0x2066), chr(0x2069)
    assert led_ui._md("**כללים** 1*100W+1*150W או 3*150W") == \
        f"**כללים** {lri}{tick}1*100W+1*150W{tick}{pdi} או {lri}{tick}3*150W{tick}{pdi}"
    assert led_ui._md("**bold**") == "**bold**" and led_ui._md("no stars") == "no stars"
