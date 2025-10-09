"""
URL processing utilities.

This module provides functions for encoding RTSP URLs with special characters in passwords.
"""
from urllib.parse import quote
import logging

logger = logging.getLogger(__name__)


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
