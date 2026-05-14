"""Tests for the passthrough alert parser.

The passthrough doesn't depend on email body format — `event_type`,
`event_subtype`, and `parser_confidence` are constants. The only things we
extract are: `subject` (header), `detected_at` (Date header), `body_text`
(stored opaquely for downstream display only).

We still run every committed sample through the function to confirm:
  (a) no exception ever escapes,
  (b) constants are stable across all samples,
  (c) headers extract sensibly.
"""

import email
from pathlib import Path

import pytest

from app.services.alert_parsers import passthrough, EVENT_TYPE, EVENT_SUBTYPE, PARSER_ID


SAMPLES_DIR = Path(__file__).resolve().parents[2] / "experiments" / "alerting_feature" / "email_samples"


def _load(name: str):
    path = SAMPLES_DIR / name
    with open(path, "rb") as f:
        return email.message_from_bytes(f.read())


def test_passthrough_returns_constant_classification():
    msg = _load("350160735.eml")
    r = passthrough(msg)
    assert r.parser_id == PARSER_ID
    assert r.parser_confidence == "exact"
    assert r.event_type == EVENT_TYPE == "alert"
    assert r.event_subtype == EVENT_SUBTYPE == "ai_alert"


def test_passthrough_extracts_headers_not_body_fields():
    msg = _load("350160735.eml")
    r = passthrough(msg)
    # Subject comes from header
    assert r.subject and "Channel" in r.subject  # the sample's Subject mentions a channel
    # detected_at comes from the Date header (RFC 5322 parse) — should be tz-aware
    assert r.detected_at is not None
    assert r.detected_at.tzinfo is not None
    # extra is empty — we don't pull anything from the body
    assert r.extra == {}


def test_passthrough_body_stored_opaquely():
    """body_text is preserved as a blob for downstream display, but the parser
    must not derive any field from it."""
    msg = _load("350160735.eml")
    r = passthrough(msg)
    assert r.body_text is not None
    # Sanity that we got SOMETHING (the Calipsa samples have multi-line bodies)
    assert len(r.body_text) > 0


def test_every_sample_roundtrips_with_stable_constants():
    """Every committed sample must produce identical event_type/subtype/parser_id.
    This is the key contract: the system does not depend on Calipsa template
    drift — even radically different bodies still yield the same classification.
    """
    samples = sorted(SAMPLES_DIR.glob("*.eml"))
    assert samples, "expected .eml samples under experiments/alerting_feature/email_samples/"
    for s in samples:
        with open(s, "rb") as f:
            msg = email.message_from_bytes(f.read())
        r = passthrough(msg)
        assert r.parser_id == PARSER_ID, f"parser_id drift on {s.name}"
        assert r.event_type == EVENT_TYPE, f"event_type drift on {s.name}"
        assert r.event_subtype == EVENT_SUBTYPE, f"event_subtype drift on {s.name}"
        assert r.parser_confidence == "exact", f"confidence drift on {s.name}"


def test_passthrough_handles_minimal_email():
    """A bare email with no body or attachments should still produce a valid
    result. (We never crash on weird inputs — the email arriving IS the alert.)"""
    msg = email.message_from_string("From: x@y.com\nSubject: ping\n\n")
    r = passthrough(msg)
    assert r.event_type == "alert"
    assert r.parser_confidence == "exact"
    assert r.subject == "ping"


def test_passthrough_handles_email_without_date_header():
    """No Date header → detected_at is None. Still valid."""
    msg = email.message_from_string("From: x@y.com\nSubject: no-date\n\nbody")
    r = passthrough(msg)
    assert r.detected_at is None
    assert r.event_type == "alert"
