"""
WebSocket streaming endpoints for RTSP camera feeds.

Cleanup guarantees for both the DB session and the FFmpeg subprocess are
central to correctness — they were the root cause of two production outages
(2026-06-18, 2026-07-01) traced to leaks in this module. See
`experiments/incidents/2026-06-18_portal_slowdown_and_resource_leaks.md`.

Design invariants:
  * DB session lifetime is scoped to the camera lookup only, never held
    across the long stream loop.
  * FFmpeg subprocesses are always spawned inside an async context manager
    that reaps on exit.
  * All blocking I/O runs on the event loop (async subprocess pipes,
    asyncio.to_thread for OpenCV).
  * A per-worker semaphore caps concurrent streams as defense in depth.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.database import SessionLocal, get_db
from app.models.camera import Camera

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_CONCURRENT_STREAMS = int(os.environ.get("STREAM_MAX_CONCURRENT_PER_WORKER", "16"))
FFMPEG_REAP_TIMEOUT_SEC = float(os.environ.get("STREAM_FFMPEG_REAP_TIMEOUT_SEC", "5"))
STREAM_CHUNK_SIZE_MPEG1 = 1024
STREAM_CHUNK_SIZE_H264 = 8192
JPEG_FPS = 30
_JPEG_FRAME_INTERVAL_SEC = 1.0 / JPEG_FPS

_stream_semaphore = asyncio.Semaphore(MAX_CONCURRENT_STREAMS)


# ---------------------------------------------------------------------------
# Lifecycle context managers — the guarantee that we don't leak resources
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _db_session_scope():
    """Short-lived DB session guaranteed to release on exit.

    Use for one-shot lookups only — never hold across an await that could
    block for more than milliseconds. We create the session via
    SessionLocal() directly rather than through Depends(get_db) so the
    lifetime is bounded by this `async with`, not by the entire endpoint
    function's return.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        with suppress(Exception):
            db.rollback()  # release any open transaction (defensive)
        with suppress(Exception):
            db.close()


@asynccontextmanager
async def _ffmpeg_proc(cmd: list[str]):
    """Spawn an FFmpeg subprocess with async pipes; guaranteed reap on exit.

    Using asyncio.create_subprocess_exec (not subprocess.Popen) so the read
    loop below runs on the event loop and integrates with FastAPI's
    cancellation. The kill+wait in the finally block never propagates —
    even if ffmpeg is already gone or wait times out, the outer scope must
    still complete.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), timeout=FFMPEG_REAP_TIMEOUT_SEC)


@asynccontextmanager
async def _opencv_capture(rtsp_url: str):
    """Open a cv2.VideoCapture; guaranteed release() on exit.

    OpenCV is CPU-bound C code — cap.read() must be wrapped in
    asyncio.to_thread by callers so the event loop stays free.
    """
    import cv2  # local import so the module still loads if opencv is missing

    cap = await asyncio.to_thread(cv2.VideoCapture, rtsp_url)
    try:
        yield cap
    finally:
        with suppress(Exception):
            await asyncio.to_thread(cap.release)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _validate_ws_token(token: Optional[str]) -> bool:
    """Return True iff `token` decodes to a valid JWT payload."""
    if not token:
        return False
    try:
        payload = decode_token(token)
    except Exception as exc:
        logger.error("WebSocket token verification failed: %s", exc)
        return False
    return bool(payload)


async def _resolve_camera_rtsp(camera_id: str) -> Optional[str]:
    """Look up a camera's RTSP URL. Session is opened+closed inside this call."""
    async with _db_session_scope() as db:
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if camera is None:
            return None
        return camera.rtsp_url or None


def _build_mpeg1_cmd(rtsp_url: str) -> list[str]:
    """FFmpeg command for MPEG1-TS output over WebSocket (JSMpeg player)."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-f", "mpegts",
        "-codec:v", "mpeg1video",
        "-s", "640x480",
        "-b:v", "1000k",
        "-bf", "0",
        "-muxdelay", "0.001",
        "-r", "25",
        "-an",
        "pipe:1",
    ]


def _build_h264_cmd(rtsp_url: str) -> list[str]:
    """FFmpeg command for fragmented-MP4 H.264 output (browser MSE)."""
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-i", rtsp_url,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", "20",
        "-keyint_min", "10",
        "-sc_threshold", "0",
        "-s", "1280x720",
        "-b:v", "800k",
        "-maxrate", "900k",
        "-bufsize", "1800k",
        "-r", "10",
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-an",
        "pipe:1",
    ]


async def _pump_ffmpeg_to_ws(
    ws: WebSocket,
    proc: asyncio.subprocess.Process,
    *,
    chunk_size: int,
    camera_id: str,
    label: str,
) -> None:
    """Read from proc.stdout, forward to ws. Exits on EOF, error, or client close.

    A WebSocketDisconnect raised by ws.send_bytes propagates out — the outer
    handler catches it. Never hangs on a dead pipe: proc.stdout.read returns
    b'' when ffmpeg exits.
    """
    total_bytes = 0
    chunks = 0
    try:
        while True:
            chunk = await proc.stdout.read(chunk_size)
            if not chunk:
                # EOF — ffmpeg has closed its stdout. Grab any stderr for the log.
                stderr_bytes = b""
                with suppress(Exception):
                    stderr_bytes = await asyncio.wait_for(
                        proc.stderr.read(), timeout=1.0
                    )
                rc = proc.returncode
                if rc is None:
                    with suppress(ProcessLookupError):
                        proc.kill()
                if rc is not None and rc != 0:
                    logger.error(
                        "ffmpeg exited with error for camera %s (%s) exit=%s stderr=%r",
                        camera_id, label, rc,
                        stderr_bytes.decode("utf-8", errors="ignore")[:500],
                    )
                return
            await ws.send_bytes(chunk)
            total_bytes += len(chunk)
            chunks += 1
    finally:
        logger.info(
            "stream pump exit camera=%s label=%s chunks=%d bytes=%d",
            camera_id, label, chunks, total_bytes,
        )


def _semaphore_available() -> bool:
    """True if at least one slot is free in the concurrency cap."""
    # Semaphore._value is a private attribute but is the documented way to
    # check availability without acquiring. Fine for a soft check before
    # deciding whether to accept a new stream.
    return _stream_semaphore._value > 0  # noqa: SLF001


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/info", tags=["Streaming"])
async def get_streaming_info():
    """
    Get information about available WebSocket streaming endpoints.

    WebSocket endpoints are not displayed in Swagger UI because OpenAPI 3.0
    doesn't support WebSocket documentation. Use this endpoint to discover
    available streaming options.
    """
    return {
        "message": "WebSocket streaming endpoints available",
        "note": "WebSocket endpoints don't appear in Swagger UI (OpenAPI limitation)",
        "endpoints": [
            {
                "name": "MPEG1 Stream (FFmpeg-based)",
                "method": "WebSocket",
                "url": "ws://<host>/api/v1/stream/ws/camera/{camera_id}?token={jwt_token}",
                "description": "Stream RTSP camera as MPEG1 for JSMpeg player",
                "features": {"resolution": "640x480", "fps": 25, "bitrate": "1000k",
                             "format": "MPEG1", "latency": "low"},
                "requirements": ["FFmpeg installed on server", "JSMpeg player on client",
                                 "Valid JWT token"],
            },
            {
                "name": "JPEG Stream (OpenCV-based)",
                "method": "WebSocket",
                "url": "ws://<host>/api/v1/stream/ws/camera/{camera_id}/jpeg?token={jwt_token}",
                "description": "Stream camera as individual JPEG frames",
                "features": {"resolution": "640x480", "fps": JPEG_FPS,
                             "format": "JPEG", "quality": "80%"},
                "requirements": ["OpenCV (opencv-python) installed",
                                 "Custom frame renderer on client", "Valid JWT token"],
            },
            {
                "name": "H.264 fMP4 Stream (low latency)",
                "method": "WebSocket",
                "url": "ws://<host>/api/v1/stream/ws/camera/{camera_id}/h264?token={jwt_token}",
                "description": "Fragmented-MP4 H.264 for browser Media Source Extensions",
                "features": {"resolution": "1280x720", "fps": 10, "bitrate": "800k",
                             "format": "fMP4 (H.264)", "latency": "~300-500ms"},
                "requirements": ["FFmpeg installed on server", "MSE-capable browser",
                                 "Valid JWT token"],
            },
        ],
        "authentication": {
            "method": "JWT token as query parameter",
            "example": "?token=<jwt>",
            "how_to_get_token": "POST /api/v1/auth/login",
        },
        "limits": {
            "max_concurrent_streams_per_worker": MAX_CONCURRENT_STREAMS,
            "close_code_when_full": 1013,
        },
    }


async def _run_stream(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str],
    build_cmd,
    chunk_size: int,
    label: str,
) -> None:
    """Shared FFmpeg-based stream driver (MPEG1 and H.264).

    Encapsulates the accept -> auth -> lookup -> pump -> cleanup lifecycle
    with guaranteed teardown of both the DB session and the FFmpeg process.
    """
    await websocket.accept()
    try:
        if not _validate_ws_token(token):
            await websocket.close(code=1008, reason="Authentication required: invalid or expired token")
            return

        rtsp_url = await _resolve_camera_rtsp(camera_id)
        if rtsp_url is None:
            await websocket.close(code=1008, reason=f"Camera '{camera_id}' not found or has no RTSP URL")
            return
        # DB session is closed here. From this point on we hold no DB resources.

        if not _semaphore_available():
            await websocket.close(code=1013, reason="Too many concurrent streams; try again shortly")
            return

        async with _stream_semaphore:
            logger.info("stream starting camera=%s label=%s", camera_id, label)
            async with _ffmpeg_proc(build_cmd(rtsp_url)) as proc:
                await _pump_ffmpeg_to_ws(
                    websocket, proc,
                    chunk_size=chunk_size, camera_id=camera_id, label=label,
                )

    except WebSocketDisconnect:
        logger.info("client disconnected camera=%s label=%s", camera_id, label)
    except Exception:
        logger.exception("stream failed camera=%s label=%s", camera_id, label)
        with suppress(Exception):
            await websocket.close(code=1011, reason="Internal server error")
    finally:
        with suppress(Exception):
            await websocket.close()


@router.websocket("/ws/camera/{camera_id}")
async def stream_camera(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication"),
) -> None:
    """WebSocket: MPEG1-TS stream (JSMpeg player)."""
    await _run_stream(
        websocket, camera_id, token,
        build_cmd=_build_mpeg1_cmd,
        chunk_size=STREAM_CHUNK_SIZE_MPEG1,
        label="mpeg1",
    )


@router.websocket("/ws/camera/{camera_id}/h264")
async def stream_camera_h264(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication"),
) -> None:
    """WebSocket: fragmented-MP4 H.264 stream (browser MSE, ~300-500ms latency)."""
    await _run_stream(
        websocket, camera_id, token,
        build_cmd=_build_h264_cmd,
        chunk_size=STREAM_CHUNK_SIZE_H264,
        label="h264",
    )


@router.websocket("/ws/camera/{camera_id}/jpeg")
async def stream_camera_jpeg(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication"),
) -> None:
    """WebSocket: JPEG frames via OpenCV (no FFmpeg required).

    Cleanup shape matches the FFmpeg handlers — same guarantees on DB
    session and VideoCapture release.
    """
    await websocket.accept()
    try:
        if not _validate_ws_token(token):
            await websocket.close(code=1008, reason="Authentication required: invalid or expired token")
            return

        rtsp_url = await _resolve_camera_rtsp(camera_id)
        if rtsp_url is None:
            await websocket.close(code=1008, reason=f"Camera '{camera_id}' not found or has no RTSP URL")
            return

        try:
            import cv2  # noqa: F401
        except ImportError:
            logger.error("opencv-python not installed; JPEG endpoint unavailable")
            await websocket.close(code=1011, reason="OpenCV not installed on server")
            return

        if not _semaphore_available():
            await websocket.close(code=1013, reason="Too many concurrent streams; try again shortly")
            return

        async with _stream_semaphore:
            logger.info("stream starting camera=%s label=jpeg", camera_id)
            frame_count = 0
            try:
                async with _opencv_capture(rtsp_url) as cap:
                    is_open = await asyncio.to_thread(cap.isOpened)
                    if not is_open:
                        await websocket.close(code=1011, reason="Failed to open RTSP stream")
                        logger.error("cv2 failed to open RTSP for camera %s", camera_id)
                        return
                    import cv2 as _cv2
                    while True:
                        ret, frame = await asyncio.to_thread(cap.read)
                        if not ret or frame is None:
                            logger.warning("cv2 read failed for camera %s (source ended?)", camera_id)
                            break
                        frame = await asyncio.to_thread(_cv2.resize, frame, (640, 480))
                        ok, buf = await asyncio.to_thread(
                            _cv2.imencode, ".jpg", frame,
                            [_cv2.IMWRITE_JPEG_QUALITY, 80],
                        )
                        if not ok:
                            continue
                        await websocket.send_bytes(buf.tobytes())
                        frame_count += 1
                        await asyncio.sleep(_JPEG_FRAME_INTERVAL_SEC)
            finally:
                logger.info("jpeg stream ended camera=%s frames=%d", camera_id, frame_count)

    except WebSocketDisconnect:
        logger.info("client disconnected camera=%s label=jpeg", camera_id)
    except Exception:
        logger.exception("jpeg stream failed camera=%s", camera_id)
        with suppress(Exception):
            await websocket.close(code=1011, reason="Internal server error")
    finally:
        with suppress(Exception):
            await websocket.close()


@router.get("/test/{camera_id}", tags=["Streaming"])
async def test_camera_stream(
    camera_id: str,
    db: Session = Depends(get_db),
):
    """
    Test camera RTSP connectivity and return diagnostic information.

    Same defect class as the WebSocket handlers used to have: this
    endpoint used subprocess.run() and cv2.VideoCapture directly on the
    event loop, blocking the worker for up to 10s per call. Rewritten to
    use asyncio.create_subprocess_exec + asyncio.to_thread so the worker
    stays responsive.
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        return {"status": "error", "message": f"Camera {camera_id} not found"}
    rtsp_url = camera.rtsp_url
    if not rtsp_url:
        return {"status": "error", "message": "Camera has no RTSP URL"}

    result: dict = {
        "camera_id": camera_id,
        "camera_name": camera.name,
        "rtsp_url": rtsp_url,
        "tests": {},
    }

    # Test 1: ffprobe via async subprocess (non-blocking)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-rtsp_transport", "tcp", rtsp_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            result["tests"]["ffmpeg"] = {"status": "timeout", "message": "FFmpeg connection timed out"}
        else:
            if proc.returncode == 0:
                result["tests"]["ffmpeg"] = {"status": "success", "message": "FFmpeg can access stream"}
            else:
                stderr_text = stderr.decode("utf-8", errors="ignore")
                error_lines = stderr_text.strip().split("\n")[-5:]
                result["tests"]["ffmpeg"] = {
                    "status": "failed",
                    "exit_code": proc.returncode,
                    "error": "\n".join(error_lines),
                }
    except FileNotFoundError:
        result["tests"]["ffmpeg"] = {"status": "error", "message": "ffprobe binary not found on PATH"}
    except Exception as exc:
        result["tests"]["ffmpeg"] = {"status": "error", "message": str(exc)}

    # Test 2: OpenCV via to_thread (non-blocking)
    try:
        import cv2

        async def _opencv_test() -> dict:
            cap = await asyncio.to_thread(cv2.VideoCapture, rtsp_url)
            try:
                if not await asyncio.to_thread(cap.isOpened):
                    return {"status": "failed", "message": "OpenCV cannot open stream"}
                ret, _frame = await asyncio.to_thread(cap.read)
                if not ret:
                    return {"status": "failed", "message": "OpenCV opened but cannot read frames"}
                return {"status": "success", "message": "OpenCV can read frames"}
            finally:
                with suppress(Exception):
                    await asyncio.to_thread(cap.release)

        result["tests"]["opencv"] = await asyncio.wait_for(_opencv_test(), timeout=10)
    except asyncio.TimeoutError:
        result["tests"]["opencv"] = {"status": "timeout", "message": "OpenCV probe timed out"}
    except ImportError:
        result["tests"]["opencv"] = {"status": "error", "message": "opencv-python not installed"}
    except Exception as exc:
        result["tests"]["opencv"] = {"status": "error", "message": str(exc)}

    all_passed = all(t.get("status") == "success" for t in result["tests"].values())
    result["overall_status"] = "ready" if all_passed else "failed"
    return result
