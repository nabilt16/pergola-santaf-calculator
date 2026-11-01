"""The app must never come up as a white screen: a missing, empty or damaged file becomes a message, and the rest works."""
import json
import runpy
import sys
from pathlib import Path

import pandas as pd
import pytest

import led
import led_archive
import stock_plan as sp
import stock_store as ss
import warehouse as wh

from test_stock_store import _orders

APP = Path(__file__).resolve().parents[1] / "app.py"
GARBAGE = ["", "   ", "not json", "[1, 2]", '"text"', "null", "{", '{"quantities": [1]}', '{"a": {"b"']


def _run(tmp_path, monkeypatch, module=None, strict=False):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path / "none"))
    monkeypatch.setenv("PERGOLA_STRICT", "1" if strict else "0")
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    if module:
        at.segmented_control[0].set_value(module).run()
    return at


def _messages(at):
    return [e.value for e in at.error]


# --------------------------------------------------------------------------- the entry point
def test_a_crash_while_starting_shows_a_message_not_a_white_screen(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("calculator.py is missing")
    monkeypatch.setattr(runpy, "run_path", boom)
    at = _run(tmp_path, monkeypatch)
    assert not at.exception
    assert any("calculator.py is missing" in m for m in _messages(at))


def test_in_strict_mode_the_same_crash_surfaces(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("x")
    monkeypatch.setattr(runpy, "run_path", boom)
    assert _run(tmp_path, monkeypatch, strict=True).exception


def test_the_app_starts_clean_with_no_data_at_all(tmp_path, monkeypatch):
    at = _run(tmp_path, monkeypatch)
    assert not at.exception and not _messages(at)
    for module in ("לדים", "מלאי סנטפים"):
        at = _run(tmp_path, monkeypatch, module)
        assert not at.exception and not _messages(at), module


def test_a_missing_helper_module_is_reported_in_the_module_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "stock_ui", None)               # importing it now raises ImportError
    at = _run(tmp_path, monkeypatch, "מלאי סנטפים")
    assert not at.exception and any("מודול המלאי נכשל בטעינה" in m for m in _messages(at))
    at.segmented_control[0].set_value("סנטפים").run()                # the Santaf calculator is untouched
    assert not at.exception and any("Upload a digital PDF" in i.value for i in at.info)


# --------------------------------------------------------------------------- damaged saved files: stock module
@pytest.mark.parametrize("text", GARBAGE)
def test_stock_module_survives_every_damaged_saved_file(tmp_path, monkeypatch, text):
    folder = tmp_path / "data" / "stock"
    folder.mkdir(parents=True)
    for name in ss.NAMES:
        (folder / name).write_text(text, encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "מלאי סנטפים")
    assert not at.exception, [e.value for e in at.exception]
    assert at.subheader                                               # the module is drawn


def test_damaged_stock_file_is_reported_and_orders_stay_available(tmp_path, monkeypatch):
    store = ss.StockStore(tmp_path / "data" / "stock")
    store.save_orders(_orders([("100010", "2026-10-29", 1, 1500)]), "r.xlsx")
    (store.root / "stock.json").write_text("{broken", encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "מלאי סנטפים")
    assert not at.exception and any("קובץ המלאי השמור פגום" in m for m in _messages(at))
    assert store.load_orders() is not None                             # nothing was deleted


def test_orders_without_the_expected_columns_are_reported(tmp_path, monkeypatch):
    store = ss.StockStore(tmp_path / "data" / "stock")
    store.save_stock(sp.Stock({1500: 5}, pd.Timestamp("2026-10-26")), "s.xlsx")
    (store.root / "orders.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "מלאי סנטפים")
    assert not at.exception and any("קובץ ההזמנות השמור פגום" in m for m in _messages(at))


def test_orders_that_cannot_be_processed_are_reported(tmp_path, monkeypatch):
    store = ss.StockStore(tmp_path / "data" / "stock")
    store.save_stock(sp.Stock({1500: 5}, pd.Timestamp("2026-10-26")), "s.xlsx")
    (store.root / "orders.csv").write_text("order_id,confirmation_date\n100010,not a date\n", encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "מלאי סנטפים")
    assert not at.exception


# --------------------------------------------------------------------------- the guards themselves
def _tabs_script():
    import streamlit as st
    from safety import attempt, guard
    tabs = st.tabs(["a", "b"])
    with tabs[0], guard("first tab"):
        raise ValueError("boom")
    with tabs[1], guard("second tab"):
        st.write("still here")
    value = attempt("value", lambda: 1 / 0, default=7)
    st.write(f"default {value}")


def test_a_failing_section_is_isolated_and_the_next_one_draws(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_STRICT", "0")
    at = AppTest.from_function(_tabs_script).run()
    assert not at.exception and any("first tab" in e.value for e in at.error)
    assert any("still here" in m.value for m in at.markdown) and any("default 7" in m.value for m in at.markdown)


def test_guards_let_errors_through_in_strict_mode(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_STRICT", "1")
    assert AppTest.from_function(_tabs_script).run().exception


def test_guards_do_not_swallow_stop_and_rerun(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_STRICT", "0")

    def script():
        import streamlit as st
        from safety import guard
        with guard("section"):
            st.write("before")
            st.stop()
        st.write("after")
    at = AppTest.from_function(script).run()
    assert not at.exception and not at.error and not any("after" in m.value for m in at.markdown)


# --------------------------------------------------------------------------- the stores themselves
@pytest.mark.parametrize("text", GARBAGE)
def test_stock_store_reads_never_raise(tmp_path, text):
    for name in ss.NAMES:
        (tmp_path / name).write_text(text, encoding="utf-8")
    store = ss.StockStore(tmp_path)
    assert store.load_stock() is None and store.load_orders() is None
    assert store.issued() == {} and store.orders_files() == []
    store.last_run()
    settings = store.load_settings()
    assert settings["kerf_mm"] == 0 and settings["zero_sizes"] == [1000]
    pd.Timestamp(settings["start"])


def test_stock_store_drops_only_the_damaged_entries(tmp_path):
    (tmp_path / "issued.json").write_text(json.dumps({
        "100001": {"issued_on": "2026-10-30", "note": "ok"}, "100002": {"issued_on": "garbage"},
        "100003": "text", "100004": {"note": "no date"}}), encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps({"kerf_mm": "x", "buffer_days": None, "zero_sizes": "abc",
                                                        "start": "nope", "skip_stuck": 0}), encoding="utf-8")
    store = ss.StockStore(tmp_path)
    assert list(store.issued()) == ["100001"]
    s = store.load_settings()
    assert (s["kerf_mm"], s["buffer_days"], s["zero_sizes"], s["skip_stuck"]) == (0, 14, [1000], False)
    assert s["start"] == ss.DEFAULT_SETTINGS["start"]


def test_stock_store_on_an_unwritable_location_reports_instead_of_crashing(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = ss.StockStore(blocker / "inside")                          # a folder under a file can never be created
    assert store.load_stock() is None and store.load_orders() is None and store.issued() == {}
    with pytest.raises(ss.StoreError):
        store.save_stock(sp.Stock({1500: 1}, None), "s.xlsx")


# --------------------------------------------------------------------------- LED module
@pytest.mark.parametrize("text", GARBAGE)
def test_led_stores_survive_damaged_files(tmp_path, text):
    for name in ("led_settings.json", "led_tracking.json", "led_arrivals.json"):
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert led.load_settings(tmp_path / "led_settings.json") == led.LedSettings()
    tracking = led.TrackingStore(tmp_path / "led_tracking.json").load()
    assert isinstance(tracking, dict)
    lines = pd.DataFrame({"order_id": ["1"], "code": [1], "length_mm": [1000], "quantity": [1]})
    led.TrackingStore(tmp_path / "led_tracking.json").apply(lines, {**tracking, "x": "bad"})      # never raises
    assert wh.ArrivalStore(tmp_path / "led_arrivals.json").arrived() == {}
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "index.json").write_text(text, encoding="utf-8")
    archive = led_archive.Archive(archive_dir)
    assert archive.entries("reports") == [] and archive.is_empty()


def test_led_archive_skips_damaged_index_entries(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps({
        "reports": [{"id": "a", "name": "n", "saved_at": "2026-10-30T10:00:00", "sha256": "h", "batch": "b"},
                    {"id": "broken"}, "text", 5],
        "warehouse": "not a list"}), encoding="utf-8")
    archive = led_archive.Archive(tmp_path)
    assert [e["id"] for e in archive.entries("reports")] == ["a"] and archive.entries("warehouse") == []


@pytest.mark.parametrize("text", GARBAGE)
def test_led_module_survives_damaged_saved_files(tmp_path, monkeypatch, text):
    data = tmp_path / "data"
    (data / "archive" / "reports").mkdir(parents=True)
    for path in (data / "led_settings.json", data / "led_tracking.json", data / "led_arrivals.json",
                 data / "archive" / "index.json"):
        path.write_text(text, encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "לדים")
    assert not at.exception, [e.value for e in at.exception]
    assert at.subheader


def test_led_module_reports_a_saved_report_whose_file_is_missing(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "archive"
    archive.mkdir(parents=True)
    (archive / "index.json").write_text(json.dumps({"reports": [
        {"id": "gone", "name": "report.pdf", "saved_at": "2026-10-30T10:00:00", "sha256": "h", "batch": "b",
         "period_start": "2026-10-29", "period_end": "2026-10-30"}]}), encoding="utf-8")
    at = _run(tmp_path, monkeypatch, "לדים")
    assert not at.exception and any("לא נקרא" in m for m in _messages(at))
