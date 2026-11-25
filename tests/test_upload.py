"""PDF upload stability: the first upload is accepted, shown as read, kept over reruns, never counted twice."""
import os
from pathlib import Path

import pytest

import pdf_ingest
import report_loader

from test_calculator import DATA_ROWS, _make_pdf

APP = Path(__file__).resolve().parents[1] / "app.py"
HEBREW_NAME = "דוח סנטפים 1_12 עד 31_12.pdf"


def _session(tmp_path, monkeypatch, module="סנטפים"):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path / "none"))
    at = AppTest.from_file(str(APP), default_timeout=90).run()
    if module != "סנטפים":
        at.segmented_control[0].set_value(module).run()
    return at


def _pdf(tmp_path, name="r.pdf"):
    path = tmp_path / "src.pdf"
    _make_pdf(path, grid=True, rtl=True)
    return (name, path.read_bytes(), "application/pdf")


def _santaf_upload(at, upload):
    at.radio[0].set_value("PDF report").run()
    at.file_uploader[0].set_value([upload]).run()


def _success(at):
    return any("הקובץ נקלט בהצלחה" in s.value for s in at.success)


# --------------------------------------------------------------------------- the loader itself
def test_loader_reads_once_checks_and_dedupes_identical_content(tmp_path):
    name, data, _ = _pdf(tmp_path)
    loaded = report_loader.load([(name, data), ("copy.pdf", data)])
    assert loaded.load_report.loaded == [name] and loaded.same_content == ["copy.pdf"]
    assert len(loaded.raw_all) == len(DATA_ROWS) and not report_loader.check(loaded)


@pytest.mark.parametrize("data,message", [(b"", "ריק"), (b"hello world", "אינו PDF"), (b"%PDF-1.4 broken", "לא נקרא")])
def test_loader_explains_a_bad_file_in_hebrew(data, message):
    with pytest.raises(pdf_ingest.PdfUnreadable) as caught:
        report_loader.load([("bad.pdf", data)])
    assert message in str(caught.value)


def test_a_good_file_next_to_a_bad_one_is_still_read(tmp_path):
    name, data, _ = _pdf(tmp_path)
    loaded = report_loader.load([("bad.pdf", b"hello"), (name, data)])
    assert loaded.load_report.loaded == [name] and "bad.pdf" in loaded.load_report.errors


# --------------------------------------------------------------------------- the Santaf screen
def test_first_upload_is_accepted_and_the_calculation_is_shown(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch)
    _santaf_upload(at, _pdf(tmp_path))
    assert not at.exception and _success(at) and not at.error
    assert any("panel rows" in c.value for c in at.caption)


def test_a_hebrew_file_name_is_accepted(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch)
    _santaf_upload(at, _pdf(tmp_path, HEBREW_NAME))
    assert not at.exception and _success(at)
    assert at.session_state["santaf_report"].names == [HEBREW_NAME]


def test_the_report_survives_reruns_and_the_same_file_is_not_counted_twice(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch)
    one = _pdf(tmp_path)
    _santaf_upload(at, one)
    caption = [c.value for c in at.caption if "Loaded" in c.value]
    at.run()
    at.file_uploader[0].set_value([one]).run()                                     # the same file again
    assert not at.exception and [c.value for c in at.caption if "Loaded" in c.value] == caption


def test_two_copies_of_one_report_count_once(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch)
    one = _pdf(tmp_path)
    at.radio[0].set_value("PDF report").run()
    at.file_uploader[0].set_value([one, ("copy.pdf", one[1], "application/pdf")]).run()
    assert not at.exception and any("זהה לקובץ שכבר נקלט" in c.value for c in at.caption)
    assert at.session_state["santaf_report"].load_report.loaded == [one[0]]
    assert len(at.session_state["santaf_report"].raw_all) == len(DATA_ROWS)


def test_a_new_file_replaces_the_old_one(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch)
    _santaf_upload(at, _pdf(tmp_path))
    other = tmp_path / "o.pdf"
    _make_pdf(other, grid=False, rtl=True)
    at.file_uploader[0].set_value([("o.pdf", other.read_bytes(), "application/pdf")]).run()
    assert not at.exception and at.session_state["santaf_report"].names == ["o.pdf"]


def test_a_new_session_gets_the_last_report_back_without_uploading(tmp_path, monkeypatch):
    first = _session(tmp_path, monkeypatch)
    _santaf_upload(first, _pdf(tmp_path))
    fresh = _session(tmp_path, monkeypatch)                                        # a lost connection on a phone
    fresh.radio[0].set_value("PDF report").run()
    assert not fresh.exception and any("הדוח האחרון שנקלט" in i.value for i in fresh.info)
    assert any("panel rows" in c.value for c in fresh.caption)


def test_uploading_the_same_file_after_a_restore_still_confirms_the_upload(tmp_path, monkeypatch):
    one = _pdf(tmp_path)
    _santaf_upload(_session(tmp_path, monkeypatch), one)
    fresh = _session(tmp_path, monkeypatch)
    _santaf_upload(fresh, one)
    assert not fresh.exception and _success(fresh) and not any("הדוח האחרון שנקלט" in i.value for i in fresh.info)


@pytest.mark.parametrize("data,message", [(b"", "ריק"), (b"hello world", "אינו PDF"), (b"%PDF-1.4 broken", "לא נקרא")])
def test_bad_uploads_get_a_clear_hebrew_message_and_can_be_retried(tmp_path, monkeypatch, data, message):
    at = _session(tmp_path, monkeypatch)
    _santaf_upload(at, ("bad.pdf", data, "application/pdf"))
    assert not at.exception and any(message in e.value for e in at.error)
    at.file_uploader[0].set_value([_pdf(tmp_path)]).run()                          # try again with a good file
    assert not at.exception and _success(at)


def test_ten_independent_first_uploads_on_both_screens(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path)
    accepted = 0
    for attempt in range(10):
        run_dir = tmp_path / f"attempt{attempt}"
        run_dir.mkdir()
        if attempt % 2 == 0:
            at = _session(run_dir, monkeypatch)
            _santaf_upload(at, pdf)
        else:
            at = _session(run_dir, monkeypatch, module="מלאי סנטפים")
            at.file_uploader(key="stock_orders_upload").set_value([pdf]).run()
        assert not at.exception
        accepted += any("נקלט בהצלחה" in s.value for s in at.success)
        at.run()                                                                   # and after a rerun
        assert not at.exception
    assert accepted == 10


# --------------------------------------------------------------------------- the stock screen
def test_stock_screen_upload_messages_and_empty_files(tmp_path, monkeypatch):
    at = _session(tmp_path, monkeypatch, module="מלאי סנטפים")
    at.file_uploader(key="stock_orders_upload").set_value([("empty.pdf", b"", "application/pdf")]).run()
    assert not at.exception and any("ריק" in e.value for e in at.error)
    at = _session(tmp_path, monkeypatch, module="מלאי סנטפים")
    at.file_uploader(key="stock_orders_upload").set_value([_pdf(tmp_path)]).run()
    assert not at.exception and any("הקובץ r.pdf נקלט בהצלחה" in s.value for s in at.success)
    again = at.file_uploader(key="stock_orders_upload").set_value([_pdf(tmp_path)]).run()
    assert not again.exception


# --------------------------------------------------------------------------- real reports (only where they exist)
REAL = [Path(p) for p in os.environ.get("PERGOLA_REAL_REPORTS", "").split(os.pathsep) if p]


@pytest.mark.skipif(not REAL, reason="set PERGOLA_REAL_REPORTS to real PDF paths to run this")
@pytest.mark.parametrize("path", REAL)
def test_real_reports_are_read_and_confirmed(path):
    loaded = report_loader.load([(path.name, path.read_bytes())])
    assert len(loaded.raw_all) > 0 and not [p for p in report_loader.check(loaded) if "מדפיס" in p]


# --------------------------------------------------------------------------- a row printed as a table of its own
def test_a_report_row_that_is_a_table_of_its_own_is_not_lost():
    rows = pdf_ingest.table_rows([["בייצור", "0", "0", "20", "67", "02/11/2026", "845992"]], page_no=1)
    assert [(r["order_id"], r["code"], r["zone"], r["original_quantity"], r["original_length"]) for r in rows] == [
        ("845992", "67", "20", 0.0, 0.0)]
    assert pdf_ingest.table_rows([["Order", "Date"]]) == []                 # a heading is still not an order row


# --------------------------------------------------------------------------- the printed totals: strict, not per row
def test_the_quantity_total_may_differ_only_by_the_reports_own_rounding():
    ok = pdf_ingest.totals_agree
    assert ok(100, 12.345, 100, 12.34)                # the report prints 12.345 as 12.34 (its own 2-decimal rounding)
    assert ok(60, 8.285, 60, 8.28) and ok(40, 5.0, 40, 5.0) and ok(40, 5.004, 40, 5.0)
    assert not ok(100, 12.35, 100, 12.34)             # 0.01 more is a real difference
    assert not ok(99, 12.34, 100, 12.34)              # a missing row is never hidden, whatever its quantity
    assert not ok(101, 12.34, 100, 12.34)             # nor an extra one
    assert not ok(100, 11.0, 100, 12.34)


def test_check_reports_a_missing_row_even_when_the_quantity_is_tiny(tmp_path):
    name, data, _ = _pdf(tmp_path)
    loaded = report_loader.load([(name, data)])
    rows, quantity = loaded.load_report.read[name]
    loaded.load_report.printed[name] = (rows, quantity)
    assert report_loader.check(loaded) == []
    loaded.load_report.printed[name] = (rows + 1, quantity + 0.0)                 # one more row printed than read
    assert any("מדפיס" in p for p in report_loader.check(loaded))
    loaded.load_report.printed[name] = (rows, quantity + 0.02)                     # a quantity gap of 0.02
    assert any("מדפיס" in p for p in report_loader.check(loaded))
    loaded.load_report.printed[name] = (rows, quantity + 0.005)                    # the rounding the report itself does
    assert report_loader.check(loaded) == []


def test_footer_matches_is_strict_when_the_totals_line_is_there():
    import pandas as pd
    frame = pd.DataFrame({"original_quantity": [1.5, 2.5]})
    assert pdf_ingest.footer_matches(frame, ["... 4.00 2 : ..."]) is True
    assert pdf_ingest.footer_matches(frame, ["... 4.00 3 : ..."]) is False       # the count differs
    assert pdf_ingest.footer_matches(frame, ["... 4.02 2 : ..."]) is False       # the quantity differs by more than rounding
