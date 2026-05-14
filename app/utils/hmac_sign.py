"""
HMAC signing + replay-protection for outbound webhook deliveries.

Spec §9: every POST to a webhook consumer carries:
    X-TrueLive-Signature: sha256=<hex>
    X-TrueLive-Timestamp: <unix-epoch-seconds>
    X-TrueLive-Delivery-Id: <uuid>
    X-TrueLive-Alert-Id: <uuid>

Consumers verify: HMAC-SHA256(body, shared_secret) and reject if timestamp is more
than 5 minutes from now.

For symmetry, this module also exposes `verify()` which we use in tests and could
expose to consumers that want a server-side check.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Union

from app.core.config import settings


SIGNATURE_PREFIX = "sha256="


def sign(body: Union[bytes, str], secret: Union[bytes, str]) -> str:
    """Return `sha256=<hex>` for the body+secret pair."""
    body_b = body.encode("utf-8") if isinstance(body, str) else body
    secret_b = secret.encode("utf-8") if isinstance(secret, str) else secret
    mac = hmac.new(secret_b, body_b, hashlib.sha256)
    return SIGNATURE_PREFIX + mac.hexdigest()


def verify(
    body: Union[bytes, str], signature_header: str, secret: Union[bytes, str],
) -> bool:
    """Constant-time signature compare. Returns False on any malformed input."""
    if not signature_header or not signature_header.startswith(SIGNATURE_PREFIX):
        return False
    expected = sign(body, secret)
    return hmac.compare_digest(expected, signature_header)


def verify_timestamp(
    timestamp_header: str, *, skew_seconds: int | None = None, now: float | None = None,
) -> bool:
    """
    Reject if the timestamp is more than `skew_seconds` from `now`. Defaults pull
    from settings.WEBHOOK_HMAC_TIMESTAMP_SKEW_SECONDS.
    """
    if not timestamp_header:
        return False
    try:
        ts = float(timestamp_header)
    except (TypeError, ValueError):
        return False
    skew = skew_seconds if skew_seconds is not None else settings.WEBHOOK_HMAC_TIMESTAMP_SKEW_SECONDS
    current = now if now is not None else time.time()
    return abs(current - ts) <= skew


def now_timestamp_header() -> str:
    """Stringified Unix seconds for the outbound X-TrueLive-Timestamp header."""
    return str(int(time.time()))
