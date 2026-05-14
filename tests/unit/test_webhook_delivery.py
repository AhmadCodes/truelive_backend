"""Tests for webhook delivery — retry schedule math + payload signing.

The string literals below ('shared-secret-12345', 'secret-A', 'secret-B') are
arbitrary HMAC keys for unit-test fixtures — they are never used to sign real
traffic and don't authenticate any system. Real consumer secrets are generated
by the GuardDesk registration flow and stored in webhook_consumers.secret.
"""

import json

import pytest

from app.core.config import settings
from app.tasks.deliver_webhook import _retry_delay
from app.utils.hmac_sign import sign, verify


def test_retry_schedule_matches_spec():
    """Spec §9: 1m, 5m, 30m, 2h, 12h, then give up."""
    schedule = settings.WEBHOOK_RETRY_SCHEDULE_SECONDS
    assert schedule == [60, 300, 1800, 7200, 43200]
    # _retry_delay(attempt) returns seconds until attempt+1.
    # attempt=1 (just failed first try) -> wait 60s before attempt 2
    assert _retry_delay(1) == schedule[1]  # second slot
    assert _retry_delay(2) == schedule[2]
    assert _retry_delay(3) == schedule[3]
    assert _retry_delay(4) == schedule[4]
    # After 6 attempts (indices 1..5), schedule is exhausted
    assert _retry_delay(5) is None
    assert _retry_delay(6) is None


def test_payload_signature_is_stable_for_same_body():
    body = json.dumps({"alert_id": "a1", "schema_version": "1.0"}, separators=(",", ":")).encode()
    secret = "shared-secret-12345"
    s1 = sign(body, secret)
    s2 = sign(body, secret)
    assert s1 == s2
    assert verify(body, s1, secret)


def test_payload_signature_changes_with_body_or_secret():
    body = b'{"alert_id":"a1"}'
    s1 = sign(body, "secret-A")
    s2 = sign(body, "secret-B")
    assert s1 != s2
    s3 = sign(b'{"alert_id":"a2"}', "secret-A")
    assert s1 != s3
