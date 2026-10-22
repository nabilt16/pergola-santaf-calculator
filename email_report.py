"""Send summary reports and tables by email (Gmail/SMTP), ported from the LED inventory app.

Differences from that app, because this one is meant to run on a public URL:
  * credentials come only from Streamlit secrets / environment variables, never from source code;
  * recipients can be restricted (`allowed_recipients`), and the UI limits sends per session, so
    the page cannot be used as an open mail relay;
  * every value is HTML-escaped before it goes into the message.

Secrets (`.streamlit/secrets.toml` locally, "Secrets" in Streamlit Cloud settings):

    [email]
    sender = "you@gmail.com"
    password = "gmail app password"         # https://myaccount.google.com/apppasswords
    # optional:
    smtp_host = "smtp.gmail.com"
    smtp_port = 587
    default_recipient = "someone@example.com"
    allowed_recipients = ["a@example.com", "b@example.com"]
"""
from __future__ import annotations

import html
import io
import mimetypes
import os
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Mapping, Sequence

import pandas as pd

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]{2,}$")
MAX_RECIPIENTS = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class EmailNotConfigured(RuntimeError):
    """No sender / password has been provided."""


@dataclass(frozen=True)
class EmailConfig:
    sender: str
    password: str = field(repr=False)             # never shown in logs or tracebacks
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    default_recipient: str = ""
    allowed_recipients: tuple[str, ...] = ()


def load_config(secrets: Mapping | None = None, environ: Mapping | None = None) -> EmailConfig:
    """Read the email settings from the `[email]` secrets section or EMAIL_* environment variables."""
    environ = os.environ if environ is None else environ
    section = {}
    try:
        section = dict((secrets or {}).get("email", {}) or {})
    except Exception:                              # no secrets file at all
        section = {}

    def pick(key: str, env_key: str, default=""):
        return section.get(key) or environ.get(env_key) or default

    sender, password = pick("sender", "EMAIL_SENDER"), pick("password", "EMAIL_PASSWORD")
    if not sender or not password:
        raise EmailNotConfigured(
            "Email is not configured. Add an [email] section with `sender` and `password` "
            "(a Gmail app password) to the app's secrets.")
    allowed = section.get("allowed_recipients") or [
        a for a in re.split(r"[,;\s]+", environ.get("EMAIL_ALLOWED_RECIPIENTS", "")) if a]
    return EmailConfig(
        sender=str(sender).strip(), password=str(password).replace(" ", ""),
        smtp_host=str(pick("smtp_host", "EMAIL_SMTP_HOST", "smtp.gmail.com")),
        smtp_port=int(pick("smtp_port", "EMAIL_SMTP_PORT", 587)),
        default_recipient=str(pick("default_recipient", "EMAIL_DEFAULT_RECIPIENT")).strip(),
        allowed_recipients=tuple(a.strip().lower() for a in allowed),
    )


def parse_recipients(text: str, config: EmailConfig | None = None) -> list[str]:
    """Split on commas/semicolons/whitespace and validate; raises ValueError with a clear message."""
    addresses = [a for a in re.split(r"[,;\s]+", str(text or "").strip()) if a]
    if not addresses:
        raise ValueError("Enter a recipient email address.")
    if len(addresses) > MAX_RECIPIENTS:
        raise ValueError(f"At most {MAX_RECIPIENTS} recipients per message.")
    bad = [a for a in addresses if not _EMAIL_RE.match(a)]
    if bad:
        raise ValueError(f"Not a valid email address: {', '.join(bad)}")
    if config and config.allowed_recipients:
        blocked = [a for a in addresses if a.lower() not in config.allowed_recipients]
        if blocked:
            raise ValueError(f"Not an allowed recipient: {', '.join(blocked)}")
    return addresses


def build_message(subject: str, html_body: str, sender: str, recipients: Sequence[str],
                  attachments: Sequence[tuple[str, bytes]] = ()) -> EmailMessage:
    """HTML message with a plain-text fallback and optional (filename, bytes) attachments."""
    if any(c in subject for c in "\r\n"):
        raise ValueError("Invalid subject.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    msg.set_content("This report is best viewed in an email client that supports HTML.")
    msg.add_alternative(html_body, subtype="html")
    total = 0
    for filename, data in attachments:
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments are larger than 10 MB.")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        main, sub = mime.split("/", 1)
        msg.add_attachment(data, maintype=main, subtype=sub, filename=filename)
    return msg


def send_message(config: EmailConfig, msg: EmailMessage) -> None:
    """Deliver through SMTP with STARTTLS; raises smtplib errors for the UI to show."""
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(config.sender, config.password)
        server.send_message(msg)


def friendly_error(exc: Exception) -> str:
    """A message for the user that never includes credentials."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the login. For Gmail use an app password "
                "(Google account > Security > App passwords), not the normal password.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    if isinstance(exc, (smtplib.SMTPException, OSError)):
        return f"Could not send the email ({type(exc).__name__}). Try again in a moment."
    return str(exc)


# --------------------------------------------------------------------------
# Report content
# --------------------------------------------------------------------------
def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value):,}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _esc(value)


def html_table(df: pd.DataFrame, title: str = "") -> str:
    """A right-to-left HTML table with inline styles (email clients ignore stylesheets)."""
    head = "".join(f'<th style="padding:9px 12px;text-align:center;white-space:nowrap">{_esc(c)}</th>'
                   for c in df.columns)
    body = ""
    for i, row in enumerate(df.itertuples(index=False)):
        bg = "#f8f9fa" if i % 2 else "#ffffff"
        cells = "".join(f'<td style="padding:8px 12px;text-align:center;white-space:nowrap">{_fmt(v)}</td>'
                        for v in row)
        body += f'<tr style="background:{bg}">{cells}</tr>'
    heading = (f'<h3 style="margin:22px 0 8px;font-size:16px;color:#2c3e50">{_esc(title)}</h3>'
               if title else "")
    return (f'{heading}<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;'
            f'font-size:14px;border:1px solid #ddd"><thead><tr style="background:#2c3e50;color:#fff">'
            f'{head}</tr></thead><tbody>{body}</tbody></table></div>')


def build_report_html(title: str, period: str, metrics: Sequence[tuple[str, object]],
                      tables: Sequence[tuple[str, pd.DataFrame]], note: str = "") -> str:
    """The full email body: header, metric boxes, then each table."""
    boxes = "".join(
        f'<div style="background:#f8f9fa;border-radius:8px;padding:12px 18px;flex:1;min-width:110px;'
        f'text-align:center"><div style="font-size:20px;font-weight:bold;color:#2c3e50">{_fmt(v)}</div>'
        f'<div style="font-size:11px;color:#666">{_esc(label)}</div></div>' for label, v in metrics)
    sections = "".join(html_table(df, name) for name, df in tables if df is not None and not df.empty)
    note_html = (f'<p style="margin:0 0 16px;font-size:13px;color:#555">{_esc(note)}</p>' if note else "")
    return f"""<html dir="rtl"><body style="margin:0;padding:20px;background:#f0f0f0;font-family:Arial,sans-serif;direction:rtl">
<div style="max-width:760px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.12)">
<div style="background:#2c3e50;padding:22px 28px"><h1 style="color:#fff;margin:0;font-size:22px">{_esc(title)}</h1>
<p style="color:#bdc3c7;margin:8px 0 0;font-size:13px">{_esc(period)}</p></div>
<div style="padding:28px">{note_html}<div style="display:flex;gap:14px;margin-bottom:18px;flex-wrap:wrap">{boxes}</div>{sections}</div>
<div style="background:#f5f5f5;padding:14px 28px;text-align:center;font-size:12px;color:#999;border-top:1px solid #e0e0e0">מחשבון סנטפים לפרגולות</div>
</div></body></html>"""


def excel_bytes(sheets: Mapping[str, pd.DataFrame]) -> bytes:
    """One .xlsx with a sheet per table (sheet names are clipped to Excel's 31 characters)."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets.items():
            out = df.copy()
            for col in out.columns:                     # Excel cannot store timezone-aware / mixed objects
                if pd.api.types.is_datetime64_any_dtype(out[col]):
                    out[col] = out[col].dt.strftime("%Y-%m-%d")
            out.to_excel(writer, sheet_name=re.sub(r"[\[\]:*?/\\]", "-", name)[:31], index=False)
    return buf.getvalue()
