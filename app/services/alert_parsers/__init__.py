"""
Passthrough "parser" for the alerting pipeline.

The pipeline used to have a dispatch / registry of per-sender parsers that
regex-matched fields out of the email body. That has been removed:

- Calipsa already did the AI detection — the email arriving IS the alert.
- Body-format dependencies are brittle: a vendor template tweak silently drops
  classification to `unparsed`, but the alert itself is still correct.

What we extract now is strictly from headers + MIME structure:

- `subject` — the `Subject:` header
- `detected_at` — the `Date:` header, parsed to a tz-aware datetime
- `body_text` — the text/plain part if present, stored opaquely (we do not
  derive any fields from it). HTML-only bodies are stripped to plain text for
  display. The raw .eml is always in MinIO for full-fidelity inspection.
- Attachments via MIME walk (in `process_inbound_alert`, not here).

`event_type` / `event_subtype` are fixed constants. If the downstream consumer
wants finer classification, they can derive it from `subject` or fetch the raw
email — that's a consumer-side concern, not a producer-side one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from typing import Optional


PARSER_ID = "passthrough_v1"
PARSER_VERSION = 1
EVENT_TYPE = "alert"
EVENT_SUBTYPE = "ai_alert"


@dataclass
class ParserResult:
    parser_id: str
    parser_version: int
    parser_confidence: str
    event_type: str
    event_subtype: Optional[str] = None
    confidence: Optional[float] = None
    detected_at: Optional[datetime] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    extra: dict = field(default_factory=dict)


def passthrough(msg: Message) -> ParserResult:
    """Build a ParserResult from headers + MIME only. Never touches body
    content for field extraction."""
    from app.services.alert_parsers.helpers import (
        extract_subject, extract_text_body, extract_date,
    )
    return ParserResult(
        parser_id=PARSER_ID,
        parser_version=PARSER_VERSION,
        parser_confidence="exact",
        event_type=EVENT_TYPE,
        event_subtype=EVENT_SUBTYPE,
        detected_at=extract_date(msg),
        subject=extract_subject(msg) or None,
        body_text=extract_text_body(msg) or None,
        extra={},
    )
