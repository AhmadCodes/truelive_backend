"""
HLS camera URLs (HLS-SRV, 07_work/hls_card.md).

An http:// or https:// camera URL must reach the layout Core receives byte for
byte (its query string is the stream token), and that query string must not be
logged. RTSP handling stays exactly as it was.

    python -m pytest tests/unit/test_hls_urls.py -o addopts='' -p no:cacheprovider
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.api.v1 import stream as stream_api
from app.schemas.camera import CameraCreate, CameraUpdate
from app.services import config_generator, screenshot_service, snapshot_service, stream_config_generator
from app.services.actor import MASK, diff_fields, mask_payload
from app.utils.url_processor import (
    encode_rtsp_password,
    is_hls_url,
    redact_camera_url,
    try_encode_rtsp_password,
)

pytestmark = [pytest.mark.unit]

# Fixture token: JWT-shaped, plus the characters the old encoder mangled ("@", ":")
# and ones a careless urlencode would rewrite ("%2F", "+", "=", "&" order).
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJmaXh0dXJlIjoidHJ1ZSJ9.Zml4dHVyZS1zaWc_-x"
HLS_URLS = [
    f"https://hls.example.com/live/cam1/manifest.m3u8?token={TOKEN}",
    f"http://192.0.2.10:8080/live/cam1/index.m3u8?token={TOKEN}",
    f"https://hls.example.com:8443/v2/playback/abc?token={TOKEN}",            # no .m3u8 suffix
    f"https://hls.example.com:8443/p.m3u8?z=1&token={TOKEN}&a=x%2Fy+z&b=",     # order and escapes kept
    f"https://hls.example.com:8443/p.m3u8?user=op@example.com&sig=a:b@c",     # "@" and ":" in the query
    f"HTTPS://HLS.example.com/p.m3u8?token={TOKEN}",                          # scheme case
    f"  https://hls.example.com/p.m3u8?token={TOKEN}  ",                      # whitespace is Core's to trim
    "https://hls.example.com/p.m3u8",                                         # no query
    f"https://hls.example.com/p.m3u8?token={TOKEN}#frag@x:y",
    "https://hls.example.com/p.m3u8?token=" + "A" * 4000,                     # long token
]

# RTSP: (input, expected output), as the encoder behaved before HLS support.
RTSP_CASES = [
    ("rtsp://admin:password123@192.0.2.20:554/stream", "rtsp://admin:password123@192.0.2.20:554/stream"),
    ("rtsp://admin:shin@bet2015@cam.example.com:554/stream", "rtsp://admin:shin%40bet2015@cam.example.com:554/stream"),
    ("rtsp://admin:p#ss/w?rd@192.0.2.20:554/s?channel=1&subtype=1",
     "rtsp://admin:p%23ss%2Fw%3Frd@192.0.2.20:554/s?channel=1&subtype=1"),
    ("rtsps://admin:a b@192.0.2.20/s", "rtsps://admin:a%20b@192.0.2.20/s"),
    ("RTSP://admin:a@b@192.0.2.20/s", "RTSP://admin:a%40b@192.0.2.20/s"),
    ("rtsp://192.0.2.20:554/stream", "rtsp://192.0.2.20:554/stream"),
    ("rtsp://admin@192.0.2.20/stream", "rtsp://admin@192.0.2.20/stream"),
    ("not-a-url", "not-a-url"),
    ("", ""),
]


# --------------------------------------------------------------------------- #
# url_processor
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", HLS_URLS)
def test_hls_url_is_never_rewritten(url):
    assert encode_rtsp_password(url) == url
    assert try_encode_rtsp_password(url) == url
    assert is_hls_url(url)


@pytest.mark.parametrize("url,expected", RTSP_CASES)
def test_rtsp_encoding_is_unchanged(url, expected):
    assert encode_rtsp_password(url) == expected
    assert try_encode_rtsp_password(url) == expected
    assert not is_hls_url(url)


@pytest.mark.parametrize("value", [None, 5, "", "https", "httpx://h/p", "ftp://h/p", "rtsp://h/http://x"])
def test_is_hls_url_rejects_everything_else(value):
    assert not is_hls_url(value)


@pytest.mark.parametrize("url,expected", [
    (f"https://hls.example.com/live/cam1/manifest.m3u8?token={TOKEN}",
     "https://hls.example.com/live/cam1/manifest.m3u8?<redacted>"),
    ("https://hls.example.com:8443/p.m3u8?user=op@example.com&sig=a:b@c", "https://hls.example.com:8443/p.m3u8?<redacted>"),
    (f"https://hls.example.com/p.m3u8#{TOKEN}", "https://hls.example.com/p.m3u8?<redacted>"),
    ("https://user:pw@hls.example.com/p.m3u8?t=1", "https://<redacted>@hls.example.com/p.m3u8?<redacted>"),
    ("https://hls.example.com/p.m3u8", "https://hls.example.com/p.m3u8"),
    ("rtsp://admin:s3cr3t-pw@192.0.2.20:554/stream", "rtsp://<redacted>@192.0.2.20:554/stream"),
    ("rtsp://admin:s3/cr?3t@pw@192.0.2.20:554/s?channel=1", "rtsp://<redacted>@192.0.2.20:554/s?<redacted>"),
    ("rtsp://192.0.2.20:554/stream", "rtsp://192.0.2.20:554/stream"),
    ("s3cr3t-without-scheme", "<redacted>"),
    ("", ""),
    (None, ""),
])
def test_redact_camera_url(url, expected):
    assert redact_camera_url(url) == expected


@pytest.mark.parametrize("url", HLS_URLS)
def test_redacted_hls_url_holds_no_query(url):
    out = redact_camera_url(url)
    for secret in (TOKEN, "op@example.com", "a:b@c", "AAAA", "x%2Fy"):
        assert secret not in out


# --------------------------------------------------------------------------- #
# Entry: the API schemas accept the URL and keep it as typed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", HLS_URLS)
def test_camera_schemas_keep_the_url(url):
    created = CameraCreate(id="cam-hls", device_id="dev-1", name="HLS cam", rtsp_url=url)
    assert created.rtsp_url == url
    assert created.model_dump()["rtsp_url"] == url
    assert CameraUpdate(rtsp_url=url).model_dump(exclude_unset=True) == {"rtsp_url": url}


def test_audit_masks_the_whole_url():
    url = HLS_URLS[0]
    assert mask_payload("camera", {"name": "c", "rtsp_url": url}) == {"name": "c", "rtsp_url": MASK}
    diff = diff_fields("camera", {"rtsp_url": url}, {"rtsp_url": url + "x"})
    assert TOKEN not in repr(diff)


# --------------------------------------------------------------------------- #
# Generators: what goes into the layout
# --------------------------------------------------------------------------- #

class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    order_by = join = filter

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _OneCameraDb:
    """A session stand-in: every site layout row points at the one camera."""

    def __init__(self, camera):
        self.camera = camera

    def query(self, *entities):
        name = getattr(entities[0], "__name__", "")
        if name == "SiteCamerasLayout":
            return _Query([SimpleNamespace(camera_id=self.camera.id, slot_row=1, slot_col=1)])
        if name == "Camera":
            return _Query([self.camera])
        return _Query([])


@pytest.mark.parametrize("url", HLS_URLS)
def test_location_uris_carry_the_hls_url_unchanged(url, caplog):
    camera = SimpleNamespace(id="cam-hls", name="HLS cam", rtsp_url=url, device_id="dev-1")
    db = _OneCameraDb(camera)
    with caplog.at_level(logging.DEBUG):
        assert config_generator._get_location_uris("site-1", db) == [{"url": url, "osd_text": "HLS cam"}]
        uris = stream_config_generator.get_location_uris_for_camera(camera, db)
    assert [u["url"] for u in uris] == [url] * len(uris)
    if "?" in url:
        assert url.split("?", 1)[1] not in caplog.text


def test_location_uris_still_encode_rtsp_passwords():
    camera = SimpleNamespace(id="cam-r", name="R", rtsp_url=RTSP_CASES[1][0], device_id="dev-1")
    assert config_generator._get_location_uris("site-1", _OneCameraDb(camera))[0]["url"] == RTSP_CASES[1][1]


# --------------------------------------------------------------------------- #
# Logs of the capture services; ffmpeg options of the portal preview
# --------------------------------------------------------------------------- #

class _DeadCapture:
    def __init__(self, _url):
        pass

    def set(self, *_a):
        return True

    def read(self):
        return False, None

    def release(self):
        pass


@pytest.mark.parametrize("service,getter,capture", [
    (screenshot_service, "get_camera_screenshot", "capture_screenshot"),
    (snapshot_service, "get_camera_snapshot", "capture_snapshot"),
])
@pytest.mark.parametrize("url,secret", [
    (HLS_URLS[0], TOKEN),
    ("https://h.example.com/p?t=" + "short-tok", "short-tok"),          # inside the old [:50] window
    ("rtsp://admin:s3cr3t-pw@192.0.2.20:554/stream", "s3cr3t-pw"),
])
def test_capture_services_never_log_the_secret(service, getter, capture, url, secret, monkeypatch, caplog):
    monkeypatch.setattr(service.cv2, "VideoCapture", _DeadCapture)
    monkeypatch.setattr(service.time, "sleep", lambda _s: None)
    with caplog.at_level(logging.DEBUG):
        assert getattr(service, getter)(url, timeout=0.01) is None       # timeout path
        with pytest.raises(ValueError) as failure:
            monkeypatch.setattr(service, getter, lambda *_a, **_k: None)
            getattr(service, capture)(url)

        def boom(_url):
            raise RuntimeError("cannot open")

        monkeypatch.undo()
        monkeypatch.setattr(service.cv2, "VideoCapture", boom)
        assert getattr(service, getter)(url, timeout=0.01) is None       # error path
    assert "Timeout while capturing" in caplog.text and "Error capturing" in caplog.text
    assert secret not in caplog.text and secret not in str(failure.value)
    assert redact_camera_url(url) in caplog.text


@pytest.mark.parametrize("build", [stream_api._build_mpeg1_cmd, stream_api._build_h264_cmd])
def test_preview_ffmpeg_command(build):
    hls = build(HLS_URLS[0])
    assert "-rtsp_transport" not in hls
    assert hls[hls.index("-i") + 1] == HLS_URLS[0]

    rtsp_url = "rtsp://192.0.2.20:554/stream"
    rtsp = build(rtsp_url)
    assert rtsp[:3] == ["ffmpeg", "-rtsp_transport", "tcp"]
    assert rtsp[rtsp.index("-i") + 1] == rtsp_url
    hls_opts = ["-extension_picky", "0", "-headers", f"Authorization: {TOKEN}\r\n"]
    assert hls[1:5] == hls_opts
    # Only the input options differ between the two commands.
    assert [a for a in rtsp if a not in ("-rtsp_transport", "tcp", rtsp_url)] == [
        a for a in hls[:1] + hls[5:] if a != HLS_URLS[0]]


def test_ffmpeg_failure_log_keeps_the_error_and_masks_the_token(caplog):
    banner = "ffmpeg version 7.1 Copyright (c) 2000-2026 the FFmpeg developers\n" + "  configuration: --x\n" * 40
    error = f"Error opening input file {HLS_URLS[0]}\nServer returned 5XX Server Error reply\n"

    class _Stream:
        def __init__(self, data):
            self.data = data

        async def read(self, _n=-1):
            data, self.data = self.data, b""
            return data

    proc = SimpleNamespace(stdout=_Stream(b""), stderr=_Stream((banner + error).encode()), returncode=8)
    with caplog.at_level(logging.ERROR, logger=stream_api.logger.name):
        asyncio.run(stream_api._pump_ffmpeg_to_ws(None, proc, chunk_size=8, camera_id="c1", label="h264"))
    assert "Server returned 5XX" in caplog.text and "exit=8" in caplog.text
    assert TOKEN not in caplog.text and "hls.example.com" in caplog.text
