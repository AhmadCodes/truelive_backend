"""Tests for app.utils.secrets_gen."""

import pytest

from app.utils.secrets_gen import (
    generate_alert_local_part, generate_service_account_token,
    ALERT_LOCAL_PREFIX, SERVICE_ACCOUNT_TOKEN_PREFIX,
)


def test_alert_local_part_format():
    s = generate_alert_local_part()
    assert s.startswith(ALERT_LOCAL_PREFIX)
    # 12 url-safe bytes -> ~16 chars after the cam- prefix
    assert len(s) >= len(ALERT_LOCAL_PREFIX) + 12


def test_alert_local_part_uniqueness():
    seen = {generate_alert_local_part() for _ in range(200)}
    assert len(seen) == 200, "collisions in 200 generations: entropy too low"


def test_service_account_token_format_and_verify():
    raw, hashed = generate_service_account_token()
    assert raw.startswith(SERVICE_ACCOUNT_TOKEN_PREFIX)
    # 32 url-safe bytes -> ~43 chars + 5 prefix
    assert len(raw) >= 40
    # The hash is bcrypt -> starts with $2 (any of $2a/$2b/$2y)
    assert hashed.startswith("$2")
    from passlib.hash import bcrypt  # type: ignore
    assert bcrypt.verify(raw, hashed)
    assert not bcrypt.verify(raw + "tamper", hashed)


def test_service_account_token_unique_per_call():
    raws = {generate_service_account_token()[0] for _ in range(20)}
    assert len(raws) == 20
