"""File-type lists for st.file_uploader that phones understand.

Streamlit turns plain extensions into an `accept` of "application/streamlit,.pdf". Phones filter the file picker by
real MIME types, so each extension is sent together with its MIME type ("application/pdf,.pdf" for a desktop
drag-and-drop that only knows the extension). Older Streamlit versions do not take MIME types in `type`, so there
the plain extensions are used.
"""
from __future__ import annotations

MIME = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "zip": "application/zip",
    "json": "application/json",
}


def _supports_mime() -> bool:
    try:
        from streamlit.elements.lib import file_uploader_utils
        return hasattr(file_uploader_utils, "classify_file_type")
    except Exception:
        return False


def upload_types(*extensions: str) -> list[str]:
    """`type=` for st.file_uploader: the extensions plus (where Streamlit allows it) their MIME types."""
    exts = [e.lower().lstrip(".") for e in extensions]
    if not _supports_mime():
        return exts
    mimes = list(dict.fromkeys(MIME[e] for e in exts if e in MIME))
    return [*mimes, *exts]
