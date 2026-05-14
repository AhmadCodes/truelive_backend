"""
Shared MIME / text helpers used by alert parsers.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Optional

# Tiny HTML-to-text used as a last-resort body extractor. Not a full sanitizer —
# good enough for `body_text` storage that goes into the webhook payload.
_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(nbsp|amp|lt|gt|quot|apos);")
_ENTITY_MAP = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_WS = re.compile(r"\s+")


def extract_subject(msg: Message) -> str:
    return (msg.get("Subject") or "").strip()


def extract_text_body(msg: Message) -> str:
    """Prefer text/plain. Fall back to text/html stripped to plain."""
    text_part = None
    html_part = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain" and text_part is None:
                text_part = part
            elif ctype == "text/html" and html_part is None:
                html_part = part
    else:
        text_part = msg

    chosen = text_part if text_part is not None else html_part
    if chosen is None:
        return ""

    try:
        payload = chosen.get_payload(decode=True)
        if payload is None:
            return ""
        charset = chosen.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
    except Exception:
        body = chosen.get_payload() or ""

    if (chosen.get_content_type() or "").lower() == "text/html":
        body = _strip_html(body)
    return body.strip()


def _strip_html(s: str) -> str:
    s = _TAG.sub(" ", s)
    s = _ENTITY.sub(lambda m: _ENTITY_MAP.get(m.group(1), ""), s)
    s = _WS.sub(" ", s)
    return s


def extract_date(msg: Message) -> Optional[datetime]:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def extract_attachments(msg: Message):
    """
    Yield (filename, content_type, raw_bytes) for each non-inline-text part.

    Inline images are returned too (kind classification happens in the parser).
    """
    if not msg.is_multipart():
        return
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype.startswith(("text/plain", "text/html")):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        filename = part.get_filename() or ""
        yield filename, ctype or "application/octet-stream", payload
