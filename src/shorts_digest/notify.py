"""Email the digest with the standard library's smtplib (no extra dependencies).

Needs SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_APP_PASSWORD and DIGEST_TO (in .env locally, as
repository secrets on GitHub). For Gmail: host smtp.gmail.com, port 587, and an *app password*
(Google account -> Security -> App passwords), never your normal password.

The email has the Markdown digest as plain text plus a simple HTML version, and the CSV
attached. Port 465 uses SSL from the start; any other port uses STARTTLS.
"""

from __future__ import annotations

import html
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from shorts_digest.config import Env

SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_APP_PASSWORD", "DIGEST_TO")
TIMEOUT_SECONDS = 30

_LINK_RE = re.compile(r"(https?://[^\s<]+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_ITALIC_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)")


def markdown_to_html(md: str) -> str:
    """Just enough Markdown for our own digest format: headings, bold, italics, code,
    links and line breaks. Everything is HTML-escaped first, so titles can't inject markup."""
    out = []
    for raw in md.splitlines():
        line = html.escape(raw.rstrip())
        line = _BOLD_RE.sub(r"<b>\1</b>", line)
        line = _CODE_RE.sub(r"<code>\1</code>", line)
        line = _ITALIC_RE.sub(r"<i>\1</i>", line)
        line = _LINK_RE.sub(r'<a href="\1">\1</a>', line)
        if line.startswith("## "):
            out.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            out.append(f"<h2>{line[2:]}</h2>")
        elif line:
            out.append(f"{line}<br>")
        else:
            out.append("<br>")
    body = "\n".join(out)
    return f'<html><body style="font-family:sans-serif;line-height:1.4">{body}</body></html>'


def build_message(
    subject: str, md: str, sender: str, to: str, csv_path: Path | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to  # comma-separated list is fine
    msg.set_content(md)
    msg.add_alternative(markdown_to_html(md), subtype="html")
    if csv_path is not None and csv_path.exists():
        msg.add_attachment(
            csv_path.read_bytes(), maintype="text", subtype="csv", filename=csv_path.name
        )
    return msg


def send_digest(env: Env, subject: str, md: str, csv_path: Path | None = None) -> None:
    """Send one digest email. Raises on any SMTP problem (the caller decides what it means).

    Error messages never include the password: smtplib's exceptions carry the server's
    reply, not our credentials.
    """
    env.require(*SMTP_KEYS)
    host, port = env.get("SMTP_HOST"), int(env.get("SMTP_PORT"))
    user, password, to = env.get("SMTP_USER"), env.get("SMTP_APP_PASSWORD"), env.get("DIGEST_TO")
    msg = build_message(subject, md, user, to, csv_path)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(msg)


def digest_subject(day_label: str, main: int, new_voices: int) -> str:
    extra = f" + {new_voices} new voices" if new_voices else ""
    return f"Tech Shorts Digest — {day_label}: {main} picks{extra}"
