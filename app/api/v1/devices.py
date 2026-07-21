"""
Device management API endpoints.

A Device is a single NVR/DVR recorder. Every Device belongs to exactly one
parent Site (the physical place) and may be reparented by updating ``site_id``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Optional
from app.api.deps import DBSession, user_or_scope, admin_or_scope
from app.models.device import Device
from app.models.site import Site
from app.models.category import SiteCategoryMapping
from app.schemas.device import (
    DeviceCreate,
    DeviceUpdate,
    DeviceResponse,
    DeviceDetailResponse,
    DeviceListResponse
)

router = APIRouter()


def _generate_device_id() -> str:
    """Mint a new device identifier."""
    return f"DEV_{uuid.uuid4().hex[:8].upper()}"


def _require_site(site_id: str, db) -> Site:
    """Resolve a parent site or raise a customer-friendly 404."""
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site '{site_id}' not found"
        )
    return site


def _to_detail(device: Device, include_cameras: bool = False) -> DeviceDetailResponse:
    """Build the detailed device response, including site name and categories."""
    data = DeviceDetailResponse.model_validate(device)
    data.site_name = device.site.name if device.site else None

    if include_cameras:
        data.camera_count = len(device.cameras) if device.cameras else 0

    # Categories describe the *place*: a device reports the categories of its
    # parent site.
    site_mappings = device.site.category_mappings if device.site else None
    data.categories = [
        mapping.category for mapping in site_mappings
    ] if site_mappings else []

    return data


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    db: DBSession,
    _auth = Depends(user_or_scope("devices:read", "devices:manage")),
    site_id: Optional[str] = Query(None, description="Filter by parent site ID"),
    category_id: Optional[str] = Query(None),
    include_cameras: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=1000)
):
    """
    List all devices with optional filtering and pagination.

    - **site_id**: Filter devices by parent site
    - **category_id**: Filter devices by category UUID
    - **include_cameras**: Include camera count for each device
    - **page**: Page number (default: 1)
    - **per_page**: Items per page (default: 50, max: 1000)
    """
    query = db.query(Device)

    # Apply parent site filter if provided
    if site_id:
        query = query.filter(Device.site_id == site_id)

    # Apply category filter if provided — categories hang off the parent site
    if category_id:
        query = query.join(
            SiteCategoryMapping, SiteCategoryMapping.site_id == Device.site_id
        ).filter(
            SiteCategoryMapping.category_id == category_id
        )

    # Get total count
    total = query.count()

    # Apply pagination
    offset = (page - 1) * per_page
    devices = query.offset(offset).limit(per_page).all()

    devices_response = [
        _to_detail(device, include_cameras) for device in devices
    ]

    return DeviceListResponse(
        devices=devices_response,
        total=total,
        page=page,
        per_page=per_page
    )


@router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(
    device_data: DeviceCreate,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Create a new device under an existing site.

    Requires admin or super_admin privileges.
    """
    # The parent site must exist — a device cannot be created without a place
    site = _require_site(device_data.site_id, db)

    new_device = Device(
        id=_generate_device_id(),
        name=device_data.name,
        site_id=site.id,
        nvr_username=device_data.nvr_username,
        nvr_password=device_data.nvr_password,
        new=True,
        use_tcp=device_data.use_tcp
    )

    db.add(new_device)
    db.commit()
    db.refresh(new_device)

    return new_device


@router.get("/{device_id}", response_model=DeviceDetailResponse)
async def get_device(
    device_id: str,
    db: DBSession,
    _auth = Depends(user_or_scope("devices:read", "devices:manage"))
):
    """
    Get single device with full details including cameras and its site's categories.
    """
    device = db.query(Device).filter(Device.id == device_id).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_id}' not found"
        )

    return _to_detail(device, include_cameras=True)


@router.api_route("/{device_id}", methods=["PUT", "PATCH"], response_model=DeviceResponse)
async def update_device(
    device_id: str,
    device_data: DeviceUpdate,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Update device details.

    Setting **site_id** moves the device to another site; its cameras move
    with it, and it inherits the new site's categories and camera layout.

    Requires admin or super_admin privileges.
    """
    device = db.query(Device).filter(Device.id == device_id).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_id}' not found"
        )

    # Reparenting — the target site must exist
    if device_data.site_id is not None and device_data.site_id != device.site_id:
        site = _require_site(device_data.site_id, db)
        device.site_id = site.id

    # Update fields if provided
    if device_data.name is not None:
        device.name = device_data.name
    if device_data.nvr_username is not None:
        device.nvr_username = device_data.nvr_username
    if device_data.nvr_password is not None:
        device.nvr_password = device_data.nvr_password
    if device_data.use_tcp is not None:
        device.use_tcp = device_data.use_tcp

    db.commit()
    db.refresh(device)

    return device


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Delete device and all associated data (cascades to cameras).

    Requires admin or super_admin privileges.
    """
    device = db.query(Device).filter(Device.id == device_id).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_id}' not found"
        )

    db.delete(device)
    db.commit()

    return None
