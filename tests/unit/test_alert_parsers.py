"""Tests for the alert parser registry, using real Calipsa .eml samples
committed under experiments/alerting_feature/email_samples/."""

import email
from pathlib import Path

import pytest

from app.services.alert_parsers import dispatch, unparsed_fallback


SAMPLES_DIR = Path(__file__).resolve().parents[2] / "experiments" / "alerting_feature" / "email_samples"


def _load(name: str):
    path = SAMPLES_DIR / name
    with open(path, "rb") as f:
        return email.message_from_bytes(f.read())


def test_calipsa_known_sample_parses_exact():
    msg = _load("350160735.eml")
    r = dispatch(msg)
    assert r.parser_id == "calipsa_nvr_v1"
    assert r.parser_confidence == "exact"
    assert r.event_type == "motion"
    assert r.detected_at is not None
    assert "camera_channel" in r.extra
    assert r.subject and "motion" in r.subject.lower()


def test_all_calipsa_samples_roundtrip():
    """Every committed sample should parse without raising. At least one should
    parse with 'exact' confidence — proves the Calipsa template is intact."""
    samples = sorted(SAMPLES_DIR.glob("*.eml"))
    assert samples, "expected at least one .eml sample under experiments/alerting_feature/email_samples/"
    confidences = {"exact": 0, "heuristic": 0, "unparsed": 0, "llm_generated": 0}
    for s in samples:
        with open(s, "rb") as f:
            msg = email.message_from_bytes(f.read())
        r = dispatch(msg)
        assert r.parser_id, f"empty parser_id for {s.name}"
        assert r.event_type in ("motion", "person", "vehicle", "intrusion", "unknown")
        confidences[r.parser_confidence] += 1
    assert confidences["exact"] >= 1, f"no exact parses across {len(samples)} samples: {confidences}"


def test_unparsed_fallback_with_minimal_email():
    msg = email.message_from_string(
        "From: foo@bar.com\nSubject: Hello\n\nbody text\n"
    )
    r = unparsed_fallback(msg)
    assert r.parser_id == "unknown_v1"
    assert r.parser_confidence == "unparsed"
    assert r.event_type == "unknown"
    assert r.subject == "Hello"
    assert "body text" in r.body_text


def test_dispatch_falls_back_for_non_calipsa():
    msg = email.message_from_string(
        "From: random@somewhere.net\nSubject: not a Calipsa email\n\nhi"
    )
    r = dispatch(msg)
    assert r.parser_id == "unknown_v1"
    assert r.parser_confidence == "unparsed"
