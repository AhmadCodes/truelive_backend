"""
Relay trust helpers shared by the API and the relay (plan 06 D12).

The relay (app/services/websocket_server.py) runs under eventlet and the API
under uvicorn; this module imports neither, so both can use it.

Sender sessions. The portal API opens a Socket.IO session to the relay to push
layouts ("sender"). It identifies itself in the Socket.IO CONNECT packet's auth
payload, never in the URL:

    {"role": "sender", "secret": <RELAY_SENDER_SECRET>}

* RELAY_SENDER_SECRET set on the relay: a session whose auth says
  ``role=sender`` must carry the same secret, or the connection is refused.
  Only such sessions may send ``message``, and only they receive the presence
  list (clients_update / clients_list).
* RELAY_SENDER_SECRET unset: the relay runs *legacy-open*. Any session that has
  not registered as a PC may send ``message`` (the behaviour before D12), with
  a warning in the relay log.

Station tokens. ``token_digest`` compares PC tokens without keeping or logging
them: the relay compares sha256 digests in constant time.
"""

import hashlib
import hmac
from typing import Any, Dict, Optional

SENDER_ROLE = "sender"
AUTH_ROLE_KEY = "role"
AUTH_SECRET_KEY = "secret"


def sender_auth(secret: Optional[str]) -> Dict[str, str]:
    """The Socket.IO ``auth`` payload of a portal sender session.

    The role is always sent, so a relay with a secret refuses a sender that
    lacks it (a clear connection error) instead of treating it as a PC.
    """
    return {AUTH_ROLE_KEY: SENDER_ROLE, AUTH_SECRET_KEY: secret or ""}


def claims_sender(auth: Any) -> bool:
    """True when a CONNECT auth payload asks for a sender session."""
    return isinstance(auth, dict) and auth.get(AUTH_ROLE_KEY) == SENDER_ROLE


def secret_matches(presented: Any, expected: Optional[str]) -> bool:
    """Constant-time comparison. An empty expected secret never matches."""
    if not expected or not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def token_digest(token: Optional[str]) -> Optional[str]:
    """sha256 hex of a PC token, or None for an empty token."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def same_token(presented: Optional[str], stored: Optional[str]) -> bool:
    """True when two PC tokens are identical (constant time on the digests)."""
    a, b = token_digest(presented), token_digest(stored)
    if a is None or b is None:
        return False
    return hmac.compare_digest(a, b)
