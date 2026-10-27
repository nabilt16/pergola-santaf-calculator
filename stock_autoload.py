"""Find the Santaf stock file and order reports on this computer and load them without uploading.

Looks (not recursively) in the Downloads folder, the working folder and the app folder - or in the folders of
PERGOLA_INBOX_DIRS (separated by the OS path separator) - for:
  * a stock sheet: .xls/.xlsx whose name has "מלא" and "סנטפ" (the newest one counts);
  * order reports: .pdf/.xls/.xlsx/.csv whose name has "דוח" and "סנטפ".
Every file used is copied to `data/stock/inbox/` and read into the stock store (stock_store). A file is read once:
its hash is remembered, so scanning again changes nothing unless a new or changed file appears.

Command line:  python stock_autoload.py [folder ...]
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import calculator as calc
import pdf_ingest
import stock_plan as sp
import stock_store

SUFFIXES_STOCK = (".xlsx", ".xls")
SUFFIXES_ORDERS = (".pdf", ".xlsx", ".xls", ".csv")
STATE = "autoload.json"


@dataclass
class AutoloadReport:
    stock: str | None = None                                  # stock file read this time
    orders: list[str] = field(default_factory=list)           # order reports read this time
    new_rows: int = 0
    unchanged: int = 0                                        # matching files already read before
    errors: dict[str, str] = field(default_factory=dict)      # file -> why it was skipped
    searched: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.stock or self.orders)


def default_dirs() -> list[Path]:
    env = os.environ.get("PERGOLA_INBOX_DIRS")
    if env:
        dirs = [Path(d) for d in env.split(os.pathsep) if d]
    else:
        dirs = [Path.home() / "Downloads", Path.cwd(), Path(__file__).resolve().parent]
    seen, out = set(), []
    for d in dirs:
        key = str(d.resolve()) if d.exists() else str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_files(dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    """(stock candidates, order-report candidates) by file name, newest first."""
    stock, orders = [], []
    for directory in dirs:
        try:
            entries = [e for e in Path(directory).iterdir() if e.is_file() and not e.name.startswith("~$")]
        except OSError:
            continue
        for path in entries:
            name, suffix = path.name, path.suffix.lower()
            if "סנטפ" not in name:
                continue
            if "מלא" in name and suffix in SUFFIXES_STOCK:
                stock.append(path)
            elif "דוח" in name and suffix in SUFFIXES_ORDERS:
                orders.append(path)
    newest = lambda p: p.stat().st_mtime
    return sorted(stock, key=newest, reverse=True), sorted(orders, key=newest)


def _read_orders(path: Path):
    if path.suffix.lower() == ".pdf":
        with open(path, "rb") as handle:
            frame, report = pdf_ingest.load_many([(path.name, handle)])
        if report.errors:
            raise ValueError("; ".join(report.errors.values()))
        return frame
    return calc.load_orders(path)


def _copy(store: stock_store.StockStore, path: Path) -> None:
    inbox = store.root / "inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        target = inbox / path.name
        if not (target.exists() and target.resolve() == path.resolve()):
            shutil.copy2(path, target)
    except OSError:
        pass                                                   # the copy is a convenience; the data is already saved


def scan(store: stock_store.StockStore | None = None, dirs: list[Path] | None = None,
         zero_sizes: tuple[int, ...] = (1000,)) -> AutoloadReport:
    """Read what is new in the folders into the store. Never raises for a bad file; it is listed in `errors`."""
    store = store or stock_store.StockStore()
    dirs = dirs if dirs is not None else default_dirs()
    report = AutoloadReport(searched=[str(d) for d in dirs])
    try:
        state = json.loads((store.root / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("orders", [])
    stock_files, order_files = find_files(dirs)

    for path in stock_files[:1]:                               # only the newest stock sheet counts
        digest = _sha(path)
        if state.get("stock") == digest and store.load_stock() is not None:
            report.unchanged += 1
            break
        try:
            stock = sp.load_stock(path, add_zero_sizes=tuple(zero_sizes))
            store.save_stock(stock, path.name)
        except (ValueError, OSError, stock_store.StoreError) as exc:
            report.errors[path.name] = str(exc)
            break
        state["stock"] = digest
        report.stock = path.name
        _copy(store, path)

    for path in order_files:
        digest = _sha(path)
        if digest in state["orders"]:
            report.unchanged += 1
            continue
        try:
            report.new_rows += store.save_orders(_read_orders(path), path.name)
        except Exception as exc:                               # a bad file must not stop the others
            report.errors[path.name] = str(exc)
            continue
        state["orders"].append(digest)
        report.orders.append(path.name)
        _copy(store, path)

    if report.changed:
        try:
            store._write(STATE, json.dumps(state, ensure_ascii=False, indent=1))
        except stock_store.StoreError as exc:
            report.errors["state"] = str(exc)
    return report


def describe(report: AutoloadReport) -> str:
    parts = []
    if report.stock:
        parts.append(f"מלאי: {report.stock}")
    if report.orders:
        parts.append(f"הזמנות: {', '.join(report.orders)} ({report.new_rows} שורות חדשות)")
    if not parts:
        parts.append(f"לא נמצאו קבצים חדשים ({report.unchanged} כבר נטענו)" if report.unchanged
                     else "לא נמצאו קבצי מלאי או הזמנות סנטפים בתיקיות")
    return " · ".join(parts)


if __name__ == "__main__":
    folders = [Path(a) for a in sys.argv[1:]] or None
    result = scan(dirs=folders)
    print("searched:", *result.searched, sep="\n  ")
    print(describe(result))
    for name, why in result.errors.items():
        print(f"ERROR {name}: {why}")
