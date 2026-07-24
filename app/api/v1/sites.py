"""
Site management API endpoints.

As of the Site → Device hierarchy, a **Site** is the physical place that owns
one or more Devices (NVR/DVRs). It carries the location and contact details;
the recorder credentials live on the Device (``app/api/v1/devices.py``).

Categories and camera layouts are site-level: a category describes a place, and
a site's camera grid may draw from any camera on any device at that place.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import func
from app.api.deps import DBSession, user_or_scope, admin_or_scope
from app.models.site import Site
from app.models.device import Device
from app.models.team import Team, site_team
from app.models.category import SiteCategoryMapping
from app.schemas.device import CategoryAssignment
from app.schemas.site import (
    SiteCreate,
    SiteUpdate,
    SiteResponse,
    SiteSummaryResponse,
    SiteDetailResponse,
    SiteListResponse,
)
from app.schemas.site_camera_layout import (
    AutoPopulateResponse,
    BulkAutoPopulateResponse,
    SiteCameraLayoutConfigResponse,
    SaveLayoutRequest,
    SaveLayoutResponse,
)
from app.services.site_camera_layout_service import (
    auto_populate_site_cameras,
    auto_populate_all_sites,
    get_site_camera_layout,
    save_site_camera_layout,
    delete_site_camera_layout,
)

router = APIRouter()

# Location / contact fields an update may touch, alongside `name`.
_LOCATION_FIELDS = (
    "customer_id",
    "address",
    "telephone",
    "telephone2",
    "telephone_police",
    "telephone_fire",
    "notes",
    "lat_long",
)


def _generate_site_id() -> str:
    """Mint a new site identifier.

    Matches the width minted by migration 008's backfill
    (``'SITE_' || upper(substr(md5(...), 1, 12))``).
    """
    return f"SITE_{uuid.uuid4().hex[:12].upper()}"


@router.get("", response_model=SiteListResponse)
async def list_sites(
    db: DBSession,
    _auth=Depends(user_or_scope("sites:read", "sites:manage")),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=1000),
):
    """
    List all sites with pagination and a device count per site.

    - **page**: Page number (default: 1)
    - **per_page**: Items per page (default: 50, max: 1000)
    """
    query = db.query(Site)

    total = query.count()

    offset = (page - 1) * per_page
    sites = query.order_by(Site.name).offset(offset).limit(per_page).all()

    site_ids = [site.id for site in sites]
    counts = {}
    if site_ids:
        rows = (
            db.query(Device.site_id, func.count(Device.id))
            .filter(Device.site_id.in_(site_ids))
            .group_by(Device.site_id)
            .all()
        )
        counts = {site_id: count for site_id, count in rows}

    sites_response = []
    for site in sites:
        site_data = SiteSummaryResponse.model_validate(site)
        site_data.device_count = counts.get(site.id, 0)
        sites_response.append(site_data)

    return SiteListResponse(
        sites=sites_response, total=total, page=page, per_page=per_page
    )


@router.post("", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    site_data: SiteCreate, db: DBSession, _auth=Depends(admin_or_scope("sites:manage"))
):
    """
    Create a new site.

    Requires admin or super_admin privileges.
    """
    # Validate every requested team exists before creating anything.
    requested_teams = list(dict.fromkeys(site_data.team_ids))
    found_teams = {
        t.id for t in db.query(Team.id).filter(Team.id.in_(requested_teams)).all()
    }
    missing_teams = [tid for tid in requested_teams if tid not in found_teams]
    if missing_teams:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown team(s): {', '.join(missing_teams)}",
        )

    new_site = Site(
        id=_generate_site_id(),
        name=site_data.name,
        customer_id=site_data.customer_id,
        address=site_data.address,
        telephone=site_data.telephone,
        telephone2=site_data.telephone2,
        telephone_police=site_data.telephone_police,
        telephone_fire=site_data.telephone_fire,
        notes=site_data.notes,
        lat_long=site_data.lat_long,
    )

    db.add(new_site)
    db.flush()

    for team_id in requested_teams:
        db.execute(site_team.insert().values(site_id=new_site.id, team_id=team_id))

    db.commit()
    db.refresh(new_site)

    return new_site


@router.get("/{site_id}", response_model=SiteDetailResponse)
async def get_site(
    site_id: str,
    db: DBSession,
    _auth=Depends(user_or_scope("sites:read", "sites:manage")),
):
    """
    Get a single site with full details, including the devices it holds.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Site '{site_id}' not found"
        )

    site_data = SiteDetailResponse.model_validate(site)
    site_data.device_count = len(site.devices) if site.devices else 0
    for device in site_data.devices:
        device.site_name = site.name

    return site_data


@router.api_route("/{site_id}", methods=["PUT", "PATCH"], response_model=SiteResponse)
async def update_site(
    site_id: str,
    site_data: SiteUpdate,
    db: DBSession,
    _auth=Depends(admin_or_scope("sites:manage")),
):
    """
    Update site name and location/contact details.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Site '{site_id}' not found"
        )

    if site_data.name is not None:
        site.name = site_data.name

    for field in _LOCATION_FIELDS:
        value = getattr(site_data, field)
        if value is not None:
            setattr(site, field, value)

    db.commit()
    db.refresh(site)

    return site


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    site_id: str, db: DBSession, _auth=Depends(admin_or_scope("sites:manage"))
):
    """
    Delete a site and everything below it.

    Cascades to the site's devices and, through them, their cameras, category
    assignments, camera layouts and screen mappings.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Site '{site_id}' not found"
        )

    db.delete(site)
    db.commit()

    return None


@router.post("/auto-populate-all-cameras", response_model=BulkAutoPopulateResponse)
async def auto_populate_all_site_cameras(
    db: DBSession, _auth=Depends(admin_or_scope("sites:manage"))
):
    """
    Auto-populate camera layouts for all sites that have cameras.

    Processes each site that has cameras on at least one of its devices and
    creates/updates:
    - Layout configuration with optimal grid dimensions
    - A layout entry for each camera

    Sites without cameras are skipped.

    Returns summary including:
    - Total sites found
    - Sites successfully processed
    - Sites skipped (no cameras or errors)
    - Total cameras populated across all sites
    - Individual site results
    - Any errors encountered

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_all_sites(db)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate all site cameras: {str(e)}",
        )


@router.put("/{site_id}/category")
async def assign_category_to_site(
    site_id: str,
    category_data: CategoryAssignment,
    db: DBSession,
    _auth=Depends(admin_or_scope("sites:manage")),
):
    """
    Assign category to site.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Site '{site_id}' not found"
        )

    # Check if mapping already exists
    existing_mapping = (
        db.query(SiteCategoryMapping)
        .filter(
            SiteCategoryMapping.site_id == site_id,
            SiteCategoryMapping.category_id == category_data.category_id,
        )
        .first()
    )

    if not existing_mapping:
        # Create new mapping
        mapping = SiteCategoryMapping(
            site_id=site_id, category_id=category_data.category_id
        )
        db.add(mapping)
        db.commit()

    return {"message": "Category assigned successfully"}


@router.post("/{site_id}/auto-populate-cameras", response_model=AutoPopulateResponse)
async def auto_populate_site_camera_layout(
    site_id: str, db: DBSession, _auth=Depends(admin_or_scope("sites:manage"))
):
    """
    Auto-populate camera layout for a single site.

    Creates or updates:
    - Layout configuration with optimal grid dimensions based on camera count
    - A layout entry for each camera in row-major order

    Cameras are drawn from every device at the site, ordered by device name
    then camera name.

    Grid sizing logic:
    - 1 camera → 1×1 grid
    - 2 cameras → 1×2 grid
    - 3-4 cameras → 2×2 grid
    - 5-6 cameras → 2×3 grid
    - 7-9 cameras → 3×3 grid
    - 10-12 cameras → 3×4 grid
    - 13-16 cameras → 4×4 grid

    Maximum of 16 cameras can be assigned to a site camera layout.

    Requires admin or super_admin privileges.
    """
    try:
        result = auto_populate_site_cameras(site_id, db)
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to auto-populate site cameras: {str(e)}",
        )


# Manual camera layout management endpoints


@router.get("/{site_id}/camera-layout", response_model=SiteCameraLayoutConfigResponse)
async def get_site_camera_layout_config(
    site_id: str,
    db: DBSession,
    _auth=Depends(user_or_scope("sites:read", "sites:manage")),
):
    """
    Get the current camera layout configuration for a site.

    Returns:
    - Grid dimensions (n_rows × n_cols)
    - Total available slots
    - Number of cameras populated
    - Camera assignments with slot positions and camera names
    - Timestamps

    Returns 404 if:
    - Site not found
    - No layout configuration exists for the site
    """
    try:
        result = get_site_camera_layout(site_id, db)
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get camera layout: {str(e)}",
        )


@router.put("/{site_id}/camera-layout", response_model=SaveLayoutResponse)
async def save_site_camera_layout_config(
    site_id: str,
    layout_data: SaveLayoutRequest,
    db: DBSession,
    _auth=Depends(admin_or_scope("sites:manage")),
):
    """
    Manually create or update camera layout configuration for a site.

    Request body:
    - n_rows: Number of rows (1-4)
    - n_cols: Number of columns (1-4)
    - camera_slots: Array of camera slot assignments
      - Each slot specifies: slot_row, slot_col, camera_id

    Validation:
    - All camera IDs must exist and be available at this site — a camera on any
      of the site's devices is accepted
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
    - 404: Site or camera not found

    Requires admin or super_admin privileges.
    """
    try:
        # Convert Pydantic models to dicts for service function
        camera_slots = [
            {
                "slot_row": slot.slot_row,
                "slot_col": slot.slot_col,
                "camera_id": slot.camera_id,
            }
            for slot in layout_data.camera_slots
        ]

        result = save_site_camera_layout(
            site_id=site_id,
            n_rows=layout_data.n_rows,
            n_cols=layout_data.n_cols,
            camera_slots=camera_slots,
            db=db,
        )
        return result
    except ValueError as e:
        # Validation or not found errors
        error_msg = str(e)
        if "not found" in error_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_msg)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save camera layout: {str(e)}",
        )


@router.delete("/{site_id}/camera-layout", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site_camera_layout_config(
    site_id: str, db: DBSession, _auth=Depends(admin_or_scope("sites:manage"))
):
    """
    Delete the camera layout configuration for a site.

    Deletes:
    - The layout configuration record
    - All associated layout slot records

    Returns:
    - 204 No Content on success

    Errors:
    - 404: Site not found or no layout exists

    Requires admin or super_admin privileges.
    """
    try:
        delete_site_camera_layout(site_id, db)
        return None
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete camera layout: {str(e)}",
        )
