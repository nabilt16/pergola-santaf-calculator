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
  * a transformer is loaded to at most 80 % of its rating (60 W -> 48 W), so the longest strip
    (4000 mm, 48 W) just fits a 60 W transformer such as the ENERLED NR-60-24-CD;
  * all strips of one order (and code) are bundled on one central transformer, sized from the sum of
    their lengths (9.5 m = 114 W needs a 250 W unit when 150 W is not stocked); only a load above
    80 % of the largest transformer is shared between several.

To confirm: TRANSFORMER_RATINGS is the list of ratings in stock. Only 60 W and 250 W were named;
100 W and 500 W complete the 60 W - 500 W range. Edit it in the LED settings tab.
"""
from __future__ import annotations

import json
import math
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

TRANSFORMER_RATINGS = (60, 100, 250, 500)          # to confirm against the real stock
WATTS_PER_METER = 12.0                              # COB strip, about 180 LEDs per meter
DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class LedSettings:
    """Tunable rules. Persisted by save_settings(); the defaults match the working assumptions."""
    zone_regions: dict = field(default_factory=dict)      # zone code ("20") -> region
    max_strip_mm: int = 4000
    min_strip_mm: int = 1000
    transformer_ratings: tuple = TRANSFORMER_RATINGS
    safety_factor: float = 0.8
    watts_per_meter: float = WATTS_PER_METER


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


def plan_transformers(piece_loads: list[float], ratings=TRANSFORMER_RATINGS,
                      safety_factor: float = 0.8) -> list[dict]:
    """Share strips between transformers when one is not enough.

    First-fit decreasing: every group stays within the usable capacity of the largest rating, then
    each group gets the smallest rating that covers it. Returns [{rating, load_w, strips}].
    """
    capacity = usable_watts(max(ratings), safety_factor)
    groups: list[list[float]] = []
    for load in sorted(piece_loads, reverse=True):
        target = next((g for g in groups if sum(g) + load <= capacity + 1e-9), None)
        if target is None:
            groups.append([load])
        else:
            target.append(load)
    plan = []
    for group in groups:
        rating = choose_transformer(sum(group), ratings, safety_factor)
        limit = usable_watts(rating, safety_factor) if rating else None
        plan.append({
            "rating": rating, "load_w": round(sum(group), 1), "strips": len(group),
            "headroom_w": round(limit - sum(group), 1) if limit else None,
            "load_pct": round(sum(group) / limit * 100, 1) if limit else None,   # of the 80 % limit
        })
    return plan


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
    plan = plan_transformers(loads, settings.transformer_ratings, settings.safety_factor) if loads else []
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
            "transformers": " + ".join(f"{t['rating']}W" if t["rating"] else "מעל הטווח"
                                       for t in kit["transformers"]),
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
    out = assign_regions(lines, settings.zone_regions)
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
        out["line_key"] = line_keys(out)
        for name, default in TRACK_FIELDS.items():
            out[name] = [records.get(k, {}).get(name, default) for k in out["line_key"]]
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
        Path(path).write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def load_settings(path: Path = DATA_DIR / "led_settings.json") -> LedSettings:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return LedSettings()
    known = {k: v for k, v in raw.items() if k in LedSettings.__dataclass_fields__}
    if "transformer_ratings" in known:
        known["transformer_ratings"] = tuple(known["transformer_ratings"])
    return replace(LedSettings(), **known)
