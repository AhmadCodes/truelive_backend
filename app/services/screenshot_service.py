"""
Screenshot capture service.

Captures screenshots from RTSP camera streams and stores them in the database.
"""
import cv2
import numpy as np
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, List
from sqlalchemy.orm import Session
import threading

from app.models.camera import Camera
from app.models.screenshot import Screenshot
from app.models.site import Site

logger = logging.getLogger(__name__)

# Maximum number of concurrent screenshot captures
MAX_WORKERS = 5

# Global lock for thread safety when accessing the database
db_lock = threading.Lock()


def get_camera_screenshot(rtsp_url: str, timeout: int = 10) -> Optional[np.ndarray]:
    """
    Capture a screenshot from a camera's RTSP stream.

    Args:
        rtsp_url: The RTSP URL of the camera
        timeout: Maximum time to wait for a frame in seconds

    Returns:
        OpenCV image (numpy array) if successful, None otherwise
    """
    cap = None

    try:
        if not rtsp_url:
            logger.warning("Empty RTSP URL provided to get_camera_screenshot")
            return None

        # Set OpenCV parameters to optimize for network streaming
        # Use FFMPEG backend as it handles network streams better
        cap = cv2.VideoCapture(rtsp_url)

        # Set buffer size to minimum to get the most recent frame
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Set timeout to avoid hanging indefinitely
        start_time = time.time()

        while time.time() - start_time < timeout:
            ret, frame = cap.read()

            if ret and frame is not None and frame.size > 0:
                # Successfully captured a frame
                logger.info(f"Screenshot captured from {rtsp_url[:50]}...")
                return frame

            # Small delay to avoid maxing out CPU
            time.sleep(0.1)

        logger.warning(f"Timeout while capturing screenshot from {rtsp_url[:50]}...")
        return None

    except Exception as e:
        logger.error(f"Error capturing screenshot from {rtsp_url[:50]}...: {e}")
        return None

    finally:
        # Make sure to release the capture object
        if cap is not None:
            cap.release()


def process_camera(
    camera: Camera,
    db: Session,
    cutoff_time: int
) -> Dict[str, any]:
    """
    Process a single camera for screenshot capture.

    Args:
        camera: Camera object
        db: Database session
        cutoff_time: Unix timestamp for outdated screenshots

    Returns:
        Result dict with status and reason
    """
    result = {
        "camera_id": camera.id,
        "status": "skipped",
        "reason": "up to date"
    }

    try:
        # Check if screenshot exists and when it was last captured
        screenshot = db.query(Screenshot).filter(
            Screenshot.camera_id == camera.id
        ).first()

        # Decide if we need to update the screenshot
        should_update = False
        update_type = "skipped"

        if screenshot is None:
            # No screenshot exists for this camera
            logger.info(f"No screenshot found for camera {camera.id} - will create new")
            should_update = True
            update_type = "created"
            result["reason"] = "no screenshot exists"

        elif screenshot.capture_time < cutoff_time:
            # Screenshot is older than 24 hours
            logger.info(f"Screenshot for camera {camera.id} is outdated - will update")
            should_update = True
            update_type = "updated"
            result["reason"] = "outdated"

        if should_update:
            # Try to get a screenshot
            frame = get_camera_screenshot(camera.rtsp_url)

            if frame is not None:
                # Convert frame to bytes for database storage
                height, width = frame.shape[:2]
                current_time = int(time.time())

                # Encode image as PNG bytes
                success, buffer = cv2.imencode('.png', frame)

                if not success:
                    result["status"] = "failed"
                    result["reason"] = "image encoding failed"
                    return result

                image_bytes = buffer.tobytes()

                # Update or create screenshot in database
                if screenshot is None:
                    # Create new
                    new_screenshot = Screenshot(
                        camera_id=camera.id,
                        image=image_bytes,
                        height=height,
                        width=width,
                        capture_time=current_time
                    )
                    db.add(new_screenshot)
                else:
                    # Update existing
                    screenshot.image = image_bytes
                    screenshot.height = height
                    screenshot.width = width
                    screenshot.capture_time = current_time

                db.commit()
                result["status"] = update_type
                logger.info(f"Screenshot {update_type} for camera {camera.id}")

            else:
                logger.warning(f"Failed to capture screenshot for camera {camera.id}")
                result["status"] = "failed"
                result["reason"] = "capture failed"

    except Exception as e:
        logger.error(f"Error processing camera {camera.id}: {e}")
        result["status"] = "failed"
        result["reason"] = str(e)
        db.rollback()

    return result


async def batch_update_screenshots(
    db: Session,
    max_time: int = 300
) -> Dict[str, int]:
    """
    Check for cameras without screenshots or with outdated screenshots and update them.

    Uses thread pool for parallel processing to ensure it runs efficiently in background.

    Args:
        db: Database session
        max_time: Maximum time in seconds to spend on the entire update process

    Returns:
        Summary dict with counts:
            {
                "checked": int,
                "updated": int,
                "created": int,
                "failed": int,
                "skipped": int
            }
    """
    start_time = time.time()
    results = {
        "checked": 0,
        "updated": 0,
        "created": 0,
        "failed": 0,
        "skipped": 0
    }

    try:
        # Calculate the cutoff time for outdated screenshots (24 hours ago)
        current_time = int(time.time())
        cutoff_time = current_time - (24 * 60 * 60)

        # Get all cameras that need to be checked
        all_cameras = []
        sites = db.query(Site).all()

        for site in sites:
            # Get cameras for this site
            cameras = db.query(Camera).filter(Camera.site_id == site.id).all()
            all_cameras.extend(cameras)

        results["checked"] = len(all_cameras)
        logger.info(f"Found {len(all_cameras)} cameras to check for screenshots")

        # Process cameras in parallel using a thread pool
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all camera processing tasks
            future_to_camera = {
                executor.submit(process_camera, camera, db, cutoff_time): camera
                for camera in all_cameras
            }

            # Process results as they complete
            for future in as_completed(future_to_camera):
                # Check if we've exceeded the maximum time
                if time.time() - start_time > max_time:
                    logger.warning(f"Screenshot update reached time limit of {max_time} seconds")
                    break

                try:
                    result = future.result()
                    status = result.get("status")

                    # Update the appropriate counter
                    if status in results:
                        results[status] += 1

                except Exception as e:
                    logger.error(f"Error processing camera task: {e}")
                    results["failed"] += 1

        logger.info(f"Screenshot update completed: {results}")
        return results

    except Exception as e:
        logger.error(f"Error in batch_update_screenshots: {e}")
        return results


def capture_screenshot(rtsp_url: str) -> bytes:
    """
    Capture a single screenshot from an RTSP stream.

    This is a simpler function for on-demand screenshot capture.

    Args:
        rtsp_url: RTSP stream URL

    Returns:
        Screenshot as JPEG bytes

    Raises:
        ValueError: If screenshot capture fails
    """
    frame = get_camera_screenshot(rtsp_url, timeout=10)

    if frame is None:
        raise ValueError(f"Failed to capture screenshot from {rtsp_url}")

    # Encode as JPEG
    success, buffer = cv2.imencode('.jpg', frame)

    if not success:
        raise ValueError("Failed to encode screenshot as JPEG")

    return buffer.tobytes()
