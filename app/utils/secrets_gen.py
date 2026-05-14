"""
Token / opaque-id generators for the alerting feature.

`generate_alert_local_part` produces the local part of the per-camera email
address that gets pasted into Calipsa.

`generate_service_account_token` produces a fresh service-account bearer token
in the `tlsa_<random>` format, returning both the raw token (shown once) and
the bcrypt hash for storage.
"""

from __future__ import annotations

import secrets


SERVICE_ACCOUNT_TOKEN_PREFIX = "tlsa_"
ALERT_LOCAL_PREFIX = "cam-"


def generate_alert_local_part() -> str:
    """`cam-<16-char-token>` — 96 bits of entropy. Prefix is human-readable."""
    return f"{ALERT_LOCAL_PREFIX}{secrets.token_urlsafe(12)}"


def generate_service_account_token() -> tuple[str, str]:
    """
    Return (raw_token, bcrypt_hash).

    The raw token is shown to the caller exactly once on token creation; the hash
    is stored. Verification uses passlib.hash.bcrypt.verify(raw, hash).
    """
    from passlib.hash import bcrypt  # type: ignore
    raw = f"{SERVICE_ACCOUNT_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    hashed = bcrypt.hash(raw)
    return raw, hashed
