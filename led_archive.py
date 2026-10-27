"""Local archive of the parsed LED reports and warehouse lists.

Every report or list that is read is saved here once (identified by the hash of the original file), so a
previous month can be opened again without uploading the PDF, and the app can start on the most recent data.
Nothing is deleted by the app. Files live in `data/archive/` next to the app (the `data/` folder is not part
of the git repository) as one CSV per file plus an `index.json`.

On a cloud host the disk is temporary, so export_zip() / import_zip() make a backup that can be restored.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from led import DATA_DIR

ARCHIVE_DIR = DATA_DIR / "archive"
KINDS = ("reports", "warehouse")

_INT = {"reports": ["code", "length_mm", "quantity", "page"], "warehouse": ["unit_count", "row"]}
_BOOL = {"reports": ["ordered", "received", "supplied"], "warehouse": ["parse_ok"]}
_FLOAT = {"reports": [], "warehouse": ["total_w"]}
_DATE = {"reports": ["order_date"], "warehouse": []}
_ALLOWED_NAME = re.compile(
    r"^(archive/(reports|warehouse)/[A-Za-z0-9_-]+\.csv|archive/index\.json|led_tracking\.json|led_settings\.json)$")
MAX_IMPORT_BYTES = 50 * 1024 * 1024


class ArchiveError(RuntimeError):
    """The archive could not be written (read-only disk) or a backup is not valid."""


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Archive:
    def __init__(self, root: Path = ARCHIVE_DIR):
        self.root = Path(root)

    # ---- index
    @property
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _read_index(self) -> dict:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return {kind: list(raw.get(kind, [])) for kind in KINDS}

    def _write_index(self, index: dict) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self._index_path.with_suffix(".tmp")
            temp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            temp.replace(self._index_path)
        except OSError as exc:
            raise ArchiveError("אי אפשר לכתוב לדיסק בסביבה הזו") from exc

    def entries(self, kind: str) -> list[dict]:
        """Saved items of one kind, newest first."""
        return sorted(self._read_index()[kind], key=lambda e: (e["saved_at"], e["id"]), reverse=True)

    def is_empty(self) -> bool:
        return not any(self._read_index()[kind] for kind in KINDS)

    # ---- save / load
    def save(self, kind: str, name: str, frame: pd.DataFrame, digest: str, batch: str | None = None):
        """Save a parsed file once. Returns (entry, is_new); a file with the same hash is not saved twice."""
        index = self._read_index()
        existing = next((e for e in index[kind] if e["sha256"] == digest), None)
        if existing:
            return existing, False
        now = datetime.now()
        entry_id = f"{now:%Y%m%d-%H%M%S-%f}-{digest[:8]}"
        entry = {"id": entry_id, "name": name, "saved_at": now.isoformat(timespec="microseconds"),
                 "batch": batch or f"{now:%Y%m%d-%H%M%S-%f}", "sha256": digest, "rows": int(len(frame))}
        if kind == "reports" and len(frame):
            entry.update(period_start=frame["order_date"].min().strftime("%Y-%m-%d"),
                         period_end=frame["order_date"].max().strftime("%Y-%m-%d"),
                         quantity=int(frame["quantity"].sum()))
        try:
            (self.root / kind).mkdir(parents=True, exist_ok=True)
            frame.to_csv(self.root / kind / f"{entry_id}.csv", index=False, encoding="utf-8-sig")
        except OSError as exc:
            raise ArchiveError("אי אפשר לכתוב לדיסק בסביבה הזו") from exc
        index[kind].append(entry)
        self._write_index(index)
        return entry, True

    def load(self, kind: str, entry_id: str) -> pd.DataFrame:
        frame = pd.read_csv(self.root / kind / f"{entry_id}.csv", dtype=str, keep_default_na=False,
                            encoding="utf-8-sig")
        for column in _INT[kind]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
        for column in _FLOAT[kind]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        for column in _BOOL[kind]:
            frame[column] = frame[column].str.lower().eq("true")
        for column in _DATE[kind]:
            frame[column] = pd.to_datetime(frame[column])
        return frame

    def latest_batch(self, kind: str) -> list[dict]:
        """The items saved together in the most recent upload."""
        entries = self.entries(kind)
        return [e for e in entries if e["batch"] == entries[0]["batch"]] if entries else []

    # ---- backup
    def export_zip(self) -> bytes:
        """The archive plus the saved settings and tracking, as one zip."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            if self.root.exists():
                for path in sorted(self.root.rglob("*")):
                    if path.is_file() and path.suffix in (".csv", ".json"):
                        archive.write(path, f"archive/{path.relative_to(self.root).as_posix()}")
            for name in ("led_tracking.json", "led_settings.json"):
                if (self.root.parent / name).exists():
                    archive.write(self.root.parent / name, name)
        return buffer.getvalue()

    def import_zip(self, data: bytes) -> dict:
        """Restore a backup: adds what is missing, never overwrites or deletes. Returns counts."""
        if len(data) > MAX_IMPORT_BYTES:
            raise ArchiveError("הגיבוי גדול מדי")
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ArchiveError("הקובץ אינו גיבוי תקין") from exc
        names = archive.namelist()
        if any(not _ALLOWED_NAME.match(n) for n in names):
            raise ArchiveError("הגיבוי מכיל קבצים לא צפויים ולכן לא נטען")
        index = self._read_index()
        restored_index = json.loads(archive.read("archive/index.json")) if "archive/index.json" in names else {}
        added = 0
        for kind in KINDS:
            known = {e["id"] for e in index[kind]}
            for entry in restored_index.get(kind, []):
                member = f"archive/{kind}/{entry['id']}.csv"
                if entry["id"] in known or member not in names:
                    continue
                path = self.root / kind / f"{entry['id']}.csv"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(member))
                except OSError as exc:
                    raise ArchiveError("אי אפשר לכתוב לדיסק בסביבה הזו") from exc
                index[kind].append(entry)
                added += 1
        self._write_index(index)
        extras = 0
        for name in ("led_tracking.json", "led_settings.json"):
            target = self.root.parent / name
            if name in names and not target.exists():
                target.write_bytes(archive.read(name))
                extras += 1
        return {"added": added, "files": extras}
