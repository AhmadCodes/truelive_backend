"""HLS review fixes (hls_card.md, HLS-SRV).

* GET /stream/test/{camera_id} needs authentication and never returns the
  stream URL's password or token (response field, ffprobe and OpenCV text).
* The relay masks the query of every http(s) URL in a station report before
  it is validated and stored (the D1 guard only knows userinfo).

No network: ffprobe and cv2 are replaced.
"""
import asyncio
import time
from types import SimpleNamespace

import cv2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import stream as stream_api
from app.database import get_db
from app.schemas import station_protocol as sp
from app.services import station_reports
from tests.relay_testkit import RelayHarness, import_relay

relay = import_relay()

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJmaXh0dXJlIjoidHJ1ZSJ9.Zml4dHVyZS1zaWc_-x"
HLS = f"https://hls.example.com:8443/live/cam1/manifest.m3u8?token={TOKEN}&u=op@example.com"
RTSP = "rtsp://" + "fixture-user" + ":" + "fixture-pw" + "@192.0.2.7:554/stream"


class _Db:
    def __init__(self, url):
        self.camera = SimpleNamespace(id="c1", name="Cam", rtsp_url=url)

    def query(self, _model):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return self.camera


# --------------------------------------------------------------------------- #
# GET /stream/test/{camera_id}
# --------------------------------------------------------------------------- #


def test_stream_test_endpoint_needs_authentication():
    app = FastAPI()
    app.include_router(stream_api.router, prefix="/api/v1/stream")
    app.dependency_overrides[get_db] = lambda: _Db(HLS)
    response = TestClient(app).get("/api/v1/stream/test/c1")
    assert response.status_code in (401, 403)
    assert TOKEN not in response.text


@pytest.mark.parametrize("url, secret", [(HLS, TOKEN), (RTSP, "fixture-pw")], ids=["hls", "rtsp"])
@pytest.mark.parametrize("opencv_raises", [False, True])
def test_stream_test_endpoint_never_returns_the_credential(url, secret, opencv_raises, monkeypatch):
    class _Proc:
        returncode = 1

        async def communicate(self):
            # ffprobe quotes its input, whole and (for HLS) a derived segment URL.
            text = f"[in] {url}: Server returned 403\nhttps://hls.example.com:8443/live/cam1/seg1.m4s?token={TOKEN} failed\n"
            return b"", text.encode()

    async def _spawn(*_args, **_kw):
        return _Proc()

    class _Cap:
        def __init__(self, opened_url):
            if opencv_raises:
                raise RuntimeError(f"cannot open {opened_url}")

        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(stream_api.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(cv2, "VideoCapture", _Cap)
    result = asyncio.run(stream_api.test_camera_stream("c1", _auth=object(), db=_Db(url)))
    text = repr(result)
    assert secret not in text and "op@example.com" not in text
    assert result["rtsp_url"].endswith("?<redacted>") or "<redacted>@" in result["rtsp_url"]
    assert "192.0.2.7" in text or "hls.example.com:8443" in text  # still says which camera host
    assert result["tests"]["ffmpeg"]["status"] == "failed"
    assert result["tests"]["opencv"]["status"] == ("error" if opencv_raises else "failed")


# --------------------------------------------------------------------------- #
# Station reports: the query of an http(s) URL is masked
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text, expected", [
    (HLS, "https://hls.example.com:8443/live/cam1/manifest.m3u8?<redacted>"),
    (f"HTTPS://h.example.com/p?token={TOKEN}", "HTTPS://h.example.com/p?<redacted>"),
    (f"http://h.example.com/p#{TOKEN}", "http://h.example.com/p?<redacted>"),
    (f"open failed: http://h.example.com/p?t={TOKEN} (403), again", "open failed: http://h.example.com/p?<redacted> (403), again"),
    # What Core (***), the Client (<redacted> values) and the backend already emit.
    ("https://h.example.com/p?token=***", "https://h.example.com/p?<redacted>"),
    ("https://h.example.com/p?token=<redacted>&x=1", "https://h.example.com/p?<redacted>"),
    ("https://h.example.com/p?<redacted>", "https://h.example.com/p?<redacted>"),
    ("https://h.example.com/p", "https://h.example.com/p"),
    # RTSP is not touched.
    ("rtsp://192.0.2.7:554/s?channel=1&subtype=0", "rtsp://192.0.2.7:554/s?channel=1&subtype=0"),
    ("no url here? none", "no url here? none"),
])
def test_mask_http_queries(text, expected):
    assert station_reports.mask_http_queries(text) == expected
    assert station_reports.mask_http_queries(expected) == expected  # idempotent


def test_mask_http_queries_reaches_nested_values_and_keys():
    data = {"a": [1, {"uri": HLS}], HLS: None, "n": 5}
    out = station_reports.mask_http_queries(data)
    assert TOKEN not in repr(out) and "op@example.com" not in repr(out)
    assert out["a"][1]["uri"].endswith("manifest.m3u8?<redacted>") and out["n"] == 5
    assert TOKEN in data["a"][1]["uri"]  # the input is not changed
    # The D1 guard alone would have let the token through (or, with "@", dropped the report).
    assert sp.find_userinfo({"uri": f"https://h.example.com/p?token={TOKEN}"}) is None


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "q" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", False)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)
    relay.enable_feature(sp.FEATURE_STATION_STATUS)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()
    relay.pending_deploy_events.clear()
    relay.health_buffer.clear()
    relay.report_ids.clear()


def test_station_status_with_an_hls_url_is_kept_without_its_token(h, caplog):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token, capabilities=[sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS])
    assert relay._registered_pc("pc-a") == "PC_A", h.errors("pc-a")
    with caplog.at_level("DEBUG"):
        ack = relay.station_status("pc-a", {
            "message_id": "hls-1", "ts": time.time(), "health": "degraded",
            "extra": {"sources": [{"uri": HLS, "state": "offline"}]},
            "readiness": [{"code": "CAMERA_AUTH", "detail": "403 from " + HLS}]})
    # Not rejected, although the query holds an "@" (which D1 reads as userinfo).
    assert ack["status"] == "ok"
    health = relay.health_buffer.peek("PC_A")
    assert TOKEN not in repr(health) and "op@example.com" not in repr(health)
    assert TOKEN not in caplog.text
    # RTSP userinfo outside free text still rejects the report (D1, unchanged).
    rejected = relay.station_status("pc-a", {"message_id": "hls-2", "ts": time.time(), "health": "ok",
                                             "extra": {"url": RTSP}})
    assert rejected["status"] == "rejected"
