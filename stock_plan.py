"""Santaf stock planning: when does the stock run out for whole orders, and what must be bought.

Rule ("whole orders only"): an order leaves the warehouse only if EVERY panel it needs is in stock,
otherwise nothing of it is released. Panel sizes are the processed ones (lengths rounded up to 500 mm,
quantities rounded up, 8000 mm and above split in two) from calculator.process_orders().

Orders are released in the order of the reports (by date, then as printed). Two views are computed:
  * in sequence ("strict"): the first order that cannot be completed is where the line gets stuck;
  * with skipping: a stuck order waits and the later orders that still fit are released.

This module only reads data and does arithmetic; it does not touch the Santaf calculator or its screens.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pandas as pd

LENGTH_STEP = 500
# Field routine: the stock of 26/10 was already used for the orders of 26-28/10 (work was done a day ahead because
# 27/10 is an election day), so those orders left in full and the simulation starts clean on 29/10.
SIM_START = pd.Timestamp("2026-10-29")
SIM_END = pd.Timestamp("2026-11-30")
URGENT_DAYS, SOON_DAYS = 7, 14            # days of stock below which a size is "urgent now" / "urgent"


# --------------------------------------------------------------------------
# Stock file
# --------------------------------------------------------------------------
@dataclass
class Stock:
    quantities: dict[int, int]                    # panel length (mm) -> units in stock
    as_of: pd.Timestamp | None = None
    zero_added: list[int] = field(default_factory=list)   # sizes added to the list with 0 units


def _number(value):
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return number if number == number else None


def load_stock(source, add_zero_sizes: tuple[int, ...] = ()) -> Stock:
    """Read a stock sheet (length, quantity columns found by content) and the 'as of' date if it has one.

    `add_zero_sizes` are sizes added to the list with 0 units (for example 1000 mm).
    """
    table = pd.read_excel(source, header=None, dtype=str)
    pairs: dict[int, dict[int, int]] = {}
    as_of = None
    for _, row in table.iterrows():
        cells = list(row)
        for column, cell in enumerate(cells):
            text = "" if pd.isna(cell) else str(cell)
            match = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", text)
            if match and as_of is None:
                day, month, year = (int(g) for g in match.groups())
                as_of = pd.Timestamp(year + 2000 if year < 100 else year, month, day)
            length = _number(cell)
            if (length is not None and column + 1 < len(cells) and length % LENGTH_STEP == 0
                    and LENGTH_STEP <= length <= 20000):
                quantity = _number(cells[column + 1])
                if quantity is not None and quantity >= 0 and quantity == int(quantity):
                    pairs.setdefault(column, {})[int(length)] = int(quantity)
    if not pairs:
        raise ValueError("No column of panel lengths with quantities was found in the stock file.")
    quantities = max(pairs.values(), key=len)
    added = [s for s in add_zero_sizes if s not in quantities]
    for size in added:
        quantities[size] = 0
    return Stock(dict(sorted(quantities.items())), as_of, added)


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def order_demand(panels: pd.DataFrame) -> pd.DataFrame:
    """One row per order: date, release position and the panels it needs per size (a dict).

    `panels` is the output of calculator.process_orders(). Lines without a length or a quantity need no
    panels; an order made only of such lines is kept with an empty demand. The release position follows
    the order of the reports (by date, then as printed).
    """
    rows = []
    real = panels[(panels["panel_length"] > 0) & (panels["quantity"] > 0)]
    first_seen = panels.groupby("order_id")["source_row"].min()
    dates = panels.groupby("order_id")["confirmation_date"].min()
    for order_id, group in real.groupby("order_id"):
        demand = group.groupby("panel_length")["quantity"].sum()
        rows.append({"order_id": order_id, "date": dates[order_id], "seq_key": first_seen[order_id],
                     "demand": {int(k): int(v) for k, v in demand.items()}})
    empty = sorted(set(panels["order_id"]) - set(real["order_id"]))
    rows += [{"order_id": o, "date": dates[o], "seq_key": first_seen[o], "demand": {}} for o in empty]
    out = pd.DataFrame(rows, columns=["order_id", "date", "seq_key", "demand"])
    out = out.sort_values(["date", "seq_key"], kind="stable").reset_index(drop=True)
    out["seq"] = range(1, len(out) + 1)
    out["panels"] = out["demand"].map(lambda d: sum(d.values()))
    return out[["seq", "date", "order_id", "panels", "demand"]]


def open_orders(orders: pd.DataFrame, start=SIM_START, end=SIM_END) -> pd.DataFrame:
    """The orders still to be released: dated `start` to `end` (inclusive), renumbered from 1.

    Earlier orders already left the warehouse and are neither open demand nor a cause of a stuck line.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    kept = orders[(orders["date"] >= start) & (orders["date"] <= end)].reset_index(drop=True).copy()
    kept["seq"] = range(1, len(kept) + 1)
    return kept


def _describe(demand: dict[int, int]) -> str:
    return ", ".join(f"{qty}×{size}" for size, qty in sorted(demand.items()))


def simulate(orders: pd.DataFrame, stock: Stock, skip_stuck: bool = True) -> pd.DataFrame:
    """Release orders one by one against the stock (all or nothing).

    Returns one row per order with `status` ('שוחררה' / 'תקועה'), the missing units per size for a stuck
    order, `in_sequence` (released before the first stuck order) and the stock left afterwards.
    With skip_stuck=False nothing is released after the first stuck order (strict sequence).
    """
    left = dict(stock.quantities)
    rows, stuck_seen = [], False
    for order in orders[orders["panels"] > 0].itertuples():      # an order without panels has nothing to release
        missing = {size: qty - left.get(size, 0) for size, qty in order.demand.items() if qty > left.get(size, 0)}
        blocked_by_line = stuck_seen and not skip_stuck
        released = not missing and not blocked_by_line
        if released:
            for size, qty in order.demand.items():
                left[size] = left.get(size, 0) - qty
        if missing and not stuck_seen:
            stuck_seen = True
            first_stuck = True
        else:
            first_stuck = False
        rows.append({
            "seq": order.seq, "date": order.date, "order_id": order.order_id, "panels": order.panels,
            "demand": _describe(order.demand), "status": "שוחררה" if released else "תקועה",
            "missing": _describe(missing), "missing_sizes": sorted(missing),
            "in_sequence": released and not stuck_seen, "first_stuck": first_stuck,
        })
    return pd.DataFrame(rows)


@dataclass
class Findings:
    stuck_at: dict | None                 # first order that cannot be completed
    released_in_sequence: int
    last_released_date: pd.Timestamp | None       # date of the last order released before the first stuck one
    clear_through: pd.Timestamp | None            # last date on which every order (and all before) was released
    released_total: int
    stuck_total: int
    panels_stuck: int


def findings(result: pd.DataFrame) -> Findings:
    """The headline numbers of a simulate() result."""
    stuck = result[result["first_stuck"]]
    sequence = result[result["in_sequence"]]
    if stuck.empty:
        return Findings(None, len(sequence), result["date"].max() if len(result) else None,
                        result["date"].max() if len(result) else None, len(sequence), 0, 0)
    first = stuck.iloc[0]
    clear = None
    for date, group in result.groupby("date", sort=True):
        if group["in_sequence"].all():
            clear = date
        else:
            break
    return Findings(
        {"date": first["date"], "order_id": first["order_id"], "missing": first["missing"],
         "demand": first["demand"]},
        len(sequence), sequence["date"].max() if len(sequence) else None, clear,
        int((result["status"] == "שוחררה").sum()), int((result["status"] == "תקועה").sum()),
        int(result.loc[result["status"] == "תקועה", "panels"].sum()))


# --------------------------------------------------------------------------
# Sizes: shortage, stock-out date, days of stock
# --------------------------------------------------------------------------
def size_table(orders: pd.DataFrame, stock: Stock, result: pd.DataFrame,
               window: tuple[pd.Timestamp, pd.Timestamp] | None = None) -> pd.DataFrame:
    """Per panel size: stock, total demand, net shortage, when the stock is used up and for how long it lasts.

    The stock-out date is the date of the order whose panels make the running demand (in release order)
    exceed the stock. Days of stock count from the stock date (or the first order date if there is none).
    """
    start, end = window or (orders["date"].min(), orders["date"].max())
    days = max(1, (end - start).days + 1)
    reference = stock.as_of if stock.as_of is not None else start
    sizes = sorted(set(stock.quantities) | {s for d in orders["demand"] for s in d})
    stuck_by_size: dict[int, int] = {}
    first_stuck_by_size: dict[int, pd.Timestamp] = {}
    for row in result[result["status"] == "תקועה"].itertuples():
        for size in row.missing_sizes:
            stuck_by_size[size] = stuck_by_size.get(size, 0) + 1
            first_stuck_by_size.setdefault(size, row.date)
    rows = []
    for size in sizes:
        have = stock.quantities.get(size, 0)
        running, out_date, out_order = 0, None, None
        total, in_orders = 0, 0
        for order in orders.itertuples():
            qty = order.demand.get(size, 0)
            if not qty:
                continue
            in_orders += 1
            total += qty
            running += qty
            if out_date is None and running > have:
                out_date, out_order = order.date, order.order_id
        average = total / days
        rows.append({
            "size": size, "in_stock_file": size in stock.quantities, "stock": have, "demand": total,
            "net_shortage": max(0, total - have), "surplus": max(0, have - total), "orders_needing": in_orders,
            "stockout_date": out_date, "stockout_order": out_order,
            "days_of_stock": (out_date - reference).days if out_date is not None else None,
            "avg_per_day": round(average, 2),
            "days_at_average": round(have / average, 1) if average else None,
            "orders_stuck": stuck_by_size.get(size, 0), "first_stuck_date": first_stuck_by_size.get(size),
        })
    return pd.DataFrame(rows)


def purchase_plan(sizes: pd.DataFrame, buffer_days: int = 14) -> pd.DataFrame:
    """What to order: the net shortage to the end of the period plus a buffer of `buffer_days` at the average rate.

    Priority follows the days of stock left: already short or <= 7 days = urgent now, <= 14 = urgent,
    otherwise it is only needed if there is a shortage by the end of the period.
    """
    plan = sizes.copy()
    plan["buffer"] = [math.ceil(r.avg_per_day * buffer_days) if r.net_shortage > 0 else 0 for r in plan.itertuples()]
    plan["order_qty"] = plan["net_shortage"] + plan["buffer"]

    def priority(row):
        if row.net_shortage == 0:
            return "אין חוסר"
        if row.days_of_stock is None or row.days_of_stock <= URGENT_DAYS:
            return "דחוף מיידי"
        return "דחוף" if row.days_of_stock <= SOON_DAYS else "בתכנון"

    plan["priority"] = [priority(r) for r in plan.itertuples()]
    rank = {"דחוף מיידי": 0, "דחוף": 1, "בתכנון": 2, "אין חוסר": 3}
    plan["_rank"] = plan["priority"].map(rank)
    return (plan.sort_values(["_rank", "stockout_date", "size"], na_position="last")
            .drop(columns="_rank").reset_index(drop=True))


# --------------------------------------------------------------------------
# On-demand cutting: a spare panel is cut only to rescue an order that is stuck on a short size
# --------------------------------------------------------------------------
SURPLUS_RATIO = 1.5            # stock >= 1.5 x the demand of the period = absolute surplus
RELEASED = "שוחררה"
STUCK = "תקועה"


def classify_sizes(sizes: pd.DataFrame) -> pd.DataFrame:
    """Add `class` to a size_table(): missing from stock, short, absolute surplus, slight surplus or balanced."""
    out = sizes.copy()

    def kind(row):
        if row.demand and row.stock == 0:
            return "חסר במלאי"
        if row.net_shortage > 0:
            return "חוסר"
        if row.surplus > 0:
            return "עודף מוחלט" if row.stock >= SURPLUS_RATIO * row.demand else "עודף קל"
        return "מאוזן"

    out["class"] = [kind(r) for r in out.itertuples()]
    return out


def _missing(demand: dict[int, int], left: dict[int, int]) -> dict[int, int]:
    return {s: q - left.get(s, 0) for s, q in demand.items() if q > left.get(s, 0)}


def _tail_released(tail: list, left: dict[int, int], skip_stuck: bool) -> set:
    """Orders of `tail` that are released from `left` without any cutting (used to prove a cut harms nothing)."""
    left = dict(left)
    released = set()
    for order in tail:
        if _missing(order.demand, left):
            if not skip_stuck:
                break
            continue
        for size, qty in order.demand.items():
            left[size] -= qty
        released.add(order.order_id)
    return released


def _choose_donor(left, pending, size, short, kerf, eligible):
    """Cheapest donor size for `short` panels of `size`: enough FREE panels (stock minus all pending demand)."""
    best = None
    for donor in sorted(left):
        if donor <= size or donor not in eligible:
            continue
        per = (donor + kerf) // (size + kerf)               # pieces of `size` per donor panel
        free = left[donor] - pending.get(donor, 0)
        if per < 1 or free <= 0:
            continue
        panels = math.ceil(short / per)
        if panels > free:
            continue
        waste = panels * donor - short * size
        if best is None or (waste, donor) < best[0]:
            best = ((waste, donor), donor, per, panels)
    return best


def _apply_cut(left, size, donor, per, panels, kerf):
    """Cut `panels` panels of `donor` into `per` pieces each; the leftover (>= 500 mm) goes back to stock."""
    left[donor] -= panels
    left[size] = left.get(size, 0) + panels * per
    offcut = (donor - per * (size + kerf)) // LENGTH_STEP * LENGTH_STEP
    if offcut >= LENGTH_STEP:
        left[offcut] = left.get(offcut, 0) + panels
        return offcut
    return 0


def _count(n: int, one: str, many: str) -> str:
    return f"{one}" if n == 1 else f"{n} {many}"


def cut_instruction(order_id, step: dict) -> str:
    """One concrete line for the warehouse: which panel to take, how many, into what, and what it rescues."""
    text = (f"להוציא {_count(step['panels'], 'פנל אחד', 'פנלים')} של {step['donor']} ולחתוך "
            f"{'אותו' if step['panels'] == 1 else 'כל אחד'} ל-{_count(step['per'], 'חלק אחד', 'חלקים')} "
            f"של {step['size']} (חסרו {step['short']}) - מציל את הזמנה {order_id}")
    spare = step["panels"] * step["per"] - step["short"]
    if spare:
        text += f"; {_count(spare, 'חלק עודף אחד', 'חלקים עודפים')} של {step['size']} חוזרים למלאי"
    if step["offcut"]:
        text += f"; שארית {step['offcut']} מ\"מ {'מהפנל' if step['panels'] == 1 else 'מכל פנל'} חוזרת למלאי"
    return text


CUT_COLUMNS = ["seq", "date", "order_id", "need_size", "need_qty", "donor", "panels_cut", "pieces_per_panel",
               "pieces_made", "spare_pieces", "offcut_size", "donor_before", "donor_after",
               "donor_future_demand", "donor_margin", "instruction"]


def absolute_surplus_sizes(orders: pd.DataFrame, stock: Stock, ratio: float = SURPLUS_RATIO) -> set[int]:
    """Sizes whose stock is at least `ratio` x the demand of the whole period: the only ones that may be cut."""
    total: dict[int, int] = {}
    for demand in orders["demand"]:
        for size, qty in demand.items():
            total[size] = total.get(size, 0) + qty
    return {size for size, have in stock.quantities.items() if have > 0 and have >= ratio * total.get(size, 0)}


def simulate_with_cuts(orders: pd.DataFrame, stock: Stock, skip_stuck: bool = True, kerf_mm: int = 0,
                       donor_ratio: float = SURPLUS_RATIO):
    """simulate() plus cutting on demand. Returns (result, cuts).

    A stuck order is rescued only if every short size can be cut from a donor size (larger, in absolute surplus
    for the whole period, see absolute_surplus_sizes) whose free
    stock - after reserving ALL pending demand of that size, this order included - covers the cut, and only if
    the rest of the line releases at least the orders it released without the cut. Orders stay whole.
    `result` has the columns of simulate() plus `rescued_by_cut`; `cuts` has one row per cut.
    """
    work = list(orders[orders["panels"] > 0].itertuples())
    left = dict(stock.quantities)
    eligible = absolute_surplus_sizes(orders, stock, donor_ratio)
    pending: dict[int, int] = {}
    for order in work:
        for size, qty in order.demand.items():
            pending[size] = pending.get(size, 0) + qty
    rows, cuts, stuck_seen = [], [], False
    for index, order in enumerate(work):
        missing = _missing(order.demand, left)
        blocked = stuck_seen and not skip_stuck
        rescued = False
        if missing and not blocked:
            trial, plan = dict(left), []
            for size, short in sorted(missing.items()):
                pick = _choose_donor(trial, pending, size, short, kerf_mm, eligible)
                if pick is None:
                    plan = None
                    break
                _, donor, per, panels = pick
                before = trial[donor]
                offcut = _apply_cut(trial, size, donor, per, panels, kerf_mm)
                plan.append({"size": size, "short": short, "donor": donor, "per": per, "panels": panels,
                             "offcut": offcut, "donor_before": before})
            if plan:
                for size, qty in order.demand.items():
                    trial[size] -= qty
                tail = work[index + 1:]
                if _tail_released(tail, left, skip_stuck) <= _tail_released(tail, trial, skip_stuck):
                    after_pending = dict(pending)
                    for size, qty in order.demand.items():
                        after_pending[size] -= qty
                    for step in plan:
                        donor = step["donor"]
                        made = step["panels"] * step["per"]
                        cuts.append({
                            "seq": order.seq, "date": order.date, "order_id": order.order_id,
                            "need_size": step["size"], "need_qty": step["short"], "donor": donor,
                            "panels_cut": step["panels"], "pieces_per_panel": step["per"],
                            "pieces_made": made, "spare_pieces": made - step["short"],
                            "offcut_size": step["offcut"], "donor_before": step["donor_before"],
                            "donor_after": trial[donor], "donor_future_demand": after_pending.get(donor, 0),
                            "donor_margin": trial[donor] - after_pending.get(donor, 0),
                            "instruction": cut_instruction(order.order_id, step)})
                    left, rescued = trial, True
        released = rescued or (not missing and not blocked)
        if released and not rescued:
            for size, qty in order.demand.items():
                left[size] -= qty
        if released:
            for size, qty in order.demand.items():
                pending[size] -= qty
        first_stuck = not released and not stuck_seen
        stuck_seen = stuck_seen or not released
        rows.append({
            "seq": order.seq, "date": order.date, "order_id": order.order_id, "panels": order.panels,
            "demand": _describe(order.demand), "status": RELEASED if released else STUCK,
            "missing": _describe(missing), "missing_sizes": sorted(missing),
            "in_sequence": released and not stuck_seen, "first_stuck": first_stuck, "rescued_by_cut": rescued})
    return pd.DataFrame(rows), pd.DataFrame(cuts, columns=CUT_COLUMNS)


def verify_cuts(orders: pd.DataFrame, stock: Stock, result: pd.DataFrame, cuts: pd.DataFrame,
                baseline: pd.DataFrame) -> list[str]:
    """Replay the run from the documented cuts alone; returns the problems found (empty list = all clear).

    Checks: stock never negative, no donor left short of its own pending demand, and every order that the
    plan without cutting released is still released.
    """
    problems = []
    demand = orders.set_index("order_id")["demand"].to_dict()
    left = dict(stock.quantities)
    for row in result.itertuples():
        for cut in cuts[cuts["order_id"] == row.order_id].itertuples():
            left[cut.donor] -= cut.panels_cut
            left[cut.need_size] = left.get(cut.need_size, 0) + cut.pieces_made
            if cut.offcut_size:
                left[cut.offcut_size] = left.get(cut.offcut_size, 0) + cut.panels_cut
            if cut.donor_margin < 0:
                problems.append(f"{row.order_id}: the cut from {cut.donor} leaves the future short ({cut.donor_margin})")
        if row.status == RELEASED:
            for size, qty in demand[row.order_id].items():
                left[size] -= qty
        negative = {s: q for s, q in left.items() if q < 0}
        if negative:
            problems.append(f"{row.order_id}: negative stock {negative}")
            break
    released = set(result.loc[result["status"] == RELEASED, "order_id"])
    harmed = set(baseline.loc[baseline["status"] == RELEASED, "order_id"]) - released
    problems += [f"{o}: released without cutting but stuck with cutting" for o in sorted(harmed)]
    return problems


def cut_report(cuts: pd.DataFrame) -> list[str]:
    """The operational recommendations, in release order."""
    return [f"{row.date:%d/%m/%Y} | הזמנה {row.order_id} | {row.instruction}" for row in cuts.itertuples()]
