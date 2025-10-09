"""
Snapshot capture API endpoints.
Allows triggering snapshot captures for cameras.
"""

from fastapi import APIRouter, HTTPException, status, BackgroundTasks
from sqlalchemy import func
from typing import List, Optional
import time
import logging

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.camera import Camera
from app.models.snapshot import Snapshot
from app.models.site import Site
from app.tasks.snapshot_tasks import update_single_snapshot, update_snapshots
from app.services.snapshot_service import process_camera

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/capture/all")
async def trigger_capture_all_cameras(
    current_user: AdminUser,
    db: DBSession
):
    """
    Trigger snapshot capture for ALL cameras in the system.

    This endpoint queues a background task to capture snapshots for all cameras.
    The task runs asynchronously via Celery.

    Only admins and super admins can trigger snapshot captures.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Summary of queued task
    """
    logger.info(f"Triggering snapshot capture for all cameras by user {current_user.username}")

    # Count total cameras
    total_cameras = db.query(func.count(Camera.id)).scalar() or 0

    if total_cameras == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No cameras found in the system"
        )

    # Queue the Celery task for background processing
    task = update_snapshots.delay()

    return {
        "success": True,
        "message": f"Snapshot capture queued for all {total_cameras} cameras",
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
    Trigger snapshot capture for all cameras at a specific site.

    This endpoint captures snapshots for all cameras belonging to the specified site.
    Cameras are processed in parallel for efficiency.

    Only admins and super admins can trigger snapshot captures.

    Args:
        site_id: Site ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Summary of capture results

    Raises:
        HTTPException: If site not found or has no cameras
    """
    logger.info(f"Triggering snapshot capture for site {site_id} by user {current_user.username}")

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

            logger.info(f"Camera {camera.id} snapshot: {result}")

        except Exception as e:
            logger.error(f"Error capturing snapshot for camera {camera.id}: {e}")
            results["failed"] += 1

    return {
        "success": True,
        "message": f"Snapshot capture completed for site '{site.name}'",
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
    Trigger snapshot capture for a single camera.

    By default, captures synchronously and returns immediately.
    Use async_task=true to queue as background task for slow connections.

    Only admins and super admins can trigger snapshot captures.

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
    logger.info(f"Triggering snapshot capture for camera {camera_id} by user {current_user.username}")

    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    if async_task:
        # Queue as background task
        task = update_single_snapshot.delay(camera_id)

        return {
            "success": True,
            "message": f"Snapshot capture queued for camera '{camera.name}'",
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
                    detail=f"Failed to capture snapshot: {result.get('reason', 'unknown error')}"
                )

            return {
                "success": True,
                "message": f"Snapshot captured for camera '{camera.name}'",
                "camera_id": camera_id,
                "camera_name": camera.name,
                "result": result
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error capturing snapshot for camera {camera_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Snapshot capture failed: {str(e)}"
            )


@router.get("/camera/{camera_id}")
async def get_camera_snapshot(
    camera_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get the latest snapshot for a camera.

    All authenticated users can view snapshots.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Snapshot metadata (not including image data)

    Raises:
        HTTPException: If camera or snapshot not found
    """
    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # Get snapshot
    snapshot = db.query(Snapshot).filter(Snapshot.camera_id == camera_id).first()

    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No snapshot found for camera '{camera.name}'"
        )

    # Calculate age in seconds
    current_time = int(time.time())
    age_seconds = current_time - snapshot.capture_time
    age_hours = age_seconds / 3600

    return {
        "camera_id": camera_id,
        "camera_name": camera.name,
        "width": snapshot.width,
        "height": snapshot.height,
        "capture_time": snapshot.capture_time,
        "age_seconds": age_seconds,
        "age_hours": round(age_hours, 2),
        "image_size_bytes": len(snapshot.image) if snapshot.image else 0
    }


@router.get("/site/{site_id}")
async def get_site_snapshots(
    site_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get snapshot metadata for all cameras at a site.

    All authenticated users can view snapshots.

    Args:
        site_id: Site ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of snapshot metadata for all cameras at the site

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

    # Get all cameras with their snapshots
    cameras = db.query(Camera).filter(Camera.site_id == site_id).all()

    current_time = int(time.time())
    snapshots_data = []

    for camera in cameras:
        snapshot = db.query(Snapshot).filter(Snapshot.camera_id == camera.id).first()

        if snapshot:
            age_seconds = current_time - snapshot.capture_time
            age_hours = age_seconds / 3600

            snapshots_data.append({
                "camera_id": camera.id,
                "camera_name": camera.name,
                "has_snapshot": True,
                "width": snapshot.width,
                "height": snapshot.height,
                "capture_time": snapshot.capture_time,
                "age_seconds": age_seconds,
                "age_hours": round(age_hours, 2),
                "image_size_bytes": len(snapshot.image) if snapshot.image else 0
            })
        else:
            snapshots_data.append({
                "camera_id": camera.id,
                "camera_name": camera.name,
                "has_snapshot": False
            })

    return {
        "site_id": site_id,
        "site_name": site.name,
        "total_cameras": len(cameras),
        "cameras_with_snapshots": sum(1 for s in snapshots_data if s.get("has_snapshot")),
        "cameras_without_snapshots": sum(1 for s in snapshots_data if not s.get("has_snapshot")),
        "snapshots": snapshots_data
    }


@router.get("/stats")
async def get_snapshot_stats(
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get overall snapshot statistics.

    All authenticated users can view snapshot statistics.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        Overall snapshot statistics
    """
    total_cameras = db.query(func.count(Camera.id)).scalar() or 0
    total_snapshots = db.query(func.count(Snapshot.camera_id)).scalar() or 0

    cameras_without_snapshots = total_cameras - total_snapshots

    # Count outdated snapshots (older than 24 hours)
    current_time = int(time.time())
    cutoff_time = current_time - (24 * 60 * 60)

    outdated_snapshots = db.query(func.count(Snapshot.camera_id)).filter(
        Snapshot.capture_time < cutoff_time
    ).scalar() or 0

    return {
        "total_cameras": total_cameras,
        "total_snapshots": total_snapshots,
        "cameras_without_snapshots": cameras_without_snapshots,
        "outdated_snapshots": outdated_snapshots,
        "up_to_date_snapshots": total_snapshots - outdated_snapshots,
        "coverage_percentage": round((total_snapshots / total_cameras * 100) if total_cameras > 0 else 0, 2)
    }
