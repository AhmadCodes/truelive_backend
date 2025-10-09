"""
Screenshot update background tasks.
"""
import logging
from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.services.screenshot_service import batch_update_screenshots

logger = logging.getLogger(__name__)


@celery_app.task(name='app.tasks.screenshot_tasks.update_screenshots')
def update_screenshots():
    """
    Celery task to update camera screenshots.

    This task runs every 10 minutes (configured in celery_app.py).
    It checks all cameras and updates screenshots that are:
    - Missing
    - Older than 24 hours

    Returns:
        dict: Summary of operations performed
    """
    logger.info("Starting screenshot update task")
    db = SessionLocal()

    try:
        # Run screenshot update with 5-minute time limit
        result = batch_update_screenshots(db=db, max_time=300)

        logger.info(f"Screenshot update task completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Error in screenshot update task: {e}")
        raise

    finally:
        db.close()


@celery_app.task(name='app.tasks.screenshot_tasks.update_single_screenshot')
def update_single_screenshot(camera_id: str):
    """
    Update screenshot for a single camera.

    This can be called on-demand for immediate screenshot capture.

    Args:
        camera_id: Camera identifier

    Returns:
        dict: Result of operation
    """
    logger.info(f"Updating screenshot for camera {camera_id}")
    db = SessionLocal()

    try:
        from app.models.camera import Camera
        from app.services.screenshot_service import process_camera
        import time

        # Get camera
        camera = db.query(Camera).filter(Camera.id == camera_id).first()

        if not camera:
            logger.error(f"Camera {camera_id} not found")
            return {
                "status": "failed",
                "reason": "camera not found"
            }

        # Process camera (force update by using cutoff time of now)
        result = process_camera(camera, db, cutoff_time=int(time.time()))

        logger.info(f"Screenshot update for camera {camera_id}: {result}")
        return result

    except Exception as e:
        logger.error(f"Error updating screenshot for camera {camera_id}: {e}")
        raise

    finally:
        db.close()
