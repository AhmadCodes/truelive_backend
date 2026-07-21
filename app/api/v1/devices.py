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
    DeviceListResponse,
    CategoryAssignment
)
from app.schemas.site_camera_layout import (
    AutoPopulateResponse,
    BulkAutoPopulateResponse,
    SiteCameraLayoutConfigResponse,
    SaveLayoutRequest,
    SaveLayoutResponse
)
from app.services.site_camera_layout_service import (
    auto_populate_device_cameras,
    auto_populate_all_devices,
    get_device_camera_layout,
    save_device_camera_layout,
    delete_device_camera_layout
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

    data.categories = [
        mapping.category for mapping in device.category_mappings
    ] if device.category_mappings else []

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

    # Apply category filter if provided
    if category_id:
        query = query.join(SiteCategoryMapping).filter(
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
    Get single device with full details including cameras and categories.
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

    Setting **site_id** moves the device to another site; its cameras and
    layouts move with it.

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
    Delete device and all associated data (cascades to cameras and layouts).

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


@router.put("/{device_id}/category")
async def assign_category_to_device(
    device_id: str,
    category_data: CategoryAssignment,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Assign category to device.

    Requires admin or super_admin privileges.
    """
    device = db.query(Device).filter(Device.id == device_id).first()

    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_id}' not found"
        )

    # Check if mapping already exists
    existing_mapping = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.device_id == device_id,
        SiteCategoryMapping.category_id == category_data.category_id
    ).first()

    if not existing_mapping:
        # Create new mapping
        mapping = SiteCategoryMapping(
            device_id=device_id,
            category_id=category_data.category_id
        )
        db.add(mapping)
        db.commit()

    return {"message": "Category assigned successfully"}


@router.post("/{device_id}/auto-populate-cameras", response_model=AutoPopulateResponse)
async def auto_populate_device_camera_layout(
    device_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Auto-populate camera layout for a single device.

    Creates or updates:
    - Layout configuration with optimal grid dimensions based on camera count
    - A layout entry for each camera in row-major order

    Grid sizing logic:
    - 1 camera → 1×1 grid
    - 2 cameras → 1×2 grid
    - 3-4 cameras → 2×2 grid
    - 5-6 cameras → 2×3 grid
    - 7-9 cameras → 3×3 grid
    - 10-12 cameras → 3×4 grid
    - 13-16 cameras → 4×4 grid

    Maximum of 16 cameras can be assigned to a device camera layout.

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_device_cameras(device_id, db)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate device cameras: {str(e)}"
        )


@router.post("/auto-populate-all-cameras", response_model=BulkAutoPopulateResponse)
async def auto_populate_all_device_cameras(
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Auto-populate camera layouts for all devices that have cameras.

    Processes each device that has cameras and creates/updates:
    - Layout configuration with optimal grid dimensions
    - A layout entry for each camera

    Devices without cameras are skipped.

    Returns summary including:
    - Total devices found
    - Devices successfully processed
    - Devices skipped (no cameras or errors)
    - Total cameras populated across all devices
    - Individual device results
    - Any errors encountered

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_all_devices(db)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate all device cameras: {str(e)}"
        )


# Manual camera layout management endpoints

@router.get("/{device_id}/camera-layout", response_model=SiteCameraLayoutConfigResponse)
async def get_device_camera_layout_config(
    device_id: str,
    db: DBSession,
    _auth = Depends(user_or_scope("devices:read", "devices:manage"))
):
    """
    Get the current camera layout configuration for a device.

    Returns:
    - Grid dimensions (n_rows × n_cols)
    - Total available slots
    - Number of cameras populated
    - Camera assignments with slot positions and camera names
    - Timestamps

    Returns 404 if:
    - Device not found
    - No layout configuration exists for the device
    """
    try:
        result = get_device_camera_layout(device_id, db)
        return result
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get camera layout: {str(e)}"
        )


@router.put("/{device_id}/camera-layout", response_model=SaveLayoutResponse)
async def save_device_camera_layout_config(
    device_id: str,
    layout_data: SaveLayoutRequest,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Manually create or update camera layout configuration for a device.

    Request body:
    - n_rows: Number of rows (1-4)
    - n_cols: Number of columns (1-4)
    - camera_slots: Array of camera slot assignments
      - Each slot specifies: slot_row, slot_col, camera_id

    Validation:
    - All camera IDs must exist and belong to this device
    - No duplicate camera IDs allowed
    - No duplicate slot positions allowed
    - Slot positions must be within grid bounds
    - Empty slots are allowed (just omit them)

    Operation:
    - Deletes existing layout configuration
    - Creates new configuration with specified grid and cameras
    - Transactional (all-or-nothing)

    Returns:
    - Summary of saved layout including grid size and camera count

    Errors:
    - 400: Validation errors (invalid cameras, duplicates, etc.)
    - 404: Device or camera not found

    Requires admin or super_admin privileges.
    """
    try:
        # Convert Pydantic models to dicts for service function
        camera_slots = [
            {
                "slot_row": slot.slot_row,
                "slot_col": slot.slot_col,
                "camera_id": slot.camera_id
            }
            for slot in layout_data.camera_slots
        ]

        result = save_device_camera_layout(
            device_id=device_id,
            n_rows=layout_data.n_rows,
            n_cols=layout_data.n_cols,
            camera_slots=camera_slots,
            db=db
        )
        return result
    except ValueError as e:
        # Validation or not found errors
        error_msg = str(e)
        if "not found" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save camera layout: {str(e)}"
        )


@router.delete("/{device_id}/camera-layout", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device_camera_layout_config(
    device_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("devices:manage"))
):
    """
    Delete the camera layout configuration for a device.

    Deletes:
    - The layout configuration record
    - All associated layout slot records (cascade)

    Returns:
    - 204 No Content on success

    Errors:
    - 404: Device not found or no layout exists

    Requires admin or super_admin privileges.
    """
    try:
        delete_device_camera_layout(device_id, db)
        return None
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete camera layout: {str(e)}"
        )
