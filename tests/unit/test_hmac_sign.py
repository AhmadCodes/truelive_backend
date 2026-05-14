"""Tests for app.utils.hmac_sign."""

import time

import pytest

from app.utils.hmac_sign import (
    sign, verify, verify_timestamp, now_timestamp_header, SIGNATURE_PREFIX,
)


def test_sign_returns_prefixed_hex():
    sig = sign(b"hello world", "secret")
    assert sig.startswith(SIGNATURE_PREFIX)
    # sha256 hex is exactly 64 chars after the prefix
    assert len(sig) == len(SIGNATURE_PREFIX) + 64
    # All-hex
    int(sig[len(SIGNATURE_PREFIX):], 16)


def test_sign_deterministic():
    a = sign(b"abc", "k")
    b = sign(b"abc", "k")
    assert a == b


def test_sign_accepts_str_inputs():
    a = sign("abc", "k")
    b = sign(b"abc", b"k")
    assert a == b


def test_verify_roundtrip():
    body = b'{"alert_id":"x","camera_id":"y"}'
    secret = "shhh"
    sig = sign(body, secret)
    assert verify(body, sig, secret)
    assert not verify(b"tampered", sig, secret)
    assert not verify(body, sig, "wrong-secret")


def test_verify_rejects_malformed_signature():
    assert not verify(b"x", "", "k")
    assert not verify(b"x", "md5=...", "k")
    assert not verify(b"x", "sha256=NOTHEX", "k")


def test_timestamp_within_window():
    now = 1_700_000_000
    assert verify_timestamp(str(now), skew_seconds=300, now=now)
    assert verify_timestamp(str(now + 299), skew_seconds=300, now=now)
    assert verify_timestamp(str(now - 299), skew_seconds=300, now=now)


def test_timestamp_outside_window():
    now = 1_700_000_000
    assert not verify_timestamp(str(now + 301), skew_seconds=300, now=now)
    assert not verify_timestamp(str(now - 301), skew_seconds=300, now=now)


def test_timestamp_rejects_malformed():
    assert not verify_timestamp("not-a-number", skew_seconds=300, now=0)
    assert not verify_timestamp("", skew_seconds=300, now=0)


def test_now_timestamp_header_format():
    h = now_timestamp_header()
    # Returns a stringified integer
    assert h.isdigit()
    assert abs(int(h) - time.time()) < 2
