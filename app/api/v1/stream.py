"""
WebSocket streaming endpoints for RTSP camera feeds.
"""
import subprocess
import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query, Depends
from sqlalchemy.orm import Session
from typing import Optional

from app.api.deps import get_db
from app.models.camera import Camera
from app.core.security import decode_token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/info", tags=["Streaming"])
async def get_streaming_info():
    """
    Get information about available WebSocket streaming endpoints.

    WebSocket endpoints are not displayed in Swagger UI because OpenAPI 3.0
    doesn't support WebSocket documentation. Use this endpoint to discover
    available streaming options.

    Returns:
        Information about MPEG1 and JPEG streaming endpoints with usage examples
    """
    return {
        "message": "WebSocket streaming endpoints available",
        "note": "WebSocket endpoints don't appear in Swagger UI (OpenAPI limitation)",
        "endpoints": [
            {
                "name": "MPEG1 Stream (FFmpeg-based)",
                "method": "WebSocket",
                "url": "ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}?token={jwt_token}",
                "description": "Stream RTSP camera as MPEG1 for JSMpeg player",
                "features": {
                    "resolution": "640x480",
                    "fps": 25,
                    "bitrate": "1000k",
                    "format": "MPEG1",
                    "latency": "low"
                },
                "requirements": [
                    "FFmpeg installed on server",
                    "JSMpeg player on client",
                    "Valid JWT token"
                ]
            },
            {
                "name": "JPEG Stream (OpenCV-based)",
                "method": "WebSocket",
                "url": "ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}/jpeg?token={jwt_token}",
                "description": "Stream camera as individual JPEG frames",
                "features": {
                    "resolution": "640x480",
                    "fps": 30,
                    "format": "JPEG",
                    "quality": "80%"
                },
                "requirements": [
                    "OpenCV (opencv-python) installed",
                    "Custom frame renderer on client",
                    "Valid JWT token"
                ]
            }
        ],
        "authentication": {
            "method": "JWT token as query parameter",
            "example": "?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            "how_to_get_token": "POST /api/v1/auth/login"
        },
        "example_usage": {
            "javascript": """
const token = "your_jwt_token_here";
const cameraId = "12345";
const ws = new WebSocket(`ws://localhost:8000/api/v1/stream/ws/camera/${cameraId}?token=${token}`);

// For MPEG1 with JSMpeg
const player = new JSMpeg.Player(wsUrl, {
    canvas: document.getElementById('canvas'),
    autoplay: true,
    audio: false
});
            """,
            "python": """
import websockets
import asyncio

async def stream_camera():
    token = "your_jwt_token_here"
    camera_id = "12345"
    uri = f"ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}?token={token}"

    async with websockets.connect(uri) as websocket:
        while True:
            data = await websocket.recv()
            # Process MPEG1 data
            print(f"Received {len(data)} bytes")
            """
        },
        "documentation": "See STREAMING.md for complete implementation guide",
        "troubleshooting": {
            "ffmpeg_not_found": "Rebuild Docker containers: docker-compose build --no-cache",
            "connection_rejected": "Check JWT token validity and camera_id existence",
            "high_cpu": "Limit concurrent streams or reduce bitrate/resolution"
        }
    }


@router.websocket("/ws/camera/{camera_id}")
async def stream_camera(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication")
):
    """
    WebSocket endpoint to stream camera feed as MPEG1 for JSMpeg player.

    Client connects to: ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}?token={jwt_token}

    The stream is converted from RTSP to MPEG1 format using FFmpeg for browser playback.

    Args:
        websocket: WebSocket connection
        camera_id: Camera ID to stream
        token: JWT access token for authentication

    Security:
        Requires valid JWT token passed as query parameter
    """
    # Accept WebSocket connection with CORS headers
    # Note: WebSocket connections don't use traditional CORS, but we accept all origins here
    # Authentication is handled via JWT token instead
    await websocket.accept()

    try:
        # Authenticate user
        if not token:
            await websocket.close(code=1008, reason="Authentication required: Missing token")
            return

        # Verify JWT token
        try:
            payload = decode_token(token)
            if not payload:
                await websocket.close(code=1008, reason="Invalid or expired token")
                return
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            await websocket.close(code=1008, reason="Invalid or expired token")
            return

        # Get database session
        db = next(get_db())

        try:
            # Get camera from database
            camera = db.query(Camera).filter(Camera.id == camera_id).first()
            if not camera:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' not found")
                return

            rtsp_url = camera.rtsp_url
            if not rtsp_url:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' has no RTSP URL")
                return

            logger.info(f"Starting stream for camera {camera_id} (requested by user)")

            # FFmpeg command to convert RTSP to MPEG1
            command = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',    # Use TCP instead of UDP for more reliability
                '-i', rtsp_url,
                '-f', 'mpegts',
                '-codec:v', 'mpeg1video',
                '-s', '640x480',      # Resolution
                '-b:v', '1000k',      # Bitrate
                '-bf', '0',           # No B-frames
                '-muxdelay', '0.001', # Low latency
                '-r', '25',           # Frame rate (25 FPS)
                '-an',                # No audio
                'pipe:1'              # Output to stdout
            ]

            process = None
            try:
                # Start FFmpeg process
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=10**8
                )

                logger.info(f"FFmpeg process started for camera {camera_id}")

                # Stream data to WebSocket
                chunk_count = 0
                while True:
                    try:
                        # Read chunks from FFmpeg
                        chunk = process.stdout.read(1024)
                        if not chunk:
                            # Check if process has exited with error
                            exit_code = process.poll()
                            if exit_code is not None and exit_code != 0:
                                stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                                logger.error(f"FFmpeg failed for camera {camera_id} with exit code {exit_code}: {stderr_output[:500]}")
                            else:
                                logger.warning(f"FFmpeg stream ended for camera {camera_id}")
                            break

                        # Send to WebSocket
                        await websocket.send_bytes(chunk)
                        chunk_count += 1

                        # Small delay to prevent overwhelming the client
                        await asyncio.sleep(0.01)

                    except WebSocketDisconnect:
                        logger.info(f"Client disconnected from camera {camera_id} stream")
                        break
                    except Exception as e:
                        logger.error(f"Streaming error for camera {camera_id}: {e}")
                        break

                logger.info(f"Stream ended for camera {camera_id} after {chunk_count} chunks")

            finally:
                # Cleanup FFmpeg process
                if process:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception as e:
                        logger.error(f"Error killing FFmpeg process: {e}")

        finally:
            db.close()

    except Exception as e:
        logger.error(f"WebSocket stream error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass

    finally:
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/ws/camera/{camera_id}/jpeg")
async def stream_camera_jpeg(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication")
):
    """
    Alternative WebSocket endpoint using OpenCV to stream JPEG frames.

    This is a simpler implementation that doesn't require FFmpeg.
    Useful for environments where FFmpeg is not available.

    Client connects to: ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}/jpeg?token={jwt_token}

    Args:
        websocket: WebSocket connection
        camera_id: Camera ID to stream
        token: JWT access token for authentication

    Security:
        Requires valid JWT token passed as query parameter

    Note:
        Requires opencv-python package installed
    """
    await websocket.accept()

    try:
        # Authenticate user
        if not token:
            await websocket.close(code=1008, reason="Authentication required: Missing token")
            return

        # Verify JWT token
        try:
            payload = decode_token(token)
            if not payload:
                await websocket.close(code=1008, reason="Invalid or expired token")
                return
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            await websocket.close(code=1008, reason="Invalid or expired token")
            return

        # Get database session
        db = next(get_db())

        try:
            # Get camera from database
            camera = db.query(Camera).filter(Camera.id == camera_id).first()
            if not camera:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' not found")
                return

            rtsp_url = camera.rtsp_url
            if not rtsp_url:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' has no RTSP URL")
                return

            logger.info(f"Starting JPEG stream for camera {camera_id}")

            try:
                import cv2
            except ImportError:
                await websocket.close(code=1011, reason="OpenCV not installed on server")
                logger.error("opencv-python not installed. Install with: pip install opencv-python")
                return

            cap = cv2.VideoCapture(rtsp_url)

            if not cap.isOpened():
                await websocket.close(code=1011, reason="Failed to open RTSP stream")
                logger.error(f"Failed to open RTSP stream for camera {camera_id}: {rtsp_url}")
                return

            try:
                frame_count = 0
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        logger.warning(f"Failed to read frame from camera {camera_id}")
                        break

                    # Resize frame for better performance
                    frame = cv2.resize(frame, (640, 480))

                    # Encode frame as JPEG
                    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

                    # Send to WebSocket
                    await websocket.send_bytes(buffer.tobytes())
                    frame_count += 1

                    # ~30 FPS
                    await asyncio.sleep(0.033)

                logger.info(f"JPEG stream ended for camera {camera_id} after {frame_count} frames")

            except WebSocketDisconnect:
                logger.info(f"Client disconnected from camera {camera_id} JPEG stream")
            except Exception as e:
                logger.error(f"JPEG streaming error for camera {camera_id}: {e}")
            finally:
                cap.release()

        finally:
            db.close()

    except Exception as e:
        logger.error(f"WebSocket JPEG stream error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass

    finally:
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/ws/camera/{camera_id}/h264")
async def stream_camera_h264(
    websocket: WebSocket,
    camera_id: str,
    token: Optional[str] = Query(None, description="JWT access token for authentication")
):
    """
    LOW-LATENCY WebSocket endpoint streaming H.264 video for Media Source Extensions (MSE).

    Client connects to: ws://localhost:8000/api/v1/stream/ws/camera/{camera_id}/h264?token={jwt_token}

    Uses fragmented MP4 (fMP4) format with H.264 codec for native browser playback
    with minimal latency (~300-500ms).

    Features:
        - Ultra-low latency (~300-500ms vs 2-3 seconds)
        - H.264 compression (better than JPEG)
        - Works with browser Media Source Extensions
        - No external libraries needed on client
    """
    await websocket.accept()

    try:
        # Authenticate
        if not token:
            await websocket.close(code=1008, reason="Authentication required: Missing token")
            return

        try:
            payload = decode_token(token)
            if not payload:
                await websocket.close(code=1008, reason="Invalid or expired token")
                return
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            await websocket.close(code=1008, reason="Invalid or expired token")
            return

        db = next(get_db())

        try:
            camera = db.query(Camera).filter(Camera.id == camera_id).first()
            if not camera:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' not found")
                return

            rtsp_url = camera.rtsp_url
            if not rtsp_url:
                await websocket.close(code=1008, reason=f"Camera '{camera_id}' has no RTSP URL")
                return

            logger.info(f"Starting LOW-LATENCY H.264 stream for camera {camera_id}")

            # FFmpeg command for ultra-low-latency H.264 with reduced bitrate (10fps)
            command = [
                'ffmpeg',
                '-rtsp_transport', 'tcp',
                '-fflags', 'nobuffer',
                '-flags', 'low_delay',
                '-i', rtsp_url,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-tune', 'zerolatency',
                '-g', '20',              # Keyframe every 2 seconds (10fps * 2s)
                '-keyint_min', '10',     # Min keyframe interval
                '-sc_threshold', '0',
                '-s', '1280x720',
                '-b:v', '800k',          # Reduced bitrate for 10fps
                '-maxrate', '900k',      # Slightly higher max for peaks
                '-bufsize', '1800k',     # 2x maxrate
                '-r', '10',              # 10 frames per second
                '-pix_fmt', 'yuv420p',
                '-profile:v', 'baseline',
                '-level', '3.1',
                '-f', 'mp4',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-an',
                'pipe:1'
            ]

            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            logger.info(f"FFmpeg H.264 process started for camera {camera_id}")

            chunk_count = 0
            total_bytes = 0

            while True:
                try:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        exit_code = process.poll()
                        if exit_code is not None and exit_code != 0:
                            stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                            logger.error(f"FFmpeg H.264 failed for camera {camera_id} (exit {exit_code}): {stderr_output[:1000]}")
                        break

                    await websocket.send_bytes(chunk)
                    chunk_count += 1
                    total_bytes += len(chunk)

                except WebSocketDisconnect:
                    logger.info(f"Client disconnected from H.264 stream for camera {camera_id}")
                    break
                except Exception as e:
                    logger.error(f"H.264 streaming error for camera {camera_id}: {e}")
                    break

            logger.info(f"H.264 stream ended for camera {camera_id} - {chunk_count} chunks, {total_bytes/1024/1024:.2f} MB")

            if process:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as e:
                    logger.error(f"Error killing FFmpeg H.264 process: {e}")

        finally:
            db.close()

    except Exception as e:
        logger.error(f"WebSocket H.264 stream error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass

    finally:
        try:
            await websocket.close()
        except:
            pass


@router.get("/test/{camera_id}", tags=["Streaming"])
async def test_camera_stream(
    camera_id: str,
    db: Session = Depends(get_db)
):
    """
    Test camera RTSP connectivity and return diagnostic information.
    
    Returns:
        Detailed diagnostics about camera reachability, RTSP URL validity,
        and FFmpeg/OpenCV compatibility.
    """
    try:
        # Get camera
        camera = db.query(Camera).filter(Camera.id == camera_id).first()
        if not camera:
            return {"status": "error", "message": f"Camera {camera_id} not found"}
        
        rtsp_url = camera.rtsp_url
        if not rtsp_url:
            return {"status": "error", "message": "Camera has no RTSP URL"}
        
        # Test with ffprobe
        import subprocess
        result = {
            "camera_id": camera_id,
            "camera_name": camera.name,
            "rtsp_url": rtsp_url,
            "tests": {}
        }
        
        # Test 1: FFprobe
        try:
            proc = subprocess.run(
                ['ffprobe', '-rtsp_transport', 'tcp', rtsp_url],
                capture_output=True,
                text=True,
                timeout=10
            )
            if proc.returncode == 0:
                result["tests"]["ffmpeg"] = {"status": "success", "message": "FFmpeg can access stream"}
            else:
                error_lines = proc.stderr.split('\n')[-5:]
                result["tests"]["ffmpeg"] = {
                    "status": "failed",
                    "exit_code": proc.returncode,
                    "error": '\n'.join(error_lines)
                }
        except subprocess.TimeoutExpired:
            result["tests"]["ffmpeg"] = {"status": "timeout", "message": "FFmpeg connection timed out"}
        except Exception as e:
            result["tests"]["ffmpeg"] = {"status": "error", "message": str(e)}
        
        # Test 2: OpenCV
        try:
            import cv2
            cap = cv2.VideoCapture(rtsp_url)
            if cap.isOpened():
                ret, frame = cap.read()
                cap.release()
                if ret:
                    result["tests"]["opencv"] = {"status": "success", "message": "OpenCV can read frames"}
                else:
                    result["tests"]["opencv"] = {"status": "failed", "message": "OpenCV opened but cannot read frames"}
            else:
                result["tests"]["opencv"] = {"status": "failed", "message": "OpenCV cannot open stream"}
        except Exception as e:
            result["tests"]["opencv"] = {"status": "error", "message": str(e)}
        
        # Overall status
        all_passed = all(t.get("status") == "success" for t in result["tests"].values())
        result["overall_status"] = "ready" if all_passed else "failed"
        
        return result
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        db.close()
