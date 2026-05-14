"""
Calipsa "Network Video Recorder" email template.

Real samples in `experiments/alerting_feature/email_samples/` look like:

    From: admin@calipsa.io
    Subject: Network Video Recorder: Motion Detected On Channel D4
    EVENT TYPE:    Motion Detected
    EVENT TIME:    2026-05-12,22:40:07
    NVR NAME:      Network Video Recorder
    NVR S/N:       0820241008CCRRFQ6386292WCVU
    CAMERA NAME(NUM):   D4(D4)

The template can carry one or more JPEG snapshots as attachments.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.message import Message

from app.services.alert_parsers import register, ParserResult
from app.services.alert_parsers.helpers import (
    extract_subject, extract_text_body, extract_date,
)


PARSER_ID = "calipsa_nvr_v1"
PARSER_VERSION = 1


def _is_calipsa(msg: Message) -> bool:
    sender = (msg.get("From") or "").lower()
    if "calipsa.io" not in sender:
        return False
    # Sender alone is enough; the body shape can drift between Calipsa releases.
    return True


# Subject prefix -> event_type mapping. Order matters (longer first).
_SUBJECT_EVENT = (
    ("intrusion detected", "intrusion"),
    ("person detected", "person"),
    ("vehicle detected", "vehicle"),
    ("motion detected", "motion"),
)

_EVENT_TYPE_BODY = re.compile(r"^EVENT TYPE:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_EVENT_TIME_BODY = re.compile(r"^EVENT TIME:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_CAMERA_NUM_BODY = re.compile(r"^CAMERA NAME\(NUM\):\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_NVR_SN_BODY = re.compile(r"^NVR S/N:\s*(\S+)", re.MULTILINE | re.IGNORECASE)


def _parse_event_time(raw: str) -> datetime | None:
    """Calipsa formats: '2026-05-12,22:40:07' (date,time) — no timezone."""
    raw = (raw or "").strip()
    if not raw:
        return None
    candidates = [
        ("%Y-%m-%d,%H:%M:%S", raw),
        ("%Y-%m-%d %H:%M:%S", raw),
        ("%Y-%m-%dT%H:%M:%S", raw),
    ]
    for fmt, val in candidates:
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _normalize_event_type(subject: str, body_event: str | None) -> tuple[str, str | None]:
    """Return (event_type, event_subtype). Falls back to 'unknown'."""
    haystack = (subject or "").lower()
    if body_event:
        haystack = f"{haystack} {body_event.lower()}"
    for needle, event_type in _SUBJECT_EVENT:
        if needle in haystack:
            # Subtype: the raw phrase as Calipsa wrote it (capitalized form)
            return event_type, (body_event or needle.title())
    return "unknown", body_event


def parse(msg: Message) -> ParserResult:
    subject = extract_subject(msg)
    body = extract_text_body(msg)

    body_event_m = _EVENT_TYPE_BODY.search(body)
    body_event = body_event_m.group(1).strip() if body_event_m else None
    event_type, event_subtype = _normalize_event_type(subject, body_event)

    time_m = _EVENT_TIME_BODY.search(body)
    detected_at = _parse_event_time(time_m.group(1)) if time_m else None
    if detected_at is None:
        # Fall back to the Date header.
        detected_at = extract_date(msg)

    camera_m = _CAMERA_NUM_BODY.search(body)
    nvr_sn_m = _NVR_SN_BODY.search(body)

    extra: dict[str, str] = {}
    if camera_m:
        extra["camera_channel"] = camera_m.group(1).strip()
    if nvr_sn_m:
        extra["nvr_serial"] = nvr_sn_m.group(1).strip()

    # "exact" if we recognized the event AND extracted a detected_at.
    if event_type != "unknown" and detected_at is not None:
        confidence = "exact"
    elif event_type != "unknown" or detected_at is not None:
        confidence = "heuristic"
    else:
        confidence = "unparsed"

    return ParserResult(
        parser_id=PARSER_ID,
        parser_version=PARSER_VERSION,
        parser_confidence=confidence,
        event_type=event_type,
        event_subtype=event_subtype,
        confidence=None,  # Calipsa doesn't supply a 0-1 numeric confidence
        detected_at=detected_at,
        subject=subject,
        body_text=body,
        extra=extra,
    )


register(_is_calipsa, parse)
