"""
Camera management API endpoints.
Only admins and super admins can create, update, and delete cameras.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.camera import Camera
from app.models.site import Site
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
    current_user: AdminUser,
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
        HTTPException: If camera ID already exists or site not found
    """
    # Check if camera ID already exists
    existing_camera = db.query(Camera).filter(Camera.id == camera_data.id).first()
    if existing_camera:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Camera with ID '{camera_data.id}' already exists"
        )

    # Verify site exists
    site = db.query(Site).filter(Site.id == camera_data.site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site with ID '{camera_data.site_id}' not found"
        )

    # Create new camera
    new_camera = Camera(
        id=camera_data.id,
        site_id=camera_data.site_id,
        name=camera_data.name,
        rtsp_url=camera_data.rtsp_url,
        main_stream_url=camera_data.main_stream_url,
        sureview_camera=camera_data.sureview_camera,
        new=camera_data.new
    )

    db.add(new_camera)
    db.commit()
    db.refresh(new_camera)

    # Return camera with site_name
    return {
        "id": new_camera.id,
        "site_id": new_camera.site_id,
        "site_name": site.name,
        "name": new_camera.name,
        "rtsp_url": new_camera.rtsp_url,
        "main_stream_url": new_camera.main_stream_url,
        "sureview_camera": new_camera.sureview_camera,
        "new": new_camera.new,
        "created_at": new_camera.created_at,
        "updated_at": new_camera.updated_at
    }


@router.get("", response_model=List[CameraDetailResponse])
async def list_cameras(
    current_user: CurrentUser,
    db: DBSession,
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=500, description="Number of records to return"),
    site_id: Optional[str] = Query(None, description="Filter by site ID"),
    sureview_camera: Optional[bool] = Query(None, description="Filter by SureView camera flag"),
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
        site_id: Filter by site ID
        sureview_camera: Filter by SureView camera flag
        new: Filter by new camera flag
        search: Search by camera name or ID

    Returns:
        List of cameras with site names
    """
    from sqlalchemy.orm import joinedload

    query = db.query(Camera).options(joinedload(Camera.site))

    # Apply filters
    if site_id:
        query = query.filter(Camera.site_id == site_id)

    if sureview_camera is not None:
        query = query.filter(Camera.sureview_camera == sureview_camera)

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

    # Add site_name to each camera
    result = []
    for camera in cameras:
        camera_dict = {
            "id": camera.id,
            "site_id": camera.site_id,
            "site_name": camera.site.name if camera.site else None,
            "name": camera.name,
            "rtsp_url": camera.rtsp_url,
            "main_stream_url": camera.main_stream_url,
            "sureview_camera": camera.sureview_camera,
            "new": camera.new,
            "created_at": camera.created_at,
            "updated_at": camera.updated_at
        }
        result.append(camera_dict)

    return result


@router.get("/count")
async def count_cameras(
    current_user: CurrentUser,
    db: DBSession,
    site_id: Optional[str] = Query(None, description="Filter by site ID"),
    sureview_camera: Optional[bool] = Query(None, description="Filter by SureView camera flag"),
    new: Optional[bool] = Query(None, description="Filter by new camera flag")
):
    """
    Get total count of cameras with optional filters.

    Args:
        current_user: Current authenticated user
        db: Database session
        site_id: Filter by site ID
        sureview_camera: Filter by SureView camera flag
        new: Filter by new camera flag

    Returns:
        Total count of cameras matching filters
    """
    query = db.query(func.count(Camera.id))

    if site_id:
        query = query.filter(Camera.site_id == site_id)

    if sureview_camera is not None:
        query = query.filter(Camera.sureview_camera == sureview_camera)

    if new is not None:
        query = query.filter(Camera.new == new)

    total = query.scalar()

    return {"total": total}


@router.get("/{camera_id}", response_model=CameraDetailResponse)
async def get_camera(
    camera_id: str,
    current_user: CurrentUser,
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
        Camera details with site name

    Raises:
        HTTPException: If camera not found
    """
    from sqlalchemy.orm import joinedload

    camera = db.query(Camera).options(joinedload(Camera.site)).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # Add site_name to response
    return {
        "id": camera.id,
        "site_id": camera.site_id,
        "site_name": camera.site.name if camera.site else None,
        "name": camera.name,
        "rtsp_url": camera.rtsp_url,
        "main_stream_url": camera.main_stream_url,
        "sureview_camera": camera.sureview_camera,
        "new": camera.new,
        "created_at": camera.created_at,
        "updated_at": camera.updated_at
    }


@router.put("/{camera_id}", response_model=CameraDetailResponse)
async def update_camera(
    camera_id: str,
    camera_data: CameraUpdate,
    current_user: AdminUser,
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
        HTTPException: If camera not found or site not found
    """
    camera = db.query(Camera).filter(Camera.id == camera_id).first()

    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # If site_id is being updated, verify the new site exists
    if camera_data.site_id and camera_data.site_id != camera.site_id:
        site = db.query(Site).filter(Site.id == camera_data.site_id).first()
        if not site:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Site with ID '{camera_data.site_id}' not found"
            )
        camera.site_id = camera_data.site_id

    # Update fields if provided
    if camera_data.name is not None:
        camera.name = camera_data.name

    if camera_data.rtsp_url is not None:
        camera.rtsp_url = camera_data.rtsp_url

    if camera_data.main_stream_url is not None:
        camera.main_stream_url = camera_data.main_stream_url

    if camera_data.sureview_camera is not None:
        camera.sureview_camera = camera_data.sureview_camera

    if camera_data.new is not None:
        camera.new = camera_data.new

    db.commit()
    db.refresh(camera)

    # Reload site relationship and return with site_name
    from sqlalchemy.orm import joinedload
    camera = db.query(Camera).options(joinedload(Camera.site)).filter(Camera.id == camera_id).first()

    return {
        "id": camera.id,
        "site_id": camera.site_id,
        "site_name": camera.site.name if camera.site else None,
        "name": camera.name,
        "rtsp_url": camera.rtsp_url,
        "main_stream_url": camera.main_stream_url,
        "sureview_camera": camera.sureview_camera,
        "new": camera.new,
        "created_at": camera.created_at,
        "updated_at": camera.updated_at
    }


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(
    camera_id: str,
    current_user: AdminUser,
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
    current_user: AdminUser,
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
    current_user: AdminUser,
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


@router.get("/site/{site_id}", response_model=List[CameraDetailResponse])
async def get_cameras_by_site(
    site_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get all cameras for a specific site.

    This is a convenience endpoint that's equivalent to GET /cameras?site_id={site_id}
    but follows REST conventions for nested resources.

    All authenticated users can view cameras.

    Args:
        site_id: Site ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of cameras for the site with site names

    Raises:
        HTTPException: If site not found
    """
    from sqlalchemy.orm import joinedload

    # Verify site exists
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site with ID '{site_id}' not found"
        )

    # Get cameras for the site
    cameras = db.query(Camera).options(joinedload(Camera.site)).filter(Camera.site_id == site_id).order_by(Camera.created_at.desc()).all()

    # Add site_name to each camera
    result = []
    for camera in cameras:
        camera_dict = {
            "id": camera.id,
            "site_id": camera.site_id,
            "site_name": camera.site.name if camera.site else None,
            "name": camera.name,
            "rtsp_url": camera.rtsp_url,
            "main_stream_url": camera.main_stream_url,
            "sureview_camera": camera.sureview_camera,
            "new": camera.new,
            "created_at": camera.created_at,
            "updated_at": camera.updated_at
        }
        result.append(camera_dict)

    return result
