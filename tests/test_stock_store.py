"""Persistence and screen of the Santaf stock plan module."""
from pathlib import Path

import pandas as pd
import pytest

import stock_plan as sp
import stock_store as ss

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _orders(rows):
    return pd.DataFrame(rows, columns=["order_id", "confirmation_date", "original_quantity", "original_length"])


def _stock_xlsx(path, rows):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append([None, "מלאי", None, "נכון לתאריך 26/10/26"])
    sheet.append([None, "אורך", "כמות", None])
    for length, qty in rows:
        sheet.append([None, length, qty, None])
    book.save(path)
    return path


def _orders_xlsx(path, rows):
    _orders(rows).to_excel(path, index=False)
    return path


# --------------------------------------------------------------------------- the store
def test_stock_orders_and_settings_survive_a_restart(tmp_path):
    store = ss.StockStore(tmp_path)
    assert store.load_stock() is None and store.load_orders() is None and store.load_settings() == ss.DEFAULT_SETTINGS
    store.save_stock(sp.Stock({1500: 60, 7000: 60}, pd.Timestamp("2026-10-26"), [1000]), "stock.xlsx")
    store.save_orders(_orders([("A", "2026-10-29", 2, 1500)]), "r1.xlsx")
    store.save_settings({**ss.DEFAULT_SETTINGS, "kerf_mm": 3, "skip_stuck": False})
    again = ss.StockStore(tmp_path)                                        # a new object = a new run of the app
    stock, info = again.load_stock()
    assert stock.quantities == {1500: 60, 7000: 60} and stock.as_of == pd.Timestamp("2026-10-26")
    assert stock.zero_added == [1000] and info["source"] == "stock.xlsx"
    assert again.load_orders()["order_id"].tolist() == ["A"]
    settings = again.load_settings()
    assert settings["kerf_mm"] == 3 and settings["skip_stuck"] is False and settings["start"] == "2026-10-29"


def test_new_reports_are_merged_without_double_counting(tmp_path):
    store = ss.StockStore(tmp_path)
    assert store.save_orders(_orders([("A", "2026-10-29", 2, 1500), ("B", "2026-10-30", 1, 2000)]), "oct.xlsx") == 2
    added = store.save_orders(_orders([("B", "2026-10-30", 1, 2000), ("C", "2026-11-01", 3, 3000)]), "overlap.xlsx")
    assert added == 1 and store.load_orders()["order_id"].tolist() == ["A", "B", "C"]
    store.save_orders(_orders([("D", "2026-11-02", 1, 2000), ("D", "2026-11-02", 1, 2000)]), "twice.xlsx")
    assert (store.load_orders()["order_id"] == "D").sum() == 2             # a genuinely repeated line is kept
    assert [f["name"] for f in store.orders_files()] == ["oct.xlsx", "overlap.xlsx", "twice.xlsx"]


def test_bad_settings_file_and_backup_round_trip(tmp_path):
    store = ss.StockStore(tmp_path / "a")
    store.save_stock(sp.Stock({1500: 5}, pd.Timestamp("2026-10-26")), "s.xlsx")
    store.save_orders(_orders([("A", "2026-10-29", 1, 1500)]), "r.xlsx")
    store.save_run({"cuts": 0}, pd.DataFrame({"x": [1]}))
    (store.root / "settings.json").write_text('{"start": "garbage"}', encoding="utf-8")
    assert store.load_settings()["start"] == "2026-10-29"                  # falls back to the default start
    backup = store.export_zip()
    other = ss.StockStore(tmp_path / "b")
    assert {"stock.json", "orders.csv", "last_run.json"} <= set(other.import_zip(backup))
    assert other.load_stock()[0].quantities == {1500: 5} and other.load_orders()["order_id"].tolist() == ["A"]
    with pytest.raises(ss.StoreError):
        other.import_zip(b"not a zip")


# --------------------------------------------------------------------------- the screen
def _app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    import os
    if "PERGOLA_INBOX_DIRS" not in os.environ:                  # never read the real Downloads folder in a test
        monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path / "no-files-here"))
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.segmented_control[0].set_value("מלאי סנטפים").run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_screen_saves_uploads_and_does_not_ask_again(tmp_path, monkeypatch):
    stock_file = _stock_xlsx(tmp_path / "stock.xlsx", [(1500, 5), (7000, 20)])
    orders_file = _orders_xlsx(tmp_path / "orders.xlsx", [
        ("X1", "2026-10-28", 50, 1500),                                     # already left: not counted
        ("A", "2026-10-29", 3, 1000),                                       # 1000 mm is not in stock: cut from 7000
        ("B", "2026-10-30", 4, 1500)])
    at = _app(tmp_path, monkeypatch)
    assert "העלה קובץ מלאי" in " ".join(i.value for i in at.info)
    at.file_uploader(key="stock_upload").set_value(("stock.xlsx", stock_file.read_bytes(), MIME)).run()
    at.file_uploader(key="stock_orders_upload").set_value(
        [("orders.xlsx", orders_file.read_bytes(), MIME)]).run()
    assert not at.exception, [e.value for e in at.exception]
    store = ss.StockStore(tmp_path / "data" / "stock")
    assert store.load_stock()[0].quantities == {1000: 0, 1500: 5, 7000: 20} and len(store.load_orders()) == 3
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["הזמנות פתוחות"] == "2" and metrics["משתחררות עם חיתוך"] == "2"
    text = " ".join(m.value for m in at.markdown)
    assert "מציל את הזמנה A" in text and "7000" in text
    assert store.last_run()["cuts"] == 1 and store.last_run()["checks_ok"] is True
    fresh = _app(tmp_path, monkeypatch)                                     # new session, same disk: nothing to upload
    assert not fresh.info or "העלה קובץ מלאי" not in " ".join(i.value for i in fresh.info)
    assert {m.label: m.value for m in fresh.metric}["משתחררות עם חיתוך"] == "2"


# --------------------------------------------------------------------------- defaults and phone uploads
def test_santaf_defaults_to_pdf_and_stock_module_to_excel(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import stock_ui
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    assert not at.exception and at.radio[0].value == "PDF report"
    assert [u.label for u in at.file_uploader] == ["Upload digital PDF order reports"]
    assert stock_ui.ORDER_TYPES[0] == "xlsx" and "pdf" in stock_ui.ORDER_TYPES        # Excel first
    config = (Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert "maxUploadSize = 200" in config


def test_a_pdf_from_a_phone_is_recognised_by_content(monkeypatch):
    import stock_ui

    class Upload:                                           # a phone may send "report" or "REPORT.PDF"
        def __init__(self, name, data):
            self.name, self._data = name, data

        def getvalue(self):
            return self._data

    called = []
    monkeypatch.setattr(stock_ui.pdf_ingest, "load_many",
                        lambda files: (called.append(files[0][0]) or pd.DataFrame(), type("R", (), {"errors": {}})()))
    for name in ("report", "REPORT.PDF"):
        stock_ui._read_orders_upload(Upload(name, b"%PDF-1.7 ..."))
    assert called == ["report", "REPORT.PDF"]


# --------------------------------------------------------------------------- upload types for phones
def test_upload_types_add_mime_types_next_to_extensions():
    from uploads import upload_types
    types = upload_types("pdf")
    assert types[-1] == "pdf" and "application/pdf" in types
    both = upload_types("xlsx", "xls", "csv")
    assert "application/vnd.ms-excel" in both and "text/csv" in both and both[-3:] == ["xlsx", "xls", "csv"]
    from streamlit.elements.lib.file_uploader_utils import normalize_upload_file_type
    assert normalize_upload_file_type(types) == ["application/pdf", ".pdf"]


# --------------------------------------------------------------------------- automatic loading from folders
import stock_autoload as sa


def _inbox(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    _stock_xlsx(folder / "מלא סנטפים (1).xlsx", [(1500, 5), (7000, 20)])
    _orders_xlsx(folder / "דוח סנטפים נובמבר.xlsx", [("A", "2026-10-29", 3, 1000), ("B", "2026-10-30", 4, 1500)])
    _orders_xlsx(folder / "something else.xlsx", [("Z", "2026-10-29", 9, 9000)])          # not Santaf: ignored
    (folder / "~$דוח סנטפים.xlsx").write_bytes(b"lock")                                    # Office lock file: ignored
    return folder


def test_autoload_reads_the_files_once_and_copies_them_to_the_inbox(tmp_path):
    folder, store = _inbox(tmp_path), ss.StockStore(tmp_path / "data" / "stock")
    first = sa.scan(store, [folder])
    assert first.stock == "מלא סנטפים (1).xlsx" and first.orders == ["דוח סנטפים נובמבר.xlsx"]
    assert first.new_rows == 2 and not first.errors
    assert store.load_stock()[0].quantities == {1000: 0, 1500: 5, 7000: 20}               # 1000 mm added with 0 units
    assert store.load_orders()["order_id"].tolist() == ["A", "B"]
    assert {p.name for p in (store.root / "inbox").iterdir()} == {"מלא סנטפים (1).xlsx", "דוח סנטפים נובמבר.xlsx"}
    again = sa.scan(store, [folder])
    assert not again.changed and again.unchanged == 2 and len(store.load_orders()) == 2    # nothing is read twice
    assert "לא נמצאו קבצים חדשים" in sa.describe(again) and "מלאי:" in sa.describe(first)


def test_autoload_takes_the_newest_stock_and_new_reports_and_survives_bad_files(tmp_path):
    folder, store = _inbox(tmp_path), ss.StockStore(tmp_path / "data" / "stock")
    sa.scan(store, [folder])
    import os
    newer = _stock_xlsx(folder / "מלאי סנטפים חדש.xlsx", [(1500, 9)])
    os.utime(newer, (newer.stat().st_atime, newer.stat().st_mtime + 100))
    _orders_xlsx(folder / "דוח סנטפים דצמבר.xlsx", [("C", "2026-11-02", 1, 2000)])
    (folder / "דוח סנטפים שבור.pdf").write_bytes(b"not a pdf")
    report = sa.scan(store, [folder])
    assert report.stock == "מלאי סנטפים חדש.xlsx" and store.load_stock()[0].quantities[1500] == 9
    assert report.orders == ["דוח סנטפים דצמבר.xlsx"] and list(report.errors) == ["דוח סנטפים שבור.pdf"]
    assert store.load_orders()["order_id"].tolist() == ["A", "B", "C"]
    assert sa.scan(store, [tmp_path / "nowhere"]).searched == [str(tmp_path / "nowhere")]  # a missing folder is fine


def test_screen_loads_from_the_folder_without_any_upload(tmp_path, monkeypatch):
    folder = _inbox(tmp_path)
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(folder))
    at = _app(tmp_path, monkeypatch)                            # opens the stock module: the scan runs by itself
    assert not at.exception, [e.value for e in at.exception]
    assert {m.label: m.value for m in at.metric}["הזמנות פתוחות"] == "2"
    assert any("טעינה אוטומטית" in i.value for i in at.info)
    store = ss.StockStore(tmp_path / "data" / "stock")
    assert store.load_stock() is not None and (store.root / "inbox").exists()
