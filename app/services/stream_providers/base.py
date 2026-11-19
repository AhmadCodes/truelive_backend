"""
The stream provider interface. One subclass per vendor; the engine
(engine.py) is written against this and knows no vendor.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import httpx

# ProviderError codes.
AUTH = "auth"                # the provider refused the credentials
NOT_FOUND = "not_found"      # the provider does not know the camera
LIMIT = "limit"              # a session or rate limit is reached
UNAVAILABLE = "unavailable"  # no answer, or an answer that is not usable
INVALID = "invalid"          # the stored settings are not usable


@dataclass(frozen=True)
class ProviderField:
    name: str
    label: str
    kind: str  # "text" | "url" | "secret" | "int"
    required: bool = True
    default: Any = None
    help: str = ""


@dataclass
class Address:
    url: str                     # the playable HLS address (holds the token)
    expires_at: Optional[float]  # unix time, None when the provider gives none
    handle: Dict[str, Any] = field(default_factory=dict)  # what the provider needs to renew or release
    info: Dict[str, Any] = field(default_factory=dict)    # non-secret facts for the operator


class ProviderError(Exception):
    """``message`` is operator text and never holds a secret."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StreamProvider:
    type_id: str = ""
    label: str = ""
    fields: Tuple[ProviderField, ...] = ()

    def check(self, settings: dict, http: httpx.Client) -> str:
        """Prove the settings work without opening a stream. Returns operator
        text, or raises ProviderError."""
        raise NotImplementedError

    def mint(self, settings: dict, camera_id: str, previous: Optional[Address], http: httpx.Client,
             last_info: Optional[dict] = None) -> Address:
        """A playable address for ``camera_id``. ``previous`` is the address
        minted before (renew it when the provider can); ``last_info`` is the
        ``info`` of this provider's latest successful mint for any camera."""
        raise NotImplementedError

    def release(self, settings: dict, camera_id: str, address: Address, http: httpx.Client) -> None:
        """Give the address back (nothing by default)."""

    def probe(self, address: Address, http: httpx.Client) -> bool:
        """True when the address answers with a playlist."""
        try:
            response = http.get(address.url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and response.text.lstrip().startswith("#EXTM3U")
