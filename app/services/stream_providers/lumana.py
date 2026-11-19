"""
Lumana (https://apidocs.lumana.ai). Measured 2026-11-20 (lumana_card.md):
a start with the stored sessionId returns the same playlist URL and a fresh
token (a JWT, 5 hours), so a renewal changes only the query string; a start
without it always opens a new session; a closed session is revived by a start
with its id; an unknown camera answers 400 and opens nothing.
"""
import base64
import json
from typing import Optional

import httpx

from app.services.stream_providers.base import (AUTH, NOT_FOUND, UNAVAILABLE, Address, ProviderError,
                                                ProviderField, StreamProvider)

DEFAULT_BASE_URL = "https://access.lumana.ai"
CHECK_CAMERA_ID = "000000000000000000000000"  # never a real camera: the start answers 400 when the key works


def jwt_exp(token: str) -> Optional[float]:
    """The ``exp`` claim, read without verifying (the token is the vendor's)."""
    try:
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return float(claims["exp"])
    except Exception:
        return None


class LumanaProvider(StreamProvider):
    type_id = "lumana"
    label = "Lumana"
    fields = (
        ProviderField("api_key", "API key", "secret", help="Sent to Lumana only; never shown again."),
        ProviderField("base_url", "API address", "url", required=False, default=DEFAULT_BASE_URL,
                      help="Leave the default unless Lumana gave you another address."),
    )

    def _get(self, settings: dict, path: str, params: dict, http: httpx.Client) -> httpx.Response:
        base = (settings.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        try:
            return http.get(base + path, params=params,
                            headers={"Authorization": f"Bearer {settings.get('api_key') or ''}"})
        except httpx.HTTPError:
            # No chained text: an httpx error names the request URL.
            raise ProviderError(UNAVAILABLE, "Lumana did not answer") from None

    def _start(self, settings: dict, camera_id: str, session_id: Optional[str], http: httpx.Client,
               sessions: Optional[int] = None) -> dict:
        params = {"cameraId": camera_id, "start": 0, "end": 0, "smartStorage": "false", "type": "HLS"}
        if session_id:
            params["sessionId"] = session_id
        response = self._get(settings, "/v2/playback/start", params, http)
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except ValueError:
                raise ProviderError(UNAVAILABLE, "Lumana sent an answer that cannot be read") from None
        if status in (401, 403):
            raise ProviderError(AUTH, "Lumana refused the API key")
        if status == 404 or (status == 400 and "camera not found" in response.text.lower()):
            raise ProviderError(NOT_FOUND, "Lumana does not know this camera id")
        if status == 503:
            raise ProviderError(UNAVAILABLE, "The camera's Lumana edge device is not connected")
        # What Lumana answers at its session limit is not known (the limit is per Lumana Core:
        # 5 per camera, at least 12, and its own apps count too), so no status is called "limit":
        # the operator gets the status and the last session count.
        if isinstance(sessions, int):
            raise ProviderError(UNAVAILABLE, f"Lumana answered HTTP {status}; {sessions} live session(s) were open "
                                             f"at the last count")
        raise ProviderError(UNAVAILABLE, f"Lumana answered HTTP {status}")

    def check(self, settings: dict, http: httpx.Client) -> str:
        try:
            self._start(settings, CHECK_CAMERA_ID, None, http)
        except ProviderError as exc:
            if exc.code != NOT_FOUND:
                raise
        return "Lumana accepted the API key"

    def mint(self, settings: dict, camera_id: str, previous: Optional[Address], http: httpx.Client,
             last_info: Optional[dict] = None) -> Address:
        session_id = (previous.handle or {}).get("session_id") if previous else None
        data = self._start(settings, camera_id, session_id, http, (last_info or {}).get("sessions"))
        token = (data.get("token") or {}).get("token") if isinstance(data.get("token"), dict) else None
        url = data.get("url")
        if not url or not token:
            raise ProviderError(UNAVAILABLE, "Lumana sent an answer without a stream address")
        return Address(
            url=f"{url}?token={token}",
            expires_at=jwt_exp(token),
            handle={"session_id": data.get("sessionId") or session_id},
            info={"sessions": data.get("currentSessions")},
        )

    def release(self, settings: dict, camera_id: str, address: Address, http: httpx.Client) -> None:
        session_id = (address.handle or {}).get("session_id")
        if not session_id:
            return
        try:  # 200 and 404 are both fine; a session also closes itself after 90 s idle
            self._get(settings, "/v2/playback/stop", {"cameraId": camera_id, "sessionId": session_id}, http)
        except ProviderError:
            pass
