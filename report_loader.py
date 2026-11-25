"""Reading uploaded Santaf order reports (PDF) in a way that survives Streamlit reruns and reconnects.

Why this exists. Every interaction makes Streamlit run the whole script again, and the PDF used to be parsed again on
every run, with nothing on the screen while it was being read. On a slow host or a phone that takes seconds: the
page looks as if the upload did not register, the file gets uploaded again (two or three times), and each new
upload or click restarts the reading. Here:

* each file is parsed once, by its content (st.cache_data), so a rerun costs nothing;
* the parsed report is kept in st.session_state under `state_key`, and a copy is saved on disk, so a rerun, a
  widget that was reset, or a new session after a lost connection does not need the file again;
* a status box shows the reading while it happens, and the success message appears only after the result was checked
  (rows found, readable lengths and quantities, printed totals compared);
* identical files count once; uploading a different file replaces the previous report.

The pure part (`load`) has no Streamlit dependency apart from the cache decorator.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import streamlit as st

import pdf_ingest
import upload_diag



@dataclass
class LoadedReport:
    raw_all: pd.DataFrame                  # every row read (status, page, line, item_number, source_file included)
    raw: pd.DataFrame                      # the rows that have a readable length and quantity
    names: list[str]
    signature: tuple
    load_report: pdf_ingest.LoadReport
    left_out: int = 0                      # rows without a readable length / quantity
    same_content: list[str] = field(default_factory=list)      # files skipped because an identical one was given
    origin: str = "upload"                 # upload | session | disk

    @property
    def source_name(self) -> str:
        loaded = self.load_report.loaded
        return loaded[0] if len(loaded) == 1 else f"{len(loaded)} PDF reports"

    @property
    def source_id(self) -> str:
        return "|".join(f"{n}:{s}" for n, s, _ in self.signature)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def signature(files: list[tuple[str, bytes]]) -> tuple:
    """(name, size, content hash) of each file: the identity of an upload."""
    return tuple(sorted((name, len(data), hashlib.sha1(data).hexdigest()[:16]) for name, data in files))


@st.cache_data(show_spinner=False, max_entries=16)
def _parse(data: bytes) -> tuple[pd.DataFrame, dict]:
    """One PDF, by its content: the rows (with details) and what the report prints about itself."""
    frame = pdf_ingest.load_pdf(io.BytesIO(data), details=True)
    return frame, {"footer_matches": bool(frame.attrs.get("footer_matches")), "printed": frame.attrs.get("printed")}


def explain(name: str, data: bytes, exc: Exception | None = None) -> str:
    """A clear Hebrew reason a file could not be used."""
    if not data:
        return ("הקובץ התקבל ריק (0 בייט). בטלפון זה קורה כשהקובץ עדיין לא ירד למכשיר או נבחר מענן בלי להיות מקוון: "
                "הורד אותו קודם לזיכרון הטלפון ובחר אותו שוב.")
    if data.lstrip()[:5] != b"%PDF-":
        return "הקובץ אינו PDF תקין (אין לו כותרת PDF). ודא שבחרת את דוח ההזמנות עצמו ולא קובץ אחר."
    text = str(exc) if exc else ""
    if "scan" in text.lower() or "text layer" in text.lower():
        return "ה-PDF הוא סריקה ואין בו שכבת טקסט. השתמש באפשרות ״Scan / photo of a document״."
    if "No order table" in text:
        return "לא נמצאה בקובץ טבלת הזמנות (תאריכים ומספרי הזמנה). ודא שזה דוח הזמנות סנטפים."
    return "הקובץ לא נקרא (פגום, מוגן בסיסמה או לא בפורמט הדוח)." + (f" פירוט: {text}" if text else "")


def load(files: list[tuple[str, bytes]]) -> LoadedReport:
    """Read the given (name, bytes) PDFs. A bad file is reported by name and skipped; if none can be read,
    pdf_ingest.PdfUnreadable is raised with every reason."""
    sig = signature(files)
    report, frames, seen, same = pdf_ingest.LoadReport(), [], {}, []
    for name, data in files:
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen and data:                      # the same file given twice counts once
            same.append(name)
            continue
        seen[digest] = name
        if not data or data.lstrip()[:5] != b"%PDF-":
            report.errors[name] = explain(name, data)
            continue
        try:
            frame, info = _parse(data)
        except pdf_ingest.PdfUnreadable as exc:
            report.errors[name] = explain(name, data, exc)
            continue
        except Exception as exc:                          # unexpected: still a message, never a crash
            report.errors[name] = explain(name, data, exc)
            continue
        frame = frame.assign(source_file=name)
        frames.append(frame)
        report.loaded.append(name)
        report.read[name] = (len(frame), round(float(frame["original_quantity"].sum()), 4))
        (report.confirmed if info["footer_matches"] else report.unconfirmed).append(name)
        if info["printed"]:
            report.printed[name] = tuple(info["printed"])
    if not frames:
        raise pdf_ingest.PdfUnreadable("; ".join(f"{n}: {m}" for n, m in report.errors.items())
                                       or "לא נבחרו קבצים.")
    raw_all, report.duplicates = pdf_ingest.merge_reports(frames)
    incomplete = raw_all[["original_quantity", "original_length"]].isna().any(axis=1)
    raw = raw_all[~incomplete].reset_index(drop=True)
    return LoadedReport(raw_all=raw_all, raw=raw, names=[n for n, _ in files], signature=sig, load_report=report,
                        left_out=int(incomplete.sum()), same_content=same)


def check(loaded: LoadedReport) -> list[str]:
    """Problems found when the result is checked: printed totals against what was read, rows without numbers."""
    problems = []
    for name, (rows, quantity) in loaded.load_report.printed.items():
        read_rows, read_quantity = loaded.load_report.read.get(name, (None, None))
        if read_rows is None:
            continue
        if not pdf_ingest.totals_agree(read_rows, read_quantity, rows, quantity):
            problems.append(f"{name}: הדוח מדפיס {rows} שורות וכמות {quantity:g}, נקראו {read_rows} שורות וכמות {read_quantity:g}")
    if loaded.left_out:
        problems.append(f"{loaded.left_out} שורות בלי כמות או אורך קריאים (הן מופיעות בבקרת התקינות ולא בחישוב)")
    return problems


# --------------------------------------------------------------------------
# A copy on disk, so a new session (a lost connection on a phone) does not need the file again
# --------------------------------------------------------------------------
def _disk_dir() -> Path:
    base = Path(os.environ.get("PERGOLA_DATA_DIR") or (Path(__file__).resolve().parent / "data"))
    return base / "santaf"


def save_to_disk(state_key: str, loaded: LoadedReport) -> bool:
    try:
        folder = _disk_dir()
        folder.mkdir(parents=True, exist_ok=True)
        loaded.raw_all.to_csv(folder / f"{state_key}.csv", index=False, encoding="utf-8-sig")
        meta = {"signature": [list(s) for s in loaded.signature], "names": loaded.names,
                "loaded": loaded.load_report.loaded, "errors": loaded.load_report.errors,
                "printed": {k: list(v) for k, v in loaded.load_report.printed.items()},
                "read": {k: list(v) for k, v in loaded.load_report.read.items()},
                "confirmed": loaded.load_report.confirmed, "unconfirmed": loaded.load_report.unconfirmed,
                "duplicates": loaded.load_report.duplicates, "same_content": loaded.same_content,
                "saved_at": pd.Timestamp.now().isoformat(timespec="seconds")}
        (folder / f"{state_key}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def load_from_disk(state_key: str) -> LoadedReport | None:
    try:
        folder = _disk_dir()
        meta = json.loads((folder / f"{state_key}.json").read_text(encoding="utf-8"))
        frame = pd.read_csv(folder / f"{state_key}.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        for column in ("original_quantity", "original_length"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["confirmation_date"] = pd.to_datetime(frame["confirmation_date"])
        frame["page"] = pd.to_numeric(frame["page"], errors="coerce")
        frame["line"] = pd.to_numeric(frame["line"], errors="coerce")
        report = pdf_ingest.LoadReport(
            loaded=list(meta["loaded"]), errors=dict(meta["errors"]), confirmed=list(meta["confirmed"]),
            unconfirmed=list(meta["unconfirmed"]), duplicates=int(meta["duplicates"]),
            printed={k: tuple(v) for k, v in meta["printed"].items()}, read={k: tuple(v) for k, v in meta["read"].items()})
        incomplete = frame[["original_quantity", "original_length"]].isna().any(axis=1)
        return LoadedReport(raw_all=frame, raw=frame[~incomplete].reset_index(drop=True), names=list(meta["names"]),
                            signature=tuple(tuple(s) for s in meta["signature"]), load_report=report,
                            left_out=int(incomplete.sum()), same_content=list(meta.get("same_content", [])),
                            origin="disk")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear(state_key: str) -> None:
    st.session_state.pop(state_key, None)
    for suffix in (".csv", ".json"):
        try:
            (_disk_dir() / f"{state_key}{suffix}").unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# The screen side
# --------------------------------------------------------------------------
def get(state_key: str, uploaded) -> LoadedReport | None:
    """The current report: the files in the uploader (read once, kept), else the one kept from before.

    Shows a status box while reading. Raises pdf_ingest.PdfUnreadable when none of the uploaded files can be read
    (the previous report, if any, is kept for the next run)."""
    files = [(f.name, bytes(f.getvalue())) for f in uploaded or []]
    kept = st.session_state.get(state_key)
    if files:
        sig = signature(files)
        if kept is not None and kept.signature == sig:
            kept.origin = "upload"                        # a rerun, or the same file after a restore: nothing to read again
            return kept
        upload_diag.mark("reading")
        with st.status("קורא את הדוח...", expanded=False) as status:
            try:
                loaded = load(files)
            except pdf_ingest.PdfUnreadable as exc:
                status.update(label="הקריאה נכשלה", state="error")
                upload_diag.mark("read failed", type(exc).__name__)
                raise
            upload_diag.mark("rows extracted", str(len(loaded.raw_all)))
            status.update(label=f"הדוח נקרא: {len(loaded.raw_all)} שורות", state="complete")
        loaded.origin = "upload"
        st.session_state[state_key] = loaded                # replaces the previous report
        save_to_disk(state_key, loaded)
        upload_diag.mark("checked against printed totals", "ok" if not any("מדפיס" in p for p in check(loaded)) else "MISMATCH")
        return loaded
    if kept is not None:
        kept.origin = "session"
        return kept
    from_disk = load_from_disk(state_key)
    if from_disk is not None:
        st.session_state[state_key] = from_disk
    return from_disk


def show(loaded: LoadedReport, state_key: str) -> None:
    """Messages about a loaded report. The success message is shown only for a report that passed the checks."""
    report = loaded.load_report
    for name, why in report.errors.items():
        st.error(f"הקובץ {name} לא נקלט: {why}  \n(Skipped {name}: {why})")
    for name in loaded.same_content:
        st.caption(f"הקובץ {name} זהה לקובץ שכבר נקלט ונספר פעם אחת.")
    if report.duplicates:
        st.warning(f"{report.duplicates} שורות מופיעות ביותר מדוח אחד (הדוחות חופפים) ונספרות פעם אחת.  \n"
                   f"({report.duplicates} row(s) appear in more than one of the files and are counted once.)")
    problems = check(loaded)
    upload_diag.mark("shown", loaded.origin)
    if loaded.origin != "upload":
        where = "מהסשן הנוכחי" if loaded.origin == "session" else "שנשמר בדיסק מהפעם הקודמת"
        st.info(f"מוצג הדוח האחרון שנקלט ({', '.join(report.loaded)}), {where}. כדי להחליף אותו, העלה קובץ חדש.")
        if st.button("נקה את הדוח השמור", key=f"{state_key}_clear"):
            clear(state_key)
            st.rerun()
    elif not any(p for p in problems if "מדפיס" in p):
        verified = report.printed and not any("מדפיס" in p for p in problems)
        st.success(f"הקובץ נקלט בהצלחה: {len(loaded.raw_all)} שורות"
                   + (" · הסכומים תואמים לסיכום המודפס בדוח" if verified else " · הסיכום המודפס לא אומת"))
    for problem in problems:
        (st.error if "מדפיס" in problem else st.warning)(problem)
