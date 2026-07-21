"""
Camera management API endpoints.
Only admins and super admins can create, update, and delete cameras.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Annotated, List, Optional

from app.api.deps import DBSession, user_or_scope, admin_or_scope
from app.models.camera import Camera
from app.models.device import Device
from app.schemas.camera import (
    CameraCreate,
    CameraUpdate,
    CameraResponse,
    CameraDetailResponse,
    CameraSummary
)


router = APIRouter()


@router.post("", response_model=CameraDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_camera(
    camera_data: CameraCreate,
    _auth: Annotated[object, Depends(admin_or_scope("cameras:manage"))],
    db: DBSession
):
    """
    Create a new camera.

    Only admins and super admins can create cameras.

    Args:
        camera_data: Camera creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created camera details

    Raises:
        HTTPException: If camera ID already exists or device not found
    """
    # Check if camera ID already exists
    existing_camera = db.query(Camera).filter(Camera.id == camera_data.id).first()
    if existing_camera:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Camera with ID '{camera_data.id}' already exists"
        )

    # Verify device exists
    device = db.query(Device).filter(Device.id == camera_data.device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{camera_data.device_id}' not found"
        )

    # Create new camera
    new_camera = Camera(
        id=camera_data.id,
        device_id=camera_data.device_id,
        name=camera_data.name,
        rtsp_url=camera_data.rtsp_url,
        main_stream_url=camera_data.main_stream_url,
        new=camera_data.new,
        use_tcp=camera_data.use_tcp
    )

    db.add(new_camera)
    db.commit()
    db.refresh(new_camera)

    # Auto-provision an alert address for the new camera. Best-effort: if the
    # alerting tables haven't been migrated in some environment, swallow the
    # error and log it — camera creation itself should not fail because of an
    # ancillary feature.
    try:
        from app.api.v1.alert_addresses import _provision_address
        _provision_address(db, new_camera.id)
    except Exception:  # pragma: no cover
        import logging
        logging.getLogger(__name__).exception(
            "auto-provision of alert address failed", extra={"camera_id": new_camera.id},
        )

    # Return camera with device_name
    return {
        "id": new_camera.id,
        "device_id": new_camera.device_id,
        "device_name": device.name,
        "name": new_camera.name,
        "rtsp_url": new_camera.rtsp_url,
        "main_stream_url": new_camera.main_stream_url,
        "new": new_camera.new,
        "use_tcp": new_camera.use_tcp,
        "created_at": new_camera.created_at,
        "updated_at": new_camera.updated_at
    }


@router.get("", response_model=List[CameraDetailResponse])
async def list_cameras(
    _auth: Annotated[object, Depends(user_or_scope("cameras:read", "cameras:manage"))],
    db: DBSession,
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records to return"),
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    new: Optional[bool] = Query(None, description="Filter by new camera flag"),
    search: Optional[str] = Query(None, description="Search by camera name or ID")
):
    """
    List all cameras with optional filtering.

    All authenticated users can view cameras.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip (pagination)
        limit: Number of records to return (pagination)
        device_id: Filter by device ID
        new: Filter by new camera flag
        search: Search by camera name or ID

    Returns:
        List of cameras with device names
    """
    from sqlalchemy.orm import joinedload

    query = db.query(Camera).options(joinedload(Camera.device))

    # Apply filters
    if device_id:
        query = query.filter(Camera.device_id == device_id)

    if new is not None:
        query = query.filter(Camera.new == new)

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (Camera.name.ilike(search_filter)) |
            (Camera.id.ilike(search_filter))
        )

    # Order by created_at descending
    query = query.order_by(Camera.created_at.desc())

    # Apply pagination
    cameras = query.offset(skip).limit(limit).all()

    # Add device_name to each camera
    result = []
    for camera in cameras:
        camera_dict = {
            "id": camera.id,
            "device_id": camera.device_id,
            "device_name": camera.device.name if camera.device else None,
            "name": camera.name,
            "rtsp_url": camera.rtsp_url,
            "main_stream_url": camera.main_stream_url,
            "new": camera.new,
            "use_tcp": camera.use_tcp,
            "created_at": camera.created_at,
            "updated_at": camera.updated_at
        }
        result.append(camera_dict)

    return result


@router.get("/count")
async def count_cameras(
    _auth: Annotated[object, Depends(user_or_scope("cameras:read", "cameras:manage"))],
    db: DBSession,
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    new: Optional[bool] = Query(None, description="Filter by new camera flag")
):
    """
    Get total count of cameras with optional filters.

    Args:
        current_user: Current authenticated user
        db: Database session
        device_id: Filter by device ID
        new: Filter by new camera flag

    Returns:
        Total count of cameras matching filters
    """
    query = db.query(func.count(Camera.id))

    if device_id:
        query = query.filter(Camera.device_id == device_id)

    if new is not None:
        query = query.filter(Camera.new == new)

    total = query.scalar()

    return {"total": total}


@router.get("/{camera_id}", response_model=CameraDetailResponse)
async def get_camera(
    camera_id: str,
    _auth: Annotated[object, Depends(user_or_scope("cameras:read", "cameras:manage"))],
    db: DBSession
):
    """
    Get single camera by ID.

    All authenticated users can view cameras.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Camera details with device name

    Raises:
        HTTPException: If camera not found
    """
    from sqlalchemy.orm import joinedload

    camera = db.query(Camera).options(joinedload(Camera.device)).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # Add device_name to response
    return {
        "id": camera.id,
        "device_id": camera.device_id,
        "device_name": camera.device.name if camera.device else None,
        "name": camera.name,
        "rtsp_url": camera.rtsp_url,
        "main_stream_url": camera.main_stream_url,
        "new": camera.new,
        "use_tcp": camera.use_tcp,
        "created_at": camera.created_at,
        "updated_at": camera.updated_at
    }


@router.api_route("/{camera_id}", methods=["PUT", "PATCH"], response_model=CameraDetailResponse)
async def update_camera(
    camera_id: str,
    camera_data: CameraUpdate,
    _auth: Annotated[object, Depends(admin_or_scope("cameras:manage"))],
    db: DBSession
):
    """
    Update camera details.

    Only admins and super admins can update cameras.

    Args:
        camera_id: Camera ID
        camera_data: Camera update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated camera details

    Raises:
        HTTPException: If camera not found or device not found
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # If device_id is being updated, verify the new device exists
    if camera_data.device_id and camera_data.device_id != camera.device_id:
        device = db.query(Device).filter(Device.id == camera_data.device_id).first()
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device with ID '{camera_data.device_id}' not found"
            )
        camera.device_id = camera_data.device_id

    # Update fields if provided
    if camera_data.name is not None:
        camera.name = camera_data.name

    if camera_data.rtsp_url is not None:
        camera.rtsp_url = camera_data.rtsp_url

    if camera_data.main_stream_url is not None:
        camera.main_stream_url = camera_data.main_stream_url

    if camera_data.new is not None:
        camera.new = camera_data.new

    # Tri-state update for use_tcp: null explicitly clears the override (inherit from device).
    # Distinguish "field absent" (no change) from "field explicitly null" via exclude_unset.
    update_data = camera_data.model_dump(exclude_unset=True)
    if "use_tcp" in update_data:
        camera.use_tcp = update_data["use_tcp"]

    db.commit()
    db.refresh(camera)

    # Reload device relationship and return with device_name
    from sqlalchemy.orm import joinedload
    camera = db.query(Camera).options(joinedload(Camera.device)).filter(Camera.id == camera_id).first()

    return {
        "id": camera.id,
        "device_id": camera.device_id,
        "device_name": camera.device.name if camera.device else None,
        "name": camera.name,
        "rtsp_url": camera.rtsp_url,
        "main_stream_url": camera.main_stream_url,
        "new": camera.new,
        "use_tcp": camera.use_tcp,
        "created_at": camera.created_at,
        "updated_at": camera.updated_at
    }


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str,
    _auth: Annotated[object, Depends(admin_or_scope("cameras:manage"))],
    db: DBSession
):
    """
    Delete a camera.

    Only admins and super admins can delete cameras.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If camera not found
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    db.delete(camera)
    db.commit()

    return None


@router.patch("/{camera_id}/mark-as-seen")
async def mark_camera_as_seen(
    camera_id: str,
    _auth: Annotated[object, Depends(admin_or_scope("cameras:manage"))],
    db: DBSession
):
    """
    Mark a camera as no longer new (set new=False).

    Only admins and super admins can mark cameras as seen.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated camera details

    Raises:
        HTTPException: If camera not found
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    camera.new = False
    db.commit()
    db.refresh(camera)

    return {"message": f"Camera '{camera.name}' marked as seen", "camera": camera}


@router.patch("/{camera_id}/toggle-new")
async def toggle_camera_new_flag(
    camera_id: str,
    _auth: Annotated[object, Depends(admin_or_scope("cameras:manage"))],
    db: DBSession
):
    """
    Toggle the new flag for a camera.

    Only admins and super admins can toggle the new flag.

    Args:
        camera_id: Camera ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated camera details

    Raises:
        HTTPException: If camera not found
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    camera.new = not camera.new
    db.commit()
    db.refresh(camera)

    return {
        "message": f"Camera '{camera.name}' new flag set to {camera.new}",
        "camera": camera
    }


@router.get("/device/{device_id}", response_model=List[CameraDetailResponse])
async def get_cameras_by_device(
    device_id: str,
    _auth: Annotated[object, Depends(user_or_scope("cameras:read", "cameras:manage"))],
    db: DBSession
):
    """
    Get all cameras for a specific device.

    This is a convenience endpoint that's equivalent to GET /cameras?device_id={device_id}
    but follows REST conventions for nested resources.

    All authenticated users can view cameras.

    Args:
        device_id: Device ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of cameras for the device with device names

    Raises:
        HTTPException: If device not found
    """
    from sqlalchemy.orm import joinedload

    # Verify device exists
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with ID '{device_id}' not found"
        )

    # Get cameras for the device
    cameras = db.query(Camera).options(joinedload(Camera.device)).filter(Camera.device_id == device_id).order_by(Camera.created_at.desc()).all()

    # Add device_name to each camera
    result = []
    for camera in cameras:
        camera_dict = {
            "id": camera.id,
            "device_id": camera.device_id,
            "device_name": camera.device.name if camera.device else None,
            "name": camera.name,
            "rtsp_url": camera.rtsp_url,
            "main_stream_url": camera.main_stream_url,
            "new": camera.new,
            "use_tcp": camera.use_tcp,
            "created_at": camera.created_at,
            "updated_at": camera.updated_at
        }
        result.append(camera_dict)

    return result
