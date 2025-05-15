import cv2
import numpy as np
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from database import Database

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Maximum number of concurrent screenshot captures
MAX_WORKERS = 5
# Global lock for thread safety when accessing the database
db_lock = threading.Lock()

def get_camera_screenshot(rtsp_url, timeout=10):
    """
    Capture a screenshot from a camera's RTSP stream
    
    Args:
        rtsp_url (str): The RTSP URL of the camera
        timeout (int): Maximum time to wait for a frame in seconds
        
    Returns:
        numpy.ndarray: OpenCV image if successful, None otherwise
    """
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
                logger.info(f"Screenshot captured from {rtsp_url}")
                return frame
                
            # Small delay to avoid maxing out CPU
            time.sleep(0.1)
            
        logger.warning(f"Timeout while capturing screenshot from {rtsp_url}")
        return None
        
    except Exception as e:
        logger.error(f"Error capturing screenshot from {rtsp_url}: {e}")
        return None
    finally:
        # Make sure to release the capture object
        if 'cap' in locals() and cap is not None:
            cap.release()

def process_camera(camera, db, cutoff_time):
    """
    Process a single camera for screenshot capture
    
    Args:
        camera: Camera object
        db: Database instance
        cutoff_time: Unix timestamp for outdated screenshots
        
    Returns:
        dict: Result of the operation
    """
    result = {
        "camera_id": camera.id,
        "status": "skipped",
        "reason": "up to date"
    }
    
    try:
        # Use a lock when accessing the database
        with db_lock:
            # Check if screenshot exists and when it was last captured
            query = "SELECT camera_id, capture_time FROM screenshots WHERE camera_id = ?"
            db_result = db._execute_query(query, (camera.id,))
            screenshot_data = db_result.fetchone()
        
        # Decide if we need to update the screenshot
        should_update = False
        
        if screenshot_data is None:
            # No screenshot exists for this camera
            logger.info(f"No screenshot found for camera {camera.id} - will create new")
            should_update = True
            update_type = "created"
            result["reason"] = "no screenshot exists"
        elif screenshot_data[1] < cutoff_time:
            # Screenshot is older than 24 hours
            logger.info(f"Screenshot for camera {camera.id} is outdated - will update")
            should_update = True
            update_type = "updated"
            result["reason"] = "outdated"
        
        if should_update:
            # Try to get a screenshot
            frame = get_camera_screenshot(camera.rtsp_url)
            
            if frame is not None:
                # Update the database with the new screenshot
                with db_lock:
                    success = db.add_or_update_screenshot(camera.id, frame)
                
                if success:
                    result["status"] = update_type
                else:
                    result["status"] = "failed"
                    result["reason"] = "database update failed"
            else:
                logger.warning(f"Failed to capture screenshot for camera {camera.id}")
                result["status"] = "failed"
                result["reason"] = "capture failed"
                
    except Exception as e:
        logger.error(f"Error processing camera {camera.id}: {e}")
        result["status"] = "failed"
        result["reason"] = str(e)
    
    return result

def update_camera_screenshots(db=None, max_time=300):
    """
    Check for cameras without screenshots or with outdated screenshots and update them
    Using thread pool for parallel processing to ensure it runs efficiently in background
    
    Args:
        db (Database, optional): Database instance. If None, a new one will be created.
        max_time (int): Maximum time in seconds to spend on the entire update process
        
    Returns:
        dict: Summary of operations performed
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
        # Use provided database instance or create a new one
        if db is None:
            db = Database()
            
        # Calculate the cutoff time for outdated screenshots (24 hours ago)
        current_time = int(time.time())
        cutoff_time = current_time - (24 * 60 * 60)
        
        # Get all cameras that need to be checked
        all_cameras = []
        sites = db.get_sites()
        
        for site in sites:
            # Get cameras for this site
            cameras = db.get_cameras_by_site(site.id)
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
            for future in future_to_camera:
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
        logger.error(f"Error in update_camera_screenshots: {e}")
        return results 