"""Tests for the Santaf stock planning (whole orders only)."""
import pandas as pd
import pytest

import calculator as calc
import stock_plan as sp

DAY = pd.Timestamp


def _panels(rows):
    """Processed panel rows (rounding and splitting done by the calculator) from (order, date, qty, length)."""
    df = pd.DataFrame(rows, columns=["Order ID", "Confirmation Date", "Original Quantity", "Original Length"])
    df["Code"], df["Zone"] = "6", "70"
    return calc.process_orders(df)


def _stock(quantities, as_of="2026-10-26"):
    return sp.Stock(dict(quantities), DAY(as_of))


# --------------------------------------------------------------------------- stock file
def test_load_stock_reads_lengths_quantities_and_date(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append([None, None, None, None])
    sheet.append([None, "מלאי סנטפים BH שקופים", None, "נכון לתאריך 26/10/26"])
    sheet.append([None, "אורך", "כמות", None])
    for length, qty in [(1500, 60), (2000, 80), (7500, 30)]:
        sheet.append([None, length, qty, None])
    path = tmp_path / "stock.xlsx"
    book.save(path)
    stock = sp.load_stock(path, add_zero_sizes=(1000,))
    assert stock.quantities == {1000: 0, 1500: 60, 2000: 80, 7500: 30}
    assert stock.as_of == DAY("2026-10-26") and stock.zero_added == [1000]
    assert sp.load_stock(path, add_zero_sizes=(1500,)).quantities[1500] == 60          # an existing size is kept
    empty = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty)
    with pytest.raises(ValueError):
        sp.load_stock(empty)


# --------------------------------------------------------------------------- orders
def test_order_demand_rounds_up_splits_and_keeps_report_order():
    panels = _panels([
        ("B", "2026-10-30", 1.2, 1300),       # quantity 2, length 1500
        ("A", "2026-10-29", 3, 4000),
        ("B", "2026-10-30", 1, 8000),         # 8000 splits into 5000 + 4000 (overlap 1000)
        ("C", "2026-10-29", 2, 0),            # no length: nothing to release
        ("D", "2026-10-29", 1, 2000),
    ])
    orders = sp.order_demand(panels)
    by_id = orders.set_index("order_id")
    assert by_id.loc["B", "demand"] == {1500: 2, 4000: 1, 5000: 1}
    assert by_id.loc["A", "demand"] == {4000: 3} and by_id.loc["C", "demand"] == {}
    assert orders["order_id"].tolist() == ["A", "C", "D", "B"]                         # date, then as printed
    assert orders["seq"].tolist() == [1, 2, 3, 4] and by_id.loc["B", "panels"] == 4


# --------------------------------------------------------------------------- whole orders only
def _orders():
    return sp.order_demand(_panels([
        ("A", "2026-10-29", 2, 2000),                       # fits
        ("B", "2026-10-30", 1, 2000), ("B", "2026-10-30", 2, 3000),     # needs 3000 x2 but only 1 is left
        ("C", "2026-10-31", 1, 2000),                       # fits again after B is skipped
        ("D", "2026-11-01", 2, 2500),                       # 2500 has no stock at all
        ("E", "2026-11-02", 1, 1500),
    ]))


def test_all_or_nothing_and_the_stuck_point():
    stock = _stock({1500: 5, 2000: 4, 2500: 0, 3000: 1})
    result = sp.simulate(_orders(), stock, skip_stuck=True)
    status = result.set_index("order_id")["status"].to_dict()
    assert status == {"A": "שוחררה", "B": "תקועה", "C": "שוחררה", "D": "תקועה", "E": "שוחררה"}
    b = result.set_index("order_id").loc["B"]
    assert b["missing"] == "1×3000" and b["missing_sizes"] == [3000]
    # B takes nothing of its 2000 panel (all or nothing): A used 2, C used 1, so 1 of 2000 is still free
    left_2000 = 4 - 2 - 1
    assert left_2000 == 1
    f = sp.findings(result)
    assert f.stuck_at["order_id"] == "B" and f.stuck_at["date"] == DAY("2026-10-30")
    assert f.released_in_sequence == 1 and f.last_released_date == DAY("2026-10-29")
    assert f.clear_through == DAY("2026-10-29")                                         # all of 29/10 is released
    assert (f.released_total, f.stuck_total, f.panels_stuck) == (3, 2, 5)
    strict = sp.simulate(_orders(), stock, skip_stuck=False)                            # nothing after the first stuck
    assert strict["status"].tolist() == ["שוחררה", "תקועה", "תקועה", "תקועה", "תקועה"]
    assert sp.findings(strict).released_total == 1


def test_stock_is_never_oversold_and_everything_fits_when_stock_is_large():
    big = _stock({1500: 99, 2000: 99, 2500: 99, 3000: 99})
    result = sp.simulate(_orders(), big)
    f = sp.findings(result)
    assert f.stuck_at is None and f.stuck_total == 0 and f.released_total == 5
    assert f.clear_through == DAY("2026-11-02")
    stock = _stock({1500: 5, 2000: 4, 2500: 0, 3000: 1})
    result = sp.simulate(_orders(), stock)
    used: dict[int, int] = {}
    demand = _orders().set_index("order_id")["demand"]
    for order in result[result["status"] == "שוחררה"]["order_id"]:
        for size, qty in demand[order].items():
            used[size] = used.get(size, 0) + qty
    assert all(used[s] <= stock.quantities.get(s, 0) for s in used)
    no_panels = sp.simulate(sp.order_demand(_panels([("Z", "2026-10-29", 2, 0)])), stock)
    assert no_panels.empty                                                              # nothing to release


# --------------------------------------------------------------------------- sizes and purchase
def test_size_table_shortage_stockout_date_and_days_of_stock():
    stock = _stock({1500: 5, 2000: 4, 2500: 0, 3000: 1})
    orders = _orders()
    result = sp.simulate(orders, stock)
    table = sp.size_table(orders, stock, result).set_index("size")
    assert table.loc[3000, "demand"] == 2 and table.loc[3000, "net_shortage"] == 1
    assert table.loc[3000, "stockout_date"] == DAY("2026-10-30") and table.loc[3000, "stockout_order"] == "B"
    assert table.loc[3000, "days_of_stock"] == 4                                        # 30/10 is 4 days after the stock date
    assert table.loc[2500, "stock"] == 0 and table.loc[2500, "net_shortage"] == 2
    assert table.loc[2500, "orders_stuck"] == 1 and table.loc[2500, "first_stuck_date"] == DAY("2026-11-01")
    assert table.loc[1500, "net_shortage"] == 0 and table.loc[1500, "surplus"] == 4
    assert pd.isna(table.loc[1500, "stockout_date"]) and table.loc[1500, "avg_per_day"] == round(1 / 5, 2)
    unlisted = _panels([("U", "2026-10-29", 3, 500)])                                   # 500 mm is not in the stock file
    t2 = sp.size_table(sp.order_demand(unlisted), stock, sp.simulate(sp.order_demand(unlisted), stock))
    assert not t2.set_index("size").loc[500, "in_stock_file"] and t2.set_index("size").loc[500, "net_shortage"] == 3


def test_purchase_plan_priorities_and_quantities():
    sizes = pd.DataFrame([
        {"size": 3000, "net_shortage": 100, "avg_per_day": 5.0, "days_of_stock": 2, "stockout_date": DAY("2026-10-28")},
        {"size": 4500, "net_shortage": 27, "avg_per_day": 1.18, "days_of_stock": 13, "stockout_date": DAY("2026-11-08")},
        {"size": 5500, "net_shortage": 20, "avg_per_day": 1.5, "days_of_stock": 20, "stockout_date": DAY("2026-11-15")},
        {"size": 7000, "net_shortage": 0, "avg_per_day": 0.4, "days_of_stock": None, "stockout_date": pd.NaT},
        {"size": 3500, "net_shortage": 109, "avg_per_day": 2.98, "days_of_stock": 0, "stockout_date": DAY("2026-10-26")},
    ])
    plan = sp.purchase_plan(sizes).set_index("size")
    assert plan["priority"].to_dict() == {3000: "דחוף מיידי", 4500: "דחוף", 5500: "בתכנון", 7000: "אין חוסר",
                                          3500: "דחוף מיידי"}
    assert plan.loc[3000, "buffer"] == 70 and plan.loc[3000, "order_qty"] == 170          # 100 + 14 days x 5.0
    assert plan.loc[3500, "buffer"] == 42 and plan.loc[7000, "order_qty"] == 0
    assert list(sp.purchase_plan(sizes)["size"])[:2] == [3500, 3000]                       # sooner stock-out first


# --------------------------------------------------------------------------- on-demand cutting
def _cut_orders():
    return sp.order_demand(_panels([
        ("A", "2026-10-29", 5, 1000),         # 1000 mm: none in stock, 5 needed
        ("B", "2026-10-30", 2, 7000),         # takes 2 of the 7000 panels itself
        ("C", "2026-10-31", 1, 3000),
    ]))


def test_classify_sizes_missing_short_and_surplus():
    stock = _stock({1000: 0, 3000: 1, 6000: 30, 7000: 60, 7500: 10})
    orders = sp.order_demand(_panels([("A", "2026-10-29", 3, 1000), ("B", "2026-10-29", 3, 3000),
                                      ("C", "2026-10-29", 30, 6000), ("D", "2026-10-29", 5, 7000),
                                      ("E", "2026-10-29", 8, 7500)]))
    table = sp.classify_sizes(sp.size_table(orders, stock, sp.simulate(orders, stock))).set_index("size")["class"]
    assert table.to_dict() == {1000: "חסר במלאי", 3000: "חוסר", 6000: "מאוזן", 7000: "עודף מוחלט", 7500: "עודף קל"}


def test_cut_only_on_demand_and_the_order_stays_whole():
    stock = _stock({7000: 5, 3000: 1})
    orders = _cut_orders()
    base = sp.simulate(orders, stock)
    assert base.set_index("order_id").loc["A", "status"] == "תקועה"
    result, cuts = sp.simulate_with_cuts(orders, stock)
    assert result["status"].tolist() == ["שוחררה"] * 3 and result["rescued_by_cut"].tolist() == [True, False, False]
    cut = cuts.iloc[0]
    assert (cut.order_id, cut.need_size, cut.need_qty, cut.donor) == ("A", 1000, 5, 7000)
    assert (cut.panels_cut, cut.pieces_per_panel, cut.pieces_made, cut.spare_pieces) == (1, 7, 7, 2)
    assert "7000" in cut.instruction and "מציל את הזמנה A" in cut.instruction
    assert cut.donor_before == 5 and cut.donor_after == 4 and cut.donor_margin == 4 - 2       # B still needs 2
    assert sp.verify_cuts(orders, stock, result, cuts, base) == []
    nothing_short = sp.simulate_with_cuts(orders, _stock({7000: 5, 3000: 1, 1000: 5}))[1]
    assert nothing_short.empty                                                                 # no blind cutting


def test_cut_never_eats_into_the_donors_own_future_demand():
    orders = sp.order_demand(_panels([("A", "2026-10-29", 5, 1000), ("B", "2026-10-30", 2, 7000)]))
    stock = _stock({7000: 2})                                  # both 7000 are reserved for B: nothing to cut
    result, cuts = sp.simulate_with_cuts(orders, stock)
    assert cuts.empty and result.set_index("order_id").loc["A", "status"] == "תקועה"
    assert result.set_index("order_id").loc["B", "status"] == "שוחררה"


def test_cut_covers_several_short_sizes_or_none_and_rejects_harm():
    orders = sp.order_demand(_panels([("A", "2026-10-29", 2, 1000), ("A", "2026-10-29", 3, 2000)]))
    result, cuts = sp.simulate_with_cuts(orders, _stock({7000: 4}))
    assert sorted(cuts["need_size"]) == [1000, 2000] and result["status"].tolist() == ["שוחררה"]
    only_one = sp.simulate_with_cuts(orders, _stock({3000: 1}))                                # 2000 cannot be covered
    assert only_one[1].empty and only_one[0]["status"].tolist() == ["תקועה"]
    # rescuing A would use the last 2000 mm panels that the later order B needs: the cut is refused
    harm = sp.order_demand(_panels([("A", "2026-10-29", 1, 1000), ("A", "2026-10-29", 2, 2000),
                                    ("B", "2026-10-30", 1, 2000)]))
    stock = _stock({2000: 2, 7000: 1})
    res, cuts = sp.simulate_with_cuts(harm, stock)
    assert res.set_index("order_id")["status"].to_dict() == {"A": "תקועה", "B": "שוחררה"} and cuts.empty
    assert sp.verify_cuts(harm, stock, res, cuts, sp.simulate(harm, stock)) == []


def test_offcut_and_kerf_and_verify_detects_a_bad_plan():
    stock = _stock({7000: 3})
    orders = sp.order_demand(_panels([("A", "2026-10-29", 2, 3000)]))
    result, cuts = sp.simulate_with_cuts(orders, stock)
    cut = cuts.iloc[0]
    assert (cut.panels_cut, cut.pieces_per_panel, cut.offcut_size) == (1, 2, 1000)             # 7000 = 2x3000 + 1000
    kerf = sp.simulate_with_cuts(sp.order_demand(_panels([("A", "2026-10-29", 7, 1000)])), stock, kerf_mm=10)[1]
    assert kerf.iloc[0].pieces_per_panel == 6                                                  # (7000+10)//(1000+10)
    bad = cuts.copy()
    bad["donor_margin"] = -1
    assert sp.verify_cuts(orders, stock, result, bad, sp.simulate(orders, stock))
    assert sp.cut_report(cuts)[0].startswith("29/10/2026 | הזמנה A")


# --------------------------------------------------------------------------- starting point 29/10
def test_open_orders_start_on_29_10_and_earlier_orders_are_ignored():
    panels = _panels([
        ("X1", "2026-10-26", 50, 3500), ("X2", "2026-10-28", 50, 3500),          # already left in full
        ("A", "2026-10-29", 2, 3500), ("B", "2026-11-30", 1, 3500),
        ("Z", "2026-12-01", 5, 3500),                                              # after the period
    ])
    everything = sp.order_demand(panels)
    window = sp.open_orders(everything)
    assert sp.SIM_START == DAY("2026-10-29") and sp.SIM_END == DAY("2026-11-30")
    assert window["order_id"].tolist() == ["A", "B"] and window["seq"].tolist() == [1, 2]
    stock = _stock({3500: 3})                                                      # stock of 26/10, already net of 26-28/10
    assert sp.findings(sp.simulate(window, stock)).stuck_total == 0               # the old orders cause no stuck
    assert sp.findings(sp.simulate(everything, stock)).stuck_total > 0            # they would, if they were counted
    assert sp.size_table(window, stock, sp.simulate(window, stock)).set_index("size").loc[3500, "demand"] == 3
    assert sp.open_orders(everything, "2026-10-28", "2026-10-28")["order_id"].tolist() == ["X2"]


def test_only_absolute_surplus_sizes_are_cut():
    orders = sp.order_demand(_panels([("A", "2026-10-29", 1, 1000), ("B", "2026-10-30", 4, 1500)]))
    slight = _stock({1500: 5, 7000: 0})                         # 1500 has a slight surplus only (5 vs 4)
    assert sp.absolute_surplus_sizes(orders, slight) == set()
    assert sp.simulate_with_cuts(orders, slight)[1].empty       # so nothing is cut from it
    big = _stock({1500: 5, 7000: 20, 7500: 2})
    assert sp.absolute_surplus_sizes(orders, big) == {7000, 7500}
    cuts = sp.simulate_with_cuts(orders, big)[1]
    assert set(cuts["donor"]) <= {7000, 7500} and cuts.iloc[0].donor == 7000
    assert sp.simulate_with_cuts(orders, slight, donor_ratio=1.0)[1].iloc[0].donor == 1500   # explicit override
