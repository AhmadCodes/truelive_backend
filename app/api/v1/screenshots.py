"""
Screenshot capture API endpoints.
Allows triggering screenshot captures for cameras.
"""

from fastapi import APIRouter, HTTPException, status, BackgroundTasks
from sqlalchemy import func
from typing import List, Optional
import time
import logging

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.camera import Camera
from app.models.screenshot import Screenshot
from app.models.site import Site
from app.tasks.screenshot_tasks import update_single_screenshot, update_screenshots
from app.services.screenshot_service import process_camera

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/capture/all")
async def trigger_capture_all_cameras(
    current_user: AdminUser,
    db: DBSession
):
    """
    Trigger screenshot capture for ALL cameras in the system.

    This endpoint queues a background task to capture screenshots for all cameras.
    The task runs asynchronously via Celery.

    Only admins and super admins can trigger screenshot captures.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Summary of queued task
    """
    logger.info(f"Triggering screenshot capture for all cameras by user {current_user.username}")

    # Count total cameras
    total_cameras = db.query(func.count(Camera.id)).scalar() or 0

    if total_cameras == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No cameras found in the system"
        )

    # Queue the Celery task for background processing
    task = update_screenshots.delay()

    return {
        "success": True,
        "message": f"Screenshot capture queued for all {total_cameras} cameras",
        "total_cameras": total_cameras,
        "task_id": task.id,
        "status": "queued"
    }


@router.post("/capture/site/{site_id}")
async def trigger_capture_site_cameras(
    site_id: str,
    current_user: AdminUser,
    db: DBSession
):
    """
    Trigger screenshot capture for all cameras at a specific site.

    This endpoint captures screenshots for all cameras belonging to the specified site.
    Cameras are processed in parallel for efficiency.

    Only admins and super admins can trigger screenshot captures.

    Args:
        site_id: Site ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Summary of capture results

    Raises:
        HTTPException: If site not found or has no cameras
    """
    logger.info(f"Triggering screenshot capture for site {site_id} by user {current_user.username}")

    # Verify site exists
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site with ID '{site_id}' not found"
        )

    # Get all cameras for this site
    cameras = db.query(Camera).filter(Camera.site_id == site_id).all()

    if not cameras:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cameras found for site '{site.name}'"
        )

    # Trigger capture for each camera (force update by using current time as cutoff)
    current_time = int(time.time())
    results = {
        "checked": len(cameras),
        "created": 0,
        "updated": 0,
        "failed": 0,
        "skipped": 0
    }

    for camera in cameras:
        try:
            result = process_camera(camera, db, cutoff_time=current_time)
            status_type = result.get("status", "failed")

            if status_type in results:
                results[status_type] += 1

            logger.info(f"Camera {camera.id} screenshot: {result}")

        except Exception as e:
            logger.error(f"Error capturing screenshot for camera {camera.id}: {e}")
            results["failed"] += 1

    return {
        "success": True,
        "message": f"Screenshot capture completed for site '{site.name}'",
        "site_id": site_id,
        "site_name": site.name,
        "results": results
    }


@router.post("/capture/camera/{camera_id}")
async def trigger_capture_single_camera(
    camera_id: str,
    current_user: AdminUser,
    db: DBSession,
    async_task: bool = False
):
    """
    Trigger screenshot capture for a single camera.

    By default, captures synchronously and returns immediately.
    Use async_task=true to queue as background task for slow connections.

    Only admins and super admins can trigger screenshot captures.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated admin or super admin
        db: Database session
        async_task: If true, queue as background task instead of capturing immediately

    Returns:
        Capture result or task info

    Raises:
        HTTPException: If camera not found
    """
    logger.info(f"Triggering screenshot capture for camera {camera_id} by user {current_user.username}")

    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    if async_task:
        # Queue as background task
        task = update_single_screenshot.delay(camera_id)

        return {
            "success": True,
            "message": f"Screenshot capture queued for camera '{camera.name}'",
            "camera_id": camera_id,
            "camera_name": camera.name,
            "task_id": task.id,
            "status": "queued"
        }
    else:
        # Capture synchronously (force update by using current time as cutoff)
        try:
            current_time = int(time.time())
            result = process_camera(camera, db, cutoff_time=current_time)

            if result.get("status") == "failed":
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to capture screenshot: {result.get('reason', 'unknown error')}"
                )

            return {
                "success": True,
                "message": f"Screenshot captured for camera '{camera.name}'",
                "camera_id": camera_id,
                "camera_name": camera.name,
                "result": result
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error capturing screenshot for camera {camera_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Screenshot capture failed: {str(e)}"
            )


@router.get("/camera/{camera_id}")
async def get_camera_screenshot(
    camera_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get the latest screenshot for a camera.

    All authenticated users can view screenshots.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Screenshot metadata (not including image data)

    Raises:
        HTTPException: If camera or screenshot not found
    """
    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # Get screenshot
    screenshot = db.query(Screenshot).filter(Screenshot.camera_id == camera_id).first()

    if not screenshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No screenshot found for camera '{camera.name}'"
        )

    # Calculate age in seconds
    current_time = int(time.time())
    age_seconds = current_time - screenshot.capture_time
    age_hours = age_seconds / 3600

    return {
        "camera_id": camera_id,
        "camera_name": camera.name,
        "width": screenshot.width,
        "height": screenshot.height,
        "capture_time": screenshot.capture_time,
        "age_seconds": age_seconds,
        "age_hours": round(age_hours, 2),
        "image_size_bytes": len(screenshot.image) if screenshot.image else 0
    }


@router.get("/site/{site_id}")
async def get_site_screenshots(
    site_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get screenshot metadata for all cameras at a site.

    All authenticated users can view screenshots.

    Args:
        site_id: Site ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of screenshot metadata for all cameras at the site

    Raises:
        HTTPException: If site not found
    """
    # Verify site exists
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site with ID '{site_id}' not found"
        )

    # Get all cameras with their screenshots
    cameras = db.query(Camera).filter(Camera.site_id == site_id).all()

    current_time = int(time.time())
    screenshots_data = []

    for camera in cameras:
        screenshot = db.query(Screenshot).filter(Screenshot.camera_id == camera.id).first()

        if screenshot:
            age_seconds = current_time - screenshot.capture_time
            age_hours = age_seconds / 3600

            screenshots_data.append({
                "camera_id": camera.id,
                "camera_name": camera.name,
                "has_screenshot": True,
                "width": screenshot.width,
                "height": screenshot.height,
                "capture_time": screenshot.capture_time,
                "age_seconds": age_seconds,
                "age_hours": round(age_hours, 2),
                "image_size_bytes": len(screenshot.image) if screenshot.image else 0
            })
        else:
            screenshots_data.append({
                "camera_id": camera.id,
                "camera_name": camera.name,
                "has_screenshot": False
            })

    return {
        "site_id": site_id,
        "site_name": site.name,
        "total_cameras": len(cameras),
        "cameras_with_screenshots": sum(1 for s in screenshots_data if s.get("has_screenshot")),
        "cameras_without_screenshots": sum(1 for s in screenshots_data if not s.get("has_screenshot")),
        "screenshots": screenshots_data
    }


@router.get("/stats")
async def get_screenshot_stats(
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get overall screenshot statistics.

    All authenticated users can view screenshot statistics.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        Overall screenshot statistics
    """
    total_cameras = db.query(func.count(Camera.id)).scalar() or 0
    total_screenshots = db.query(func.count(Screenshot.camera_id)).scalar() or 0

    cameras_without_screenshots = total_cameras - total_screenshots

    # Count outdated screenshots (older than 24 hours)
    current_time = int(time.time())
    cutoff_time = current_time - (24 * 60 * 60)

    outdated_screenshots = db.query(func.count(Screenshot.camera_id)).filter(
        Screenshot.capture_time < cutoff_time
    ).scalar() or 0

    return {
        "total_cameras": total_cameras,
        "total_screenshots": total_screenshots,
        "cameras_without_screenshots": cameras_without_screenshots,
        "outdated_screenshots": outdated_screenshots,
        "up_to_date_screenshots": total_screenshots - outdated_screenshots,
        "coverage_percentage": round((total_screenshots / total_cameras * 100) if total_cameras > 0 else 0, 2)
    }
