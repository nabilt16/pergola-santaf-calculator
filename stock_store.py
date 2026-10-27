"""Persistence for the Santaf stock plan: the last stock file, the order reports and the settings.

Everything is kept in `data/stock/` (PERGOLA_DATA_DIR moves `data/`), so a browser refresh or a restart of the
app does not ask for the files again. Order reports accumulate: a newly read report is merged with the saved
ones (a line shared by overlapping files counts once). Writes are atomic (temp file, then replace).

On a cloud host the disk is temporary, so export_zip() / import_zip() make a backup that can be restored.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

import calculator as calc
import stock_plan as sp

NAMES = ("stock.json", "orders.csv", "orders_files.json", "settings.json", "last_run.json", "last_cuts.csv")
MAX_IMPORT_BYTES = 50 * 1024 * 1024

DEFAULT_SETTINGS = {
    "start": sp.SIM_START.strftime("%Y-%m-%d"), "end": sp.SIM_END.strftime("%Y-%m-%d"),
    "skip_stuck": True, "kerf_mm": 0, "buffer_days": 14, "zero_sizes": [1000],
}


class StoreError(RuntimeError):
    """The data could not be written (read-only disk) or a backup is not valid."""


def default_dir() -> Path:
    base = Path(os.environ.get("PERGOLA_DATA_DIR") or (Path(__file__).resolve().parent / "data"))
    return base / "stock"


def merge_orders(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Union of order lines: a line in several files counts once, a line repeated inside one file keeps its repeats."""
    columns = list(frames[0].columns)
    numbered = [f.assign(_n=f.groupby(columns, dropna=False).cumcount()) for f in frames]
    combined = pd.concat(numbered, ignore_index=True)
    return combined.drop_duplicates(subset=[*columns, "_n"]).drop(columns="_n").reset_index(drop=True)


class StockStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_dir()

    def _write(self, name: str, text: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self.root / f"{name}.tmp"
            temp.write_text(text, encoding="utf-8")
            temp.replace(self.root / name)
        except OSError as exc:
            raise StoreError("אי אפשר לכתוב לדיסק בסביבה הזו") from exc

    def _read_json(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    # ---- stock file
    def save_stock(self, stock: sp.Stock, source_name: str) -> None:
        self._write("stock.json", json.dumps({
            "quantities": {str(k): v for k, v in stock.quantities.items()},
            "as_of": stock.as_of.strftime("%Y-%m-%d") if stock.as_of is not None else None,
            "zero_added": stock.zero_added, "source": source_name,
            "saved_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=1))

    def load_stock(self) -> tuple[sp.Stock, dict] | None:
        raw = self._read_json("stock.json", None)
        if not raw or not raw.get("quantities"):
            return None
        stock = sp.Stock({int(k): int(v) for k, v in raw["quantities"].items()},
                         pd.Timestamp(raw["as_of"]) if raw.get("as_of") else None, list(raw.get("zero_added", [])))
        return stock, {"source": raw.get("source", ""), "saved_at": raw.get("saved_at", "")}

    # ---- orders
    def save_orders(self, frame: pd.DataFrame, file_name: str) -> int:
        """Merge a newly read report into the saved orders. Returns how many lines were new."""
        frame = calc.normalize_columns(frame).astype(str).reset_index(drop=True)
        saved = self.load_orders()
        merged = merge_orders([saved, frame]) if saved is not None else frame
        added = len(merged) - (len(saved) if saved is not None else 0)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self.root / "orders.csv.tmp"
            merged.to_csv(temp, index=False, encoding="utf-8-sig")
            temp.replace(self.root / "orders.csv")
        except OSError as exc:
            raise StoreError("אי אפשר לכתוב לדיסק בסביבה הזו") from exc
        files = self._read_json("orders_files.json", [])
        files.append({"name": file_name, "rows": int(len(frame)), "new_rows": int(added),
                      "saved_at": datetime.now().isoformat(timespec="seconds")})
        self._write("orders_files.json", json.dumps(files, ensure_ascii=False, indent=1))
        return added

    def load_orders(self) -> pd.DataFrame | None:
        try:
            frame = pd.read_csv(self.root / "orders.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        except (OSError, ValueError):
            return None
        return frame if len(frame) else None

    def orders_files(self) -> list[dict]:
        return self._read_json("orders_files.json", [])

    # ---- settings and the last run
    def load_settings(self) -> dict:
        saved = self._read_json("settings.json", {})
        merged = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
        try:
            pd.Timestamp(merged["start"]), pd.Timestamp(merged["end"])
        except (ValueError, TypeError):
            merged["start"], merged["end"] = DEFAULT_SETTINGS["start"], DEFAULT_SETTINGS["end"]
        return merged

    def save_settings(self, settings: dict) -> None:
        if settings != self.load_settings():
            self._write("settings.json", json.dumps(settings, ensure_ascii=False, indent=1))

    def save_run(self, summary: dict, cuts: pd.DataFrame) -> None:
        """The last computed plan (summary and the cut instructions), kept next to the inputs."""
        if summary == self._read_json("last_run.json", None) and (self.root / "last_cuts.csv").exists():
            return
        self._write("last_run.json", json.dumps(summary, ensure_ascii=False, indent=1))
        self._write("last_cuts.csv", cuts.to_csv(index=False))

    def last_run(self) -> dict | None:
        return self._read_json("last_run.json", None)

    # ---- backup
    def export_zip(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in NAMES:
                if (self.root / name).exists():
                    archive.write(self.root / name, f"stock/{name}")
        return buffer.getvalue()

    def import_zip(self, data: bytes) -> list[str]:
        """Restore a backup (replaces the saved stock plan files). Returns the names restored."""
        if len(data) > MAX_IMPORT_BYTES:
            raise StoreError("הגיבוי גדול מדי")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise StoreError("הקובץ אינו גיבוי תקין") from exc
        wanted = {f"stock/{name}" for name in NAMES}
        found = [n for n in archive.namelist() if n in wanted]
        if not found:
            raise StoreError("בגיבוי אין נתוני מלאי")
        restored = []
        for name in found:
            self._write(name.split("/", 1)[1], archive.read(name).decode("utf-8-sig" if name.endswith(".csv") else "utf-8"))
            restored.append(name.split("/", 1)[1])
        return restored
