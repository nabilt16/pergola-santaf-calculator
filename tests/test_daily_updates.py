"""Daily updates: Santaf orders that already got panels, LED kits that already reached the warehouse."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import calculator as calc
import led
import stock_plan as sp
import stock_store as ss
import warehouse as wh

from test_stock_plan import _panels, _stock
from test_stock_store import _orders, _orders_xlsx, _stock_xlsx

DAY = pd.Timestamp


# --------------------------------------------------------------------------- Santaf: the ledger of issued orders
def _queue():
    return sp.order_demand(_panels([
        ("100001", "2026-10-29", 2, 2000),
        ("100002", "2026-11-10", 3, 3000),          # far in the future, but its panels are given out now
        ("100003", "2026-11-11", 3, 3000),
        ("100004", "2026-11-12", 1, 2000),
    ]))


def test_parse_order_ids_finds_six_digit_numbers_only():
    text = "100002, 100003\n12345 1234567 order 100004;100002"
    assert sp.parse_order_ids(text) == ["100002", "100003", "100004"]


def test_issued_after_the_stock_date_is_deducted_and_leaves_the_queue():
    stock = _stock({2000: 5, 3000: 4})
    issued = {"100002": {"issued_on": "2026-10-27"}}
    effect = sp.apply_issued(_queue(), stock, issued)
    assert effect.stock.quantities == {2000: 5, 3000: 1} and effect.table.iloc[0].deducted
    queue = sp.drop_orders(sp.open_orders(_queue(), "2026-10-29", "2026-11-30"), issued)
    assert queue["order_id"].tolist() == ["100001", "100003", "100004"] and queue["seq"].tolist() == [1, 2, 3]
    before = sp.simulate(sp.open_orders(_queue(), "2026-10-29", "2026-11-30"), stock)
    after = sp.simulate(queue, effect.stock)
    # without the update 100002 takes 3 of the 4 panels of 3000 and 100003 is stuck; with it only 1 is left and
    # 100003 (3 needed) is stuck as well, but 100002 is no longer in the queue
    assert before.set_index("order_id").loc["100003", "status"] == "תקועה"
    assert "100002" not in set(after["order_id"]) and after.set_index("order_id").loc["100003", "missing"] == "2×3000"


def test_issued_on_or_before_the_stock_date_is_only_removed_from_the_queue():
    stock = _stock({3000: 4})                                   # stock of 26/10
    effect = sp.apply_issued(_queue(), stock, {"100002": {"issued_on": "2026-10-26"}})
    assert effect.stock.quantities == {3000: 4} and not effect.table.iloc[0].deducted
    effect = sp.apply_issued(_queue(), stock, {"100002": {"issued_on": "2026-10-26"}})
    assert effect.shortfall == {}


def test_unknown_orders_and_a_deduction_larger_than_the_stock():
    stock = _stock({3000: 2})
    effect = sp.apply_issued(_queue(), stock, {"999999": {"issued_on": "2026-10-28"},
                                               "100002": {"issued_on": "2026-10-28"}})
    assert effect.unknown == ["999999"]
    assert effect.stock.quantities == {3000: 0} and effect.shortfall == {3000: 1}           # never below 0


def test_an_early_issue_changes_the_cutting_and_stuck_picture():
    orders = sp.order_demand(_panels([("100010", "2026-10-29", 3, 1000), ("100011", "2026-11-05", 5, 1000)]))
    stock = _stock({7000: 1})                                    # one 7000 panel gives 7 pieces of 1000
    open_ = sp.open_orders(orders, "2026-10-29", "2026-11-30")
    result, cuts = sp.simulate_with_cuts(open_, stock)
    assert result["status"].tolist() == ["שוחררה", "תקועה"]       # 3 + 5 = 8 pieces, only 7 can be made
    assert len(cuts) == 1 and cuts.iloc[0].order_id == "100010"
    issued = {"100011": {"issued_on": "2026-10-28"}}              # 100011 already got its pieces from other stock
    queue = sp.drop_orders(open_, issued)
    result, cuts = sp.simulate_with_cuts(queue, sp.apply_issued(orders, stock, issued).stock)
    assert result["status"].tolist() == ["שוחררה"] and len(cuts) == 1                    # the line is no longer stuck


# --------------------------------------------------------------------------- Santaf: the store
def test_issued_ledger_and_daily_report_update_in_the_store(tmp_path):
    store = ss.StockStore(tmp_path)
    assert store.issued() == {}
    assert store.mark_issued(["100002", "100003"], "2026-10-27", "early") == 2
    assert store.mark_issued(["100002"], "2026-10-28") == 0                              # already marked: date updated
    assert store.issued()["100002"]["issued_on"] == "2026-10-28" and store.issued()["100003"]["note"] == "early"
    store.unmark_issued(["100003"])
    assert list(ss.StockStore(tmp_path).issued()) == ["100002"]                           # survives a restart
    assert "issued.json" in ss.NAMES

    store.save_orders(_orders([("A", "2026-10-29", 2, 1500), ("B", "2026-11-10", 1, 2000), ("C", "2026-11-11", 1, 2000)]), "d1")
    added = store.save_orders(_orders([("B", "2026-11-03", 3, 2000), ("D", "2026-11-12", 1, 1500)]), "d2")   # B changed
    frame = store.load_orders()
    assert frame["order_id"].tolist() == ["A", "B", "C", "D"]                             # B replaced in place, not doubled
    assert frame.set_index("order_id").loc["B", "confirmation_date"] == "2026-11-03"
    assert added == 1 and store.orders_files()[-1]["changed_orders"] == 1
    store.save_orders(_orders([("B", "2026-11-03", 3, 2000)]), "d3")
    assert store.orders_files()[-1]["changed_orders"] == 0 and len(store.load_orders()) == 4


def _app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("PERGOLA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PERGOLA_INBOX_DIRS", str(tmp_path / "none"))
    app = Path(__file__).resolve().parents[1] / "app.py"
    at = AppTest.from_file(str(app), default_timeout=90).run()
    at.segmented_control[0].set_value("מלאי סנטפים").run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_screen_marks_orders_as_issued_and_recomputes(tmp_path, monkeypatch):
    store = ss.StockStore(tmp_path / "data" / "stock")
    store.save_stock(sp.Stock({1500: 5, 7000: 20}, DAY("2026-10-26")), "s.xlsx")
    store.save_orders(_orders([("100010", "2026-10-29", 3, 1500), ("100011", "2026-11-05", 3, 1500),
                               ("100012", "2026-11-06", 3, 1500)]), "r.xlsx")
    at = _app(tmp_path, monkeypatch)
    assert {m.label: m.value for m in at.metric}["הזמנות פתוחות"] == "3"
    at.text_area(key="stock_issue_text").set_value("100011").run()
    at.button(key="stock_issue_go").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert list(store.issued()) == ["100011"]
    assert {m.label: m.value for m in at.metric}["הזמנות פתוחות"] == "2"                  # left the queue
    assert any("הוסרו מהתור" in c.value for c in at.caption)
    again = _app(tmp_path, monkeypatch)                                                   # a new session keeps it
    assert {m.label: m.value for m in again.metric}["הזמנות פתוחות"] == "2"


# --------------------------------------------------------------------------- LED: arrival at the warehouse
def _lines(rows):
    base = {"status_key": "ordered", "stage": "הוזמן", "main_order": "", "meters": 2.0, "warehouse_state": "",
            "wh_alloc": ""}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_arrival_dates_start_from_a_baseline_list():
    first = pd.DataFrame({"order_id": ["111111", "222222"]})
    second = pd.DataFrame({"order_id": ["111111", "222222", "333333"]})
    third = pd.DataFrame({"order_id": ["333333", "444444"]})
    table = wh.arrival_dates([("2026-10-26", first), ("2026-10-28", second), ("2026-10-30", third)]).set_index("order_id")
    assert table.loc["111111", "baseline"] and table.loc["111111", "arrived_on"] == DAY("2026-10-26")
    assert not table.loc["333333", "baseline"] and table.loc["333333", "arrived_on"] == DAY("2026-10-28")
    assert table.loc["444444", "arrived_on"] == DAY("2026-10-30")
    assert wh.arrival_dates([]).empty


def test_early_release_crosses_arrived_kits_with_future_orders(tmp_path):
    today = DAY("2026-10-27")
    lines = _lines([
        {"order_id": "111111", "order_date": DAY("2026-11-10"), "warehouse_state": wh.IN_LIST, "wh_alloc": "1*100W"},
        {"order_id": "222222", "order_date": DAY("2026-11-12")},                       # not arrived, far: nothing to do
        {"order_id": "333333", "order_date": DAY("2026-10-30")},                       # due in 3 days, no kit yet
        {"order_id": "444444", "order_date": DAY("2026-11-15"), "warehouse_state": wh.MANUAL},
        {"order_id": "555555", "order_date": DAY("2026-10-26"), "warehouse_state": wh.IN_LIST},      # date is past
        {"order_id": "666666", "order_date": DAY("2026-11-20"), "warehouse_state": wh.IN_LIST, "stage": "סופק"},
        {"order_id": "777777", "order_date": DAY("2026-11-21"), "warehouse_state": wh.IN_LIST, "status_key": "cancelled"},
    ])
    arrivals = wh.arrival_dates([("2026-10-26", pd.DataFrame({"order_id": []})),
                                 ("2026-10-27", pd.DataFrame({"order_id": ["111111"]}))])
    result = wh.early_release(lines, arrivals, {"444444": "2026-10-27"}, today)
    assert result["ready"]["order_id"].tolist() == ["111111", "444444"]                # release early, soonest first
    assert result["ready"].set_index("order_id").loc["111111", "days_ahead"] == 14
    assert result["ready"].set_index("order_id").loc["444444", "arrived_on"] == DAY("2026-10-27")
    assert result["waiting"]["order_id"].tolist() == ["333333"]
    assert set(result["all"]["order_id"]) == {"111111", "222222", "333333", "444444", "555555"}


def test_manual_arrival_store_and_annotation(tmp_path):
    store = wh.ArrivalStore(tmp_path / "arr.json")
    assert store.arrived() == {} and store.mark(["111111", "222222"], "2026-10-27") == 2
    assert store.mark(["111111"], "2026-10-28") == 0 and store.arrived()["111111"] == "2026-10-28"
    store.unmark(["222222"])
    assert list(wh.ArrivalStore(tmp_path / "arr.json").arrived()) == ["111111"]
    lines = _lines([{"order_id": "111111", "order_date": DAY("2026-11-10"), "status_key": "ordered"},
                    {"order_id": "999999", "order_date": DAY("2026-11-10"), "status_key": "ordered",
                     "main_order": "111111"},
                    {"order_id": "888888", "order_date": DAY("2026-11-10"), "status_key": "ordered"}])
    out = wh.annotate_lines(lines, None, manual=store.arrived())
    assert out["warehouse_state"].tolist() == [wh.MANUAL, wh.MANUAL, ""]                # also via the main order
