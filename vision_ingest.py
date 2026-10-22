"""AI vision ingestion (optional): read order tables from photos with a Claude vision model.

The model reads the printed digits and decimal points directly, which is far more reliable
than Tesseract on low-resolution photos. NOTE: the photo is sent to the Anthropic API.

Rows come back in the same shape as ocr_ingest (canonical columns plus a `check` column), so
both engines feed calculator.process_orders() identically.
"""
from __future__ import annotations

import base64
import io
import json

import pandas as pd

from ocr_ingest import (COLUMNS, MAX_PLAUSIBLE_LENGTH, OcrUnavailable, _reconcile_dates,
                        parse_date, parse_id)

VISION_MODEL = "claude-opus-5-5"

_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "confirmation_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "code": {"type": "string"},
                    "zone": {"type": "string"},
                    "original_quantity": {"type": ["number", "null"]},
                    "original_length": {"type": ["number", "null"]},
                    "unsure": {
                        "type": "string",
                        "description": "empty if every cell is clearly legible, otherwise "
                                       "which cell(s) were hard to read and why",
                    },
                },
                "required": ["order_id", "confirmation_date", "code", "zone",
                             "original_quantity", "original_length", "unsure"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}

_NO_CREDENTIALS = ("No valid Anthropic credentials. Enter an API key, set ANTHROPIC_API_KEY, "
                   "or run `ant auth login`.")

_PROMPT = """This is a photo or scan of a printed order schedule table, usually Hebrew and laid \
out right to left (order ID in the right-most column). The columns are: order ID (הזמנה), \
coordination date (ת. תיאום), code (קוד), zone (איזור), quantity (כמות) and length in mm \
(אורך). Other columns (item status, checkboxes, notes) must be ignored.

Return one entry for every data row of the table, top to bottom, in the order printed. Skip \
header rows, titles, sub-headings and total rows. Copy each value exactly as printed: keep every \
digit, and keep decimal points in quantities exactly (3.6 stays 3.6 and 2.835 stays 2.835, never \
36 or 2835). Write dates as YYYY-MM-DD (the sheet shows dd/mm/yyyy). Use null for a number you \
cannot read and an empty string for text you cannot read; never guess a digit. In "unsure", \
name any cell you had trouble reading."""


def _image_for_api(image_bytes: bytes) -> tuple[str, bytes]:
    """(media_type, bytes): EXIF-rotated RGB as PNG, or JPEG if PNG would exceed the 5 MB limit."""
    from PIL import Image, ImageOps
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    if buf.tell() > 4_000_000:
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=92)
        return "image/jpeg", buf.getvalue()
    return "image/png", buf.getvalue()


def _row(r: dict) -> dict:
    """Validate one model-returned row with the same parsers the OCR path uses."""
    notes = [r["unsure"]] if r.get("unsure") else []
    order_id = parse_id(r.get("order_id", ""))
    if not order_id:
        notes.append("order id unreadable")
    date = parse_date(r.get("confirmation_date", ""))
    if not date:
        notes.append("date unreadable")
    qty, length = r.get("original_quantity"), r.get("original_length")
    if qty is None:
        notes.append("quantity missing")
    if length is None:
        notes.append("length missing")
    elif length > MAX_PLAUSIBLE_LENGTH:
        notes.append(f"length above {MAX_PLAUSIBLE_LENGTH} mm is implausible")
    return {"order_id": order_id or "", "confirmation_date": date,
            "code": str(r.get("code", "")).strip(), "zone": str(r.get("zone", "")).strip(),
            "original_quantity": qty, "original_length": length, "check": "; ".join(notes)}


def rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Cross-row checks shared with the OCR path: id length, dominant year, date order."""
    lengths = [len(r["order_id"]) for r in rows if r["order_id"]]
    id_len = max(set(lengths), key=lengths.count) if lengths else None
    for r in rows:
        if id_len and r["order_id"] and len(r["order_id"]) != id_len:
            r["check"] = "; ".join(filter(None, [
                r["check"], f"order id has {len(r['order_id'])} digits, expected {id_len}"]))
    _reconcile_dates(rows)
    df = pd.DataFrame(rows, columns=[*COLUMNS, "check"])
    for col in ("original_quantity", "original_length"):
        df[col] = pd.to_numeric(df[col])
    return df


def extract_orders(images: list[bytes], api_key: str | None = None, client=None,
                   model: str = VISION_MODEL) -> pd.DataFrame:
    """Read the order rows of each photo with a Claude vision model.

    Credentials: `api_key`, else ANTHROPIC_API_KEY / an `ant auth login` profile.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise OcrUnavailable("Install the Anthropic SDK: pip install anthropic") from exc
    client = client or anthropic.Anthropic(api_key=api_key or None)

    rows: list[dict] = []
    for image_bytes in images:
        media_type, data = _image_for_api(image_bytes)
        try:
            response = client.messages.create(
                model=model, max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high",
                               "format": {"type": "json_schema", "schema": _SCHEMA}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type,
                        "data": base64.standard_b64encode(data).decode()}},
                    {"type": "text", "text": _PROMPT},
                ]}],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise OcrUnavailable(_NO_CREDENTIALS) from exc
        except TypeError as exc:                 # the SDK raises this when no credential is found
            if "authentication" in str(exc).lower():
                raise OcrUnavailable(_NO_CREDENTIALS) from exc
            raise
        except anthropic.APIConnectionError as exc:
            raise OcrUnavailable(f"Could not reach the Anthropic API: {exc}") from exc
        if response.stop_reason == "refusal":
            raise OcrUnavailable("The vision model declined to read this image.")
        if response.stop_reason == "max_tokens":
            raise OcrUnavailable("The table is too long for one request; split the photo.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        rows.extend(_row(r) for r in json.loads(text)["rows"])
    return rows_to_frame(rows)
