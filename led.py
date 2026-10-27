"""LED lighting rules: order types, regions, strip splitting, transformer sizing, tracking.

Everything here works on the table produced by led_ingest.load_led_pdfs() and is independent of the
Santaf calculator. Today kits are still ordered externally per order; the kit and transformer
functions only compute what a future central inventory would need, so the data fields, code
filters (6 vs 67) and status tracking are ready for that transition.

Specification (all of it is plain parameters in LedSettings):
  * the strips are high-density COB strips (about 180 LEDs per meter) drawing 12 W per meter, so a
    4000 mm strip is 48 W net;
  * LED strips are stocked from 1000 mm to 4000 mm; a longer length is cut into pieces of at most
    4000 mm and no piece is shorter than 1000 mm;
  * transformers are the sizes of the warehouse (60, 100, 150, 200, 250, 500 W) and each is loaded to at
    most 80 % of its rating (60 W -> 48 W): a 4000 mm strip just fits a 60 W unit such as the ENERLED
    NR-60-24-CD;
  * the strips of one order (and code) share the transformers of ONE allocation sized from the sum of
    their lengths: the cheapest combination of stock sizes (least total rating, then fewest units) whose
    safe capacity carries the load, written like the warehouse list: 1*150W, 1*100W+1*150W, 3*150W;
  * zone codes map to regions (20 = חיפה / צפון, 50 = שדרות / דרום, 55 = באר שבע / דרום, 70 = מרכז,
    80 = אילת / דרום).

The warehouse allocation (warehouse.py) is the real record; this module's allocation is a recommendation
that is compared with it, so the rule can be calibrated as more of the warehouse list arrives.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pandas as pd

CODE_FULL = 6
CODE_SERVICE = 67
ORDER_TYPES = {CODE_FULL: "פרגולה מלאה", CODE_SERVICE: "תיקון / שירות / תוספת"}

REGIONS = ("צפון", "מרכז", "דרום")
UNASSIGNED = "לא משויך"

STATUS_LABELS = {
    "transferred": "הועבר למפעל", "in_production": "בייצור", "produced": "מיוצר",
    "at_factory": "במפעל", "sizes_not_final": "מידות לא סופיות", "in_warehouse": "במחסן",
    "cancelled": "בוטל", "other": "אחר", "": "",
}

STAGES = ("טרם הוזמן", "הוזמן", "נתקבל", "סופק")

TRANSFORMER_RATINGS = (60, 100, 150, 200, 250, 500)   # the sizes found in the warehouse records
WATTS_PER_METER = 12.0                                  # COB strip, about 180 LEDs per meter
DEFAULT_ZONE_REGIONS = {"20": "צפון", "50": "דרום", "55": "דרום", "70": "מרכז", "80": "דרום"}
DEFAULT_ZONE_AREAS = {"20": "חיפה", "50": "שדרות", "55": "באר שבע", "70": "מרכז", "80": "אילת"}
# Where the app keeps what it saves (settings, tracking, the archive). PERGOLA_DATA_DIR moves it, e.g. for tests.
DATA_DIR = Path(os.environ.get("PERGOLA_DATA_DIR") or (Path(__file__).resolve().parent / "data"))


@dataclass(frozen=True)
class LedSettings:
    """Tunable rules. Persisted by save_settings(); the defaults match the working assumptions."""
    zone_regions: dict = field(default_factory=dict)      # zone code ("20") -> region; overrides the defaults
    zone_areas: dict = field(default_factory=dict)        # zone code -> area / city name; overrides the defaults
    max_strip_mm: int = 4000
    min_strip_mm: int = 1000
    transformer_ratings: tuple = TRANSFORMER_RATINGS
    safety_factor: float = 0.8
    watts_per_meter: float = WATTS_PER_METER
    allocation_policy: str = "fewest_units"                # see POLICIES


def zone_regions(settings: "LedSettings") -> dict:
    """Zone code -> region: the built-in mapping with the settings on top ('לא משויך' unassigns a zone)."""
    return {**DEFAULT_ZONE_REGIONS, **settings.zone_regions}


def zone_areas(settings: "LedSettings") -> dict:
    return {**DEFAULT_ZONE_AREAS, **settings.zone_areas}


def order_type(code) -> str:
    """Strict distinction: 6 = full pergola, 67 = repair / service / add-on, anything else flagged."""
    try:
        return ORDER_TYPES.get(int(code), f"אחר (קוד {int(code)})")
    except (TypeError, ValueError):
        return "אחר"


def stage_of(ordered: bool, received: bool, supplied: bool) -> str:
    """Furthest step reached by the ticks of the report: ordered -> received -> supplied."""
    if supplied:
        return "סופק"
    if received:
        return "נתקבל"
    if ordered:
        return "הוזמן"
    return "טרם הוזמן"


# --------------------------------------------------------------------------
# Strips: lengths above the stock maximum are split
# --------------------------------------------------------------------------
def split_led_length(length_mm: int, max_len: int = 4000, min_piece: int = 1000) -> list[int]:
    """Pieces for one strip of `length_mm`: none longer than `max_len`, none shorter than `min_piece`.

    Full-length pieces first and the remainder last; a remainder under `min_piece` is topped up
    from the piece before it (4200 -> 3200 + 1000, not 4000 + 200). 6000 -> 4000 + 2000,
    12000 -> 4000 x 3. A length that is already short (under `min_piece`) stays one piece, which the
    caller cuts from a stock strip. Length 0 gives no pieces.
    """
    length_mm = int(length_mm)
    if length_mm <= 0:
        return []
    if length_mm <= max_len:
        return [length_mm]
    count = math.ceil(length_mm / max_len)
    pieces = [max_len] * (count - 1) + [length_mm - max_len * (count - 1)]
    if pieces[-1] < min_piece:
        shortfall = min_piece - pieces[-1]
        pieces[-2] -= shortfall
        pieces[-1] += shortfall
    return pieces


# --------------------------------------------------------------------------
# Power: 80 % rule and consumption estimates
# --------------------------------------------------------------------------
def usable_watts(rating_w: float, safety_factor: float = 0.8) -> float:
    """Recommended maximum load of a transformer (60 W -> 48 W)."""
    return rating_w * safety_factor


def strip_watts(length_mm: float, watts_per_meter: float) -> float:
    return length_mm / 1000.0 * watts_per_meter


def choose_transformer(load_w: float, ratings=TRANSFORMER_RATINGS, safety_factor: float = 0.8):
    """Smallest rating whose 80 % capacity covers `load_w`; None if even the largest cannot."""
    for rating in sorted(ratings):
        if usable_watts(rating, safety_factor) >= load_w - 1e-9:      # 48 W exactly fits a 60 W unit
            return rating
    return None


def transformer_table(settings: "LedSettings | None" = None) -> pd.DataFrame:
    """Per rating: the safe load (80 %), how many meters of strip that carries, and how many 4 m strips."""
    settings = settings or LedSettings()
    rows = []
    for rating in sorted(settings.transformer_ratings):
        safe = usable_watts(rating, settings.safety_factor)
        meters = safe / settings.watts_per_meter
        rows.append({"rating_w": rating, "safe_load_w": round(safe, 1), "max_meters": round(meters, 2),
                     "max_full_strips": int(meters * 1000 // settings.max_strip_mm + 1e-9)})
    return pd.DataFrame(rows, columns=["rating_w", "safe_load_w", "max_meters", "max_full_strips"])


_ALLOCATION_PART = re.compile(r"(\d+)\s*[*xX×]\s*(\d+(?:\.\d+)?)\s*[wW]?")


def parse_allocation(text) -> list[tuple[int, float]]:
    """[(count, watts)] from warehouse notation: '1*150W', '1*100W+1*150W', '3*150W'. Empty if not understood."""
    parts = _ALLOCATION_PART.findall(str(text or ""))
    return [(int(count), float(watts)) for count, watts in parts]


def format_allocation(ratings: list) -> str:
    """Units written like the warehouse list: [100, 150] -> '1*100W+1*150W', [150, 150, 150] -> '3*150W'."""
    counts = Counter(r for r in ratings if r)
    text = "+".join(f"{n}*{w:g}W" for w, n in sorted(counts.items()))
    if any(not r for r in ratings):
        return (text + "+" if text else "") + "מעל הטווח"
    return text


def _pack(loads: list[float], capacities: list[float]):
    """Put every strip on one transformer without exceeding its safe load; None if it cannot be done."""
    bins: list[list[float]] = [[] for _ in capacities]
    free = list(capacities)
    for load in sorted(loads, reverse=True):
        order = sorted(range(len(bins)), key=lambda i: -free[i])
        target = next((i for i in order if free[i] + 1e-9 >= load), None)
        if target is None:
            return None
        bins[target].append(load)
        free[target] -= load
    return bins if all(bins) else None


SETTINGS_VERSION = 2     # 2: transformer sizes of the warehouse, zone mapping with cities


POLICIES = {
    "fewest_units": "שנאי מרכזי אחד כשאפשר, צירוף רק כשאין יחידה מספיקה",
    "least_total_rating": "סך הספקים מינימלי (גם אם בכמה יחידות)",
}


def plan_transformers(piece_loads: list[float], ratings=TRANSFORMER_RATINGS, safety_factor: float = 0.8,
                      max_units: int = 6, policy: str = "fewest_units") -> list[dict]:
    """Allocate transformers to the strips of one order from the stock sizes.

    Combinations of up to `max_units` units from `ratings` are tried in the order of the policy:
    "fewest_units" (default) takes a single central transformer whenever one stock size can carry the
    load and a combination only above that; "least_total_rating" takes the smallest total rating, even in
    several units. The first combination whose safe capacity (80 %) holds every strip, each strip on one
    transformer, is returned as [{rating, load_w, strips, headroom_w, load_pct}] (load_pct is relative to
    the safe limit). 60 W carries one 4 m strip; 1*100W+1*150W and 3*150W are the kind of combinations the
    warehouse records show for larger orders.
    """
    if not piece_loads:
        return []
    stock = sorted(set(ratings))
    by_total = policy == "least_total_rating"
    combos = sorted((c for n in range(1, max_units + 1) for c in itertools.combinations_with_replacement(stock, n)),
                    key=(lambda c: (sum(c), len(c), c)) if by_total else (lambda c: (len(c), sum(c), c)))
    total = sum(piece_loads)
    chosen = None
    for combo in combos:
        capacities = [usable_watts(r, safety_factor) for r in combo]
        if sum(capacities) + 1e-9 < total:
            continue
        bins = _pack(piece_loads, capacities)
        if bins:
            chosen = list(zip(combo, bins))
            break
    if chosen is None:                            # beyond the table: as many of the largest unit as needed
        top = max(stock)
        count = math.ceil(total / usable_watts(top, safety_factor) - 1e-9)
        bins = _pack(piece_loads, [usable_watts(top, safety_factor)] * count) or [piece_loads]
        chosen = [(top, b) for b in bins]
    plan = []
    for rating, group in chosen:
        limit = usable_watts(rating, safety_factor)
        plan.append({"rating": rating, "load_w": round(sum(group), 1), "strips": len(group),
                     "headroom_w": round(limit - sum(group), 1),
                     "load_pct": round(sum(group) / limit * 100, 1)})
    return sorted(plan, key=lambda p: p["rating"])


def kit_for_order(lines: pd.DataFrame, settings: LedSettings = LedSettings()) -> dict:
    """What an order's LED lines would need from a central inventory (information only)."""
    pieces: Counter = Counter()
    notes = []
    for row in lines.itertuples():
        if row.length_mm <= 0 or row.quantity <= 0:
            notes.append("חסר אורך או כמות")
            continue
        parts = split_led_length(row.length_mm, settings.max_strip_mm, settings.min_strip_mm)
        if row.length_mm > settings.max_strip_mm:
            notes.append(f"{row.length_mm} מ״מ פוצל ל-{' + '.join(map(str, parts))}")
        if row.length_mm < settings.min_strip_mm:
            notes.append(f"{row.length_mm} מ״מ קצר מ-{settings.min_strip_mm} (נחתך מרצועה מלאה)")
        for part in parts:
            pieces[part] += int(row.quantity)
    meters = sum(length * count for length, count in pieces.items()) / 1000.0
    loads = [strip_watts(length, settings.watts_per_meter)
             for length, count in pieces.items() for _ in range(count)]
    plan = (plan_transformers(loads, settings.transformer_ratings, settings.safety_factor,
                              policy=settings.allocation_policy) if loads else [])
    return {
        "pieces": dict(sorted(pieces.items())), "strips": sum(pieces.values()),
        "meters": round(meters, 2), "watts": round(sum(loads), 1),
        "transformers": plan, "notes": sorted(set(notes)),
    }


def kits_table(lines: pd.DataFrame, settings: LedSettings = LedSettings()) -> pd.DataFrame:
    """One row per order and code with the kit it would need (cancelled lines are left out)."""
    rows = []
    active = lines[lines["status_key"] != "cancelled"]
    for (order_id, code), group in active.groupby(["order_id", "code"], sort=False):
        kit = kit_for_order(group, settings)
        if kit["strips"] == 0:                  # no length / quantity: nothing to prepare (see the issue column)
            continue
        rows.append({
            "order_id": order_id, "code": code, "order_type": order_type(code),
            "region": group["region"].iloc[0] if "region" in group else UNASSIGNED,
            "strips": kit["strips"], "meters": kit["meters"],
            "watts": kit["watts"],
            "load_pct": max((t["load_pct"] for t in kit["transformers"] if t["load_pct"] is not None),
                            default=None),
            "pieces": ", ".join(f"{count}×{length}" for length, count in kit["pieces"].items()),
            "transformers": format_allocation([t["rating"] for t in kit["transformers"]]),
            "notes": "; ".join(kit["notes"]),
        })
    return pd.DataFrame(rows, columns=["order_id", "code", "order_type", "region", "strips", "meters",
                                       "watts", "load_pct", "pieces", "transformers", "notes"])


def inventory_demand(lines: pd.DataFrame, settings: LedSettings = LedSettings()) -> dict:
    """Total demand of the given lines on a future central stock: strips per length and transformers."""
    strips: Counter = Counter()
    transformers: Counter = Counter()
    for _, group in lines[lines["status_key"] != "cancelled"].groupby(["order_id", "code"]):
        kit = kit_for_order(group, settings)
        strips.update({length: count for length, count in kit["pieces"].items()})
        transformers.update(t["rating"] for t in kit["transformers"] if t["rating"])
    return {"strips": dict(sorted(strips.items())), "transformers": dict(sorted(transformers.items()))}


# --------------------------------------------------------------------------
# Regions, issues and the working table
# --------------------------------------------------------------------------
def assign_regions(lines: pd.DataFrame, zone_regions: dict) -> pd.DataFrame:
    """Add `region` from the zone code; zones without a mapping are 'לא משויך'."""
    out = lines.copy()
    out["region"] = out["zone"].map(lambda z: zone_regions.get(str(z)) or UNASSIGNED)
    return out


def line_issue(length_mm: int, quantity: int, status_key: str, settings: LedSettings) -> str:
    notes = []
    if status_key == "cancelled":
        notes.append("בוטל")
    if length_mm == 0 and quantity == 0:
        notes.append("אין פרטי לד")
    elif length_mm == 0:
        notes.append("חסר אורך")
    elif quantity == 0:
        notes.append("חסרה כמות")
    if length_mm > settings.max_strip_mm:
        notes.append(f"מעל {settings.max_strip_mm} - יפוצל")
    if 0 < length_mm < settings.min_strip_mm:
        notes.append(f"קצר מ-{settings.min_strip_mm}")
    if status_key == "sizes_not_final":
        notes.append("מידות לא סופיות")
    return "; ".join(notes)


def prepare(lines: pd.DataFrame, settings: LedSettings = LedSettings()) -> pd.DataFrame:
    """The working table: regions, order type, stage, meters and issues added to the report lines."""
    out = assign_regions(lines, zone_regions(settings))
    areas = zone_areas(settings)
    out["area"] = out["zone"].map(lambda z: areas.get(str(z), ""))
    out["order_type"] = out["code"].map(order_type)
    out["status_he"] = out["status_key"].map(lambda k: STATUS_LABELS.get(k, "אחר"))
    out["stage"] = [stage_of(o, r, s) for o, r, s in zip(out["ordered"], out["received"], out["supplied"])]
    out["meters"] = out["length_mm"] * out["quantity"] / 1000.0
    out["issue"] = [line_issue(l, q, s, settings)
                    for l, q, s in zip(out["length_mm"], out["quantity"], out["status_key"])]
    return out


def summarize(lines: pd.DataFrame) -> pd.DataFrame:
    """Orders, lines, strips and meters, kept apart by order type (6 and 67 are never mixed)."""
    out = lines.groupby("order_type").agg(
        orders=("order_id", "nunique"), lines=("order_id", "size"),
        strips=("quantity", "sum"), meters=("meters", "sum")).reset_index()
    out["meters"] = out["meters"].round(1)
    return out


# --------------------------------------------------------------------------
# Tracking: manual fields kept per line, saved to a file next to the app
# --------------------------------------------------------------------------
TRACK_FIELDS = {"kit_ordered": False, "kit_ref": "", "tracking_note": ""}


def line_keys(lines: pd.DataFrame) -> list[str]:
    """A stable key per report line: order, code, length, quantity and which repeat it is."""
    seen: Counter = Counter()
    keys = []
    for row in lines.itertuples():
        base = f"{row.order_id}|{row.code}|{row.length_mm}|{row.quantity}"
        keys.append(f"{base}|{seen[base]}")
        seen[base] += 1
    return keys


class TrackingStore:
    """JSON file of manual tracking fields (external kit ordered, reference, note) per line key."""

    def __init__(self, path: Path = DATA_DIR / "led_tracking.json"):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, records: dict) -> bool:
        """True when written; False when the disk is read-only (some hosts), nothing is lost."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
            return True
        except OSError:
            return False

    def apply(self, lines: pd.DataFrame, records: dict | None = None) -> pd.DataFrame:
        """Add the tracking columns to `lines` (defaults for lines never edited)."""
        records = self.load() if records is None else records
        out = lines.copy()
        out["line_key"] = pd.Series(line_keys(out), index=out.index, dtype=object)
        for name, default in TRACK_FIELDS.items():
            values = [records.get(k, {}).get(name, default) for k in out["line_key"]]
            missing = [v is None or v != v for v in values]                 # None / NaN in a saved file
            if isinstance(default, bool):
                # st.data_editor only accepts a checkbox column that is really boolean, even when it is empty
                out[name] = pd.Series([default if m else bool(v) for v, m in zip(values, missing)],
                                      index=out.index, dtype=bool)
            else:
                out[name] = pd.Series(["" if m else str(v) for v, m in zip(values, missing)],
                                      index=out.index, dtype=object)
        return out

    @staticmethod
    def collect(edited: pd.DataFrame) -> dict:
        """Records worth keeping from an edited table (lines still at the defaults are dropped)."""
        records = {}
        for row in edited.itertuples():
            rec = {name: getattr(row, name) for name in TRACK_FIELDS}
            if any(rec[name] != default for name, default in TRACK_FIELDS.items()):
                records[row.line_key] = {n: (bool(v) if isinstance(TRACK_FIELDS[n], bool) else str(v))
                                         for n, v in rec.items()}
        return records


def save_settings(settings: LedSettings, path: Path = DATA_DIR / "led_settings.json") -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SETTINGS_VERSION, **asdict(settings)}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def load_settings(path: Path = DATA_DIR / "led_settings.json") -> LedSettings:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LedSettings()
    if raw.get("version", 1) < SETTINGS_VERSION:
        # saved before the transformer sizes and the zone mapping were fixed by specification: those
        # three would override the specification with old values, so they are taken from the defaults
        for stale in ("transformer_ratings", "zone_regions", "zone_areas"):
            raw.pop(stale, None)
    known = {k: v for k, v in raw.items() if k in LedSettings.__dataclass_fields__}
    if "transformer_ratings" in known:
        known["transformer_ratings"] = tuple(known["transformer_ratings"])
    return replace(LedSettings(), **known)
