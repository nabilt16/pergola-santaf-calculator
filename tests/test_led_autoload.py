"""Automatic loading of the LED reports and the warehouse list, and the 28/10 cut-off."""
from datetime import date
from pathlib import Path

import led_archive
import led_autoload as la

from test_led import _led_pdf
from test_led_data import _xlsx

OLD = ("111111", "26/10/2026", 6, "70", 2, 2000, "OK", (1, 1, 1), "")        # already handled (up to 28/10)
EDGE = ("222222", "28/10/2026", 6, "70", 1, 3000, "OK", (1, 1, 1), "")
NEW1 = ("333333", "29/10/2026", 6, "20", 3, 1500, "OK", (0, 0, 0), "")
NEW2 = ("444444", "02/11/2026", 6, "50", 2, 2500, "OK", (0, 0, 0), "")


def _downloads(tmp_path):
    folder = tmp_path / "Downloads"
    (folder / "פרגולות").mkdir(parents=True)
    _led_pdf(folder / "פרגולות" / "לדים מ25_10 עד 30_10 (1).pdf", rows=[OLD, EDGE, NEW1])
    _led_pdf(folder / "פרגולות" / "לדים מ1_11 עד 31_11.pdf", rows=[NEW2])
    _xlsx(folder / "לדים ושנאים במחסן.xlsx", [("333333", "1*100W"), ("444444", "1*150W")])
    _led_pdf(folder / "דוח סנטפים.pdf", rows=[NEW1])                              # Santaf: not an LED file
    return folder


def test_autoload_saves_reports_and_warehouse_once(tmp_path):
    folder = _downloads(tmp_path)
    archive = led_archive.Archive(tmp_path / "data" / "archive")
    dirs = [folder, folder / "פרגולות"]
    first = la.scan(archive, dirs)
    assert sorted(first.reports) == ["לדים מ1_11 עד 31_11.pdf", "לדים מ25_10 עד 30_10 (1).pdf"]
    assert first.warehouse == ["לדים ושנאים במחסן.xlsx"] and not first.errors
    assert len(archive.entries("reports")) == 2 and len(archive.entries("warehouse")) == 1
    again = la.scan(archive, dirs)
    assert not again.changed and again.unchanged == 3 and len(archive.entries("reports")) == 2
    assert "לא נמצאו קבצים חדשים" in la.describe(again) and "דוחות לדים" in la.describe(first)


def test_autoload_skips_a_bad_file_and_ignores_missing_folders(tmp_path):
    folder = _downloads(tmp_path)
    (folder / "לדים שבור.pdf").write_bytes(b"not a pdf")
    archive = led_archive.Archive(tmp_path / "data" / "archive")
    result = la.scan(archive, [folder, folder / "פרגולות", tmp_path / "nowhere"])
    assert list(result.errors) == ["לדים שבור.pdf"] and len(result.reports) == 2


def test_screen_loads_without_upload_and_starts_on_29_10(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    folder = _downloads(tmp_path)
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", f"{folder}{__import__('os').pathsep}{folder / 'פרגולות'}")
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.segmented_control[0].set_value("לדים").run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("טעינה אוטומטית" in c.value for c in at.caption)
    start = [d for d in at.date_input if d.label == "מתאריך"][0]
    assert start.value == date(2026, 10, 29)                                    # 26/10 and 28/10 are not shown
    tables = [t.value for t in at.dataframe if "הזמנה" in t.value.columns and "קוד" in t.value.columns]
    shown = set().union(*[set(t["הזמנה"]) for t in tables]) if tables else set()
    assert {"333333", "444444"} <= shown and not ({"111111", "222222"} & shown)
