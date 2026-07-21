"""
Site management API endpoints.

As of the Site → Device hierarchy, a **Site** is the physical place that owns
one or more Devices (NVR/DVRs). It carries the location and contact details;
the recorder credentials live on the Device (``app/api/v1/devices.py``).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import func
from app.api.deps import DBSession, user_or_scope, admin_or_scope
from app.models.site import Site
from app.models.device import Device
from app.schemas.site import (
    SiteCreate,
    SiteUpdate,
    SiteResponse,
    SiteSummaryResponse,
    SiteDetailResponse,
    SiteListResponse
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
    _auth = Depends(user_or_scope("sites:read", "sites:manage")),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=1000)
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
        rows = db.query(
            Device.site_id, func.count(Device.id)
        ).filter(
            Device.site_id.in_(site_ids)
        ).group_by(Device.site_id).all()
        counts = {site_id: count for site_id, count in rows}

    sites_response = []
    for site in sites:
        site_data = SiteSummaryResponse.model_validate(site)
        site_data.device_count = counts.get(site.id, 0)
        sites_response.append(site_data)

    return SiteListResponse(
        sites=sites_response,
        total=total,
        page=page,
        per_page=per_page
    )


@router.post("", response_model=SiteResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    site_data: SiteCreate,
    db: DBSession,
    _auth = Depends(admin_or_scope("sites:manage"))
):
    """
    Create a new site.

    Requires admin or super_admin privileges.
    """
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
        lat_long=site_data.lat_long
    )

    db.add(new_site)
    db.commit()
    db.refresh(new_site)

    return new_site


@router.get("/{site_id}", response_model=SiteDetailResponse)
async def get_site(
    site_id: str,
    db: DBSession,
    _auth = Depends(user_or_scope("sites:read", "sites:manage"))
):
    """
    Get a single site with full details, including the devices it holds.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site '{site_id}' not found"
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
    _auth = Depends(admin_or_scope("sites:manage"))
):
    """
    Update site name and location/contact details.

    Requires admin or super_admin privileges.
    """
    site = db.query(Site).filter(Site.id == site_id).first()

    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site '{site_id}' not found"
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
    site_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("sites:manage"))
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
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site '{site_id}' not found"
        )

    db.delete(site)
    db.commit()

    return None
