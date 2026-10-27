"""Find the LED order reports and the LED/transformer warehouse list on this computer and save them to the archive.

Looks (not recursively) in the Downloads folder, its "פרגולות" subfolder, the working folder and the app folder - or in
the folders of PERGOLA_INBOX_DIRS - for:
  * LED reports: .pdf files whose name has "לדים" (without "במחסן");
  * the warehouse list: .xlsx/.xls/.csv whose name has "לדים" and "במחסן".
Each file is saved to the archive once (led_archive identifies it by the hash of the file), so scanning again changes
nothing unless a new or changed file appears.

Command line:  python led_autoload.py [folder ...]
"""
from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import led_archive
import led_ingest
import warehouse

WAREHOUSE_SUFFIXES = (".xlsx", ".xls", ".csv")


@dataclass
class LedAutoloadReport:
    reports: list[str] = field(default_factory=list)       # LED reports saved this time
    warehouse: list[str] = field(default_factory=list)     # warehouse lists saved this time
    unchanged: int = 0                                      # matching files that were already in the archive
    errors: dict[str, str] = field(default_factory=dict)
    searched: list[str] = field(default_factory=list)
    new_ids: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.reports or self.warehouse)


def default_dirs() -> list[Path]:
    env = os.environ.get("PERGOLA_INBOX_DIRS")
    if env:
        dirs = [Path(d) for d in env.split(os.pathsep) if d]
    else:
        downloads = Path.home() / "Downloads"
        dirs = [downloads, downloads / "פרגולות", Path.cwd(), Path(__file__).resolve().parent]
    seen, out = set(), []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def find_files(dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    """(LED report PDFs, warehouse lists) by file name, oldest first."""
    reports, lists = [], []
    for directory in dirs:
        try:
            entries = [e for e in Path(directory).iterdir() if e.is_file() and not e.name.startswith("~$")]
        except OSError:
            continue
        for path in entries:
            name, suffix = path.name, path.suffix.lower()
            if "לדים" not in name:
                continue
            if "במחסן" in name and suffix in WAREHOUSE_SUFFIXES:
                lists.append(path)
            elif suffix == ".pdf" and "במחסן" not in name:
                reports.append(path)
    by_age = lambda p: p.stat().st_mtime
    return sorted(reports, key=by_age), sorted(lists, key=by_age)


def scan(archive: led_archive.Archive | None = None, dirs: list[Path] | None = None) -> LedAutoloadReport:
    """Save what is new in the folders to the archive. Never raises for a bad file; it is listed in `errors`."""
    archive = archive or led_archive.Archive()
    dirs = dirs if dirs is not None else default_dirs()
    result = LedAutoloadReport(searched=[str(d) for d in dirs])
    reports, lists = find_files(dirs)
    batch = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for path in reports:
        data = path.read_bytes()
        digest = led_archive.file_hash(data)
        if any(e["sha256"] == digest for e in archive.entries("reports")):
            result.unchanged += 1
            continue
        try:
            _, report = led_ingest.load_led_pdfs([(path.name, io.BytesIO(data))])
            for name, frame in report.frames.items():
                entry, is_new = archive.save("reports", name, frame, digest, batch)
                if is_new:
                    result.reports.append(path.name)
                    result.new_ids.append(entry["id"])
            for name, why in report.errors.items():
                result.errors[name] = why
        except Exception as exc:                              # a bad file must not stop the others
            result.errors[path.name] = str(exc)
    for path in lists:
        data = path.read_bytes()
        digest = led_archive.file_hash(data)
        if any(e["sha256"] == digest for e in archive.entries("warehouse")):
            result.unchanged += 1
            continue
        try:
            frame = warehouse.load_warehouse(path, path.name)
            _, is_new = archive.save("warehouse", path.name, frame, digest)
            if is_new:
                result.warehouse.append(path.name)
        except Exception as exc:
            result.errors[path.name] = str(exc)
    return result


def describe(result: LedAutoloadReport) -> str:
    parts = []
    if result.reports:
        parts.append(f"דוחות לדים: {', '.join(result.reports)}")
    if result.warehouse:
        parts.append(f"רשימת מחסן: {', '.join(result.warehouse)}")
    if not parts:
        parts.append(f"לא נמצאו קבצים חדשים ({result.unchanged} כבר שמורים)" if result.unchanged
                     else "לא נמצאו קבצי לדים בתיקיות")
    return " · ".join(parts)


if __name__ == "__main__":
    folders = [Path(a) for a in sys.argv[1:]] or None
    outcome = scan(dirs=folders)
    print("searched:", *outcome.searched, sep="\n  ")
    print(describe(outcome))
    for name, why in outcome.errors.items():
        print(f"ERROR {name}: {why}")
