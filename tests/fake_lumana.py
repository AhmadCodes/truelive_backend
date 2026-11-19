"""
A local stand-in for Lumana's playback API (127.0.0.1, ephemeral port), built
from what LUM-DESIGN measured live:

* ``GET /v2/playback/start?cameraId=&sessionId=`` with ``Authorization:
  Bearer <key>``: 401 for a wrong key, 400 "camera not found" for an unknown
  camera (no session), else ``{url, token: {token}, sessionId,
  currentSessions}``. A known ``sessionId`` gives the same URL and a fresh
  token and reopens a closed session; without one a new session is made. At
  ``limit`` open sessions a new one is refused with 500.
* the playlist URL: 403 for a token that was never issued or is past its
  ``exp``, 500 for a closed session, else a playlist.
* ``GET /v2/playback/stop``: 200, then 404.

``clock`` decides token times, ``lifetime`` their length, ``down`` makes every
API call fail with 502, ``start_delay`` slows a start. ``log`` holds
``(what, camera id, session id, key)`` per request; tokens are synthetic.
"""

import base64
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class FakeLumana:
    def __init__(self, keys: Set[str], cameras: Set[str], clock: Callable[[], float] = time.time,
                 lifetime: int = 18000, limit: int = 10):
        self.keys = set(keys)
        self.cameras = set(cameras)
        self.clock = clock
        self.lifetime = lifetime
        self.limit = limit
        self.down = False
        self.start_delay = 0.0
        self.extra_sessions = 0  # sessions "other users of the account" hold
        self.sessions: Dict[str, dict] = {}  # id -> {camera, open}
        self.tokens: Dict[str, float] = {}   # token -> exp
        self.log: List[Tuple[str, Optional[str], Optional[str], Optional[str]]] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                outer._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # -- helpers for tests ------------------------------------------------------

    def count(self, what: str) -> int:
        with self.lock:
            return sum(1 for entry in self.log if entry[0] == what)

    def open_sessions(self) -> int:
        with self.lock:
            return sum(1 for s in self.sessions.values() if s["open"])

    def close_session(self, session_id: Optional[str] = None) -> None:
        """Idle-close one session, or all of them."""
        with self.lock:
            for sid, session in self.sessions.items():
                if session_id in (None, sid):
                    session["open"] = False

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- the API ----------------------------------------------------------------

    def _send(self, handler, status: int, body, content_type="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _handle(self, handler) -> None:
        parsed = urlparse(handler.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if parsed.path.startswith("/v2/file/"):
            return self._playlist(handler, parsed.path, query)
        auth = handler.headers.get("Authorization") or ""
        key = auth[7:] if auth.startswith("Bearer ") else None
        what = parsed.path.rsplit("/", 1)[-1]
        with self.lock:
            self.log.append((what, query.get("cameraId"), query.get("sessionId"), key))
        if self.down:
            return self._send(handler, 502, {"message": "bad gateway"})
        if key is None:
            return self._send(handler, 401, {"message": "missing token"})
        if key not in self.keys:
            return self._send(handler, 401, {"message": "invalid token"})
        if parsed.path == "/v2/playback/start":
            return self._start(handler, query)
        if parsed.path == "/v2/playback/stop":
            with self.lock:
                session = self.sessions.get(query.get("sessionId") or "")
                if session is None or not session["open"]:
                    return self._send(handler, 404, {"message": "Playback session not found."})
                session["open"] = False
            return self._send(handler, 200, {"sessionId": query["sessionId"]})
        return self._send(handler, 404, {"message": "not found"})

    def _start(self, handler, query) -> None:
        if self.start_delay:
            time.sleep(self.start_delay)
        camera = query.get("cameraId")
        if camera not in self.cameras:
            return self._send(handler, 400, {"message": "camera not found"})
        with self.lock:
            session_id = query.get("sessionId")
            session = self.sessions.get(session_id) if session_id else None
            opened = sum(1 for s in self.sessions.values() if s["open"]) + self.extra_sessions
            if session is None or not session["open"]:
                if opened >= self.limit:
                    return self._send(handler, 500, {"message": "Internal server error"})
                opened += 1
            if session is None:
                session_id = session_id or secrets.token_hex(8)
                session = self.sessions[session_id] = {"camera": camera, "open": True}
            session["open"] = True
            now = self.clock()
            token = ".".join((_b64({"alg": "none"}), _b64({"iat": int(now), "exp": int(now) + self.lifetime,
                                                         "tokenId": secrets.token_hex(6)}), "sig"))
            self.tokens[token] = now + self.lifetime
        self._send(handler, 200, {
            "url": f"{self.url}/v2/file/{camera}/{session_id}/manifest.m3u8",
            "token": {"token": token},
            "sessionId": session_id,
            "currentSessions": opened,
        })

    def _playlist(self, handler, path: str, query) -> None:
        session_id = path.split("/")[4] if len(path.split("/")) > 4 else ""
        with self.lock:
            self.log.append(("playlist", None, session_id, None))
            expires = self.tokens.get(query.get("token") or "")
            session = self.sessions.get(session_id)
        if expires is None or expires <= self.clock():
            return self._send(handler, 403, b"Invalid JWT token: fixture", "text/plain")
        if session is None or not session["open"]:
            return self._send(handler, 500, b"Failed to stream file...", "text/plain")
        self._send(handler, 200, b"#EXTM3U\n#EXT-X-TARGETDURATION:3\n", "application/vnd.apple.mpegurl")
