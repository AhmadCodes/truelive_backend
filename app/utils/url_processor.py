"""
URL processing utilities.

This module provides functions for encoding RTSP URLs with special characters in passwords.
"""
from urllib.parse import quote, unquote
import logging
import re

logger = logging.getLogger(__name__)

# HLS camera URLs (http/https). Core picks the HLS path by scheme, so these must
# reach the layout byte for byte: the query string carries the stream token.
_HLS_SCHEMES = ("http", "https")
_QUERY_START = re.compile(r"[?#]")


def is_hls_url(url: str) -> bool:
    """True for an http:// or https:// camera URL (scheme compared trimmed, case-insensitively)."""
    if not isinstance(url, str):
        return False
    scheme, sep, _ = url.strip().partition("://")
    return bool(sep) and scheme.lower() in _HLS_SCHEMES


# A camera of a stream provider is stored as provider://<provider id>/<camera id
# at the provider, percent-encoded> (app/services/stream_providers). It holds no
# secret and never reaches a station: the deploy path replaces it.
PROVIDER_SCHEME = "provider"


def is_provider_ref(url) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith(PROVIDER_SCHEME + "://")


def parse_provider_ref(url):
    """``(provider_id, camera_id)`` of a provider reference, else None."""
    if not is_provider_ref(url):
        return None
    provider_id, sep, camera = url.strip()[len(PROVIDER_SCHEME) + 3:].partition("/")
    camera = unquote(camera)
    if not sep or not provider_id or not camera:
        return None
    return provider_id, camera


def make_provider_ref(provider_id: str, camera_id: str) -> str:
    return f"{PROVIDER_SCHEME}://{provider_id}/{quote(camera_id, safe='')}"


def redact_camera_url(url: str) -> str:
    """A camera URL that is safe to log: userinfo and the query string are masked.

    RTSP: user:pass (which may contain / ? # @) is masked, then any query.
    HLS: the query (the token) is cut first, so an "@" inside it is not taken for userinfo.
    """
    if not url or not isinstance(url, str):
        return ""
    if is_provider_ref(url):
        return url.strip()
    scheme, sep, rest = url.strip().partition("://")
    if not sep:
        return "<redacted>"
    hls = scheme.lower() in _HLS_SCHEMES
    had_query = False
    if hls:
        rest, had_query = _cut_query(rest)
    if "@" in rest:
        rest = "<redacted>@" + rest.rsplit("@", 1)[1]
    if not hls:
        rest, had_query = _cut_query(rest)
    return f"{scheme}://{rest}" + ("?<redacted>" if had_query else "")


def _cut_query(rest: str):
    m = _QUERY_START.search(rest)
    return (rest[: m.start()], True) if m else (rest, False)


def encode_rtsp_password(rtsp_url: str) -> str:
    """
    Encode special characters in RTSP password for URL safety.

    Handles RTSP URLs where the password contains special characters like @, #, etc.
    that need to be URL-encoded to work properly.

    Example:
        Input:  rtsp://admin:shin@bet2015@server.com:554/stream
        Output: rtsp://admin:shin%40bet2015@server.com:554/stream

    Args:
        rtsp_url: RTSP URL string with username:password@host format

    Returns:
        RTSP URL with encoded password

    Raises:
        ValueError: If URL format is invalid
    """
    if not rtsp_url or not isinstance(rtsp_url, str):
        logger.warning("Empty or invalid RTSP URL provided")
        return rtsp_url or ""

    try:
        # Split the URL into protocol and rest
        if '://' not in rtsp_url:
            raise ValueError("Invalid RTSP URL format - missing protocol")

        prefix, rest = rtsp_url.split('://', 1)

        # HLS (http/https): never rewritten. An "@" or ":" in its query string is
        # token data, not user:pass, and encoding it would break the stream URL.
        # A provider reference is not a URL to encode either.
        if prefix.strip().lower() in _HLS_SCHEMES or prefix.strip().lower() == PROVIDER_SCHEME:
            return rtsp_url

        # Split at @ to separate user_info from host_info
        if '@' not in rest:
            # No credentials in URL, return as-is
            return rtsp_url

        splitted_url = rest.split('@')

        # Join all parts except the last one (which is the host)
        # This handles passwords with @ characters in them
        user_info = "@".join(splitted_url[:-1])
        host_info = splitted_url[-1]

        # Split user_info into username and password
        if ':' not in user_info:
            # No password in URL, return as-is
            return rtsp_url

        username, password = user_info.split(':', 1)

        # Encode the password (encode all special characters)
        encoded_password = quote(password, safe='')

        # Reconstruct the RTSP URL
        encoded_rtsp_url = f"{prefix}://{username}:{encoded_password}@{host_info}"

        return encoded_rtsp_url

    except Exception as e:
        logger.error(f"Error encoding RTSP password: {e}")
        # Return original URL as fallback
        return rtsp_url


def try_encode_rtsp_password(rtsp_url: str) -> str:
    """
    Safely encodes RTSP password, handling any exceptions that might occur.

    This is a wrapper around encode_rtsp_password that ensures it never crashes
    and always returns a valid string (either encoded or original).

    Args:
        rtsp_url: The RTSP URL to encode the password for

    Returns:
        The URL with encoded password, or the original URL if an error occurred
    """
    if not rtsp_url:
        return ""

    try:
        return encode_rtsp_password(rtsp_url)
    except Exception as e:
        logger.error(f"Error encoding RTSP password for URL: {e}")
        # Return the original URL as a fallback
        return rtsp_url
