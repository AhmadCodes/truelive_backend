"""
SureView API endpoints for site and camera management.
"""

from fastapi import APIRouter, HTTPException, status
from typing import List
from sqlalchemy import func

from app.api.deps import DBSession, CurrentUser
from app.models.site import Site
from app.models.camera import Camera
from app.schemas.sureview import (
    GetSitesRequest,
    GetSitesResponse,
    SiteDetail,
    CustomerSitesGroup,
    CustomerSiteSummary,
    GetCamerasRequest,
    CameraDetail
)

router = APIRouter()


@router.post("/get_sites", response_model=GetSitesResponse)
async def get_sites(
    request: GetSitesRequest,
    db: DBSession,
    current_user: CurrentUser
):
    """
    Get sites filtered by customer_id and optionally by site_ids.

    Args:
        request: Request containing customer_id and optional site_ids
        db: Database session
        current_user: Current authenticated user

    Returns:
        Sites matching the filters with full details
    """
    # Build base query
    query = db.query(Site).filter(Site.customer_id == request.customer_id)

    # Apply site_ids filter if provided
    if request.site_ids:
        query = query.filter(Site.id.in_(request.site_ids))

    sites = query.all()

    # Build response
    site_details = []
    for site in sites:
        # Count cameras for this site
        camera_count = db.query(func.count(Camera.id)).filter(
            Camera.site_id == site.id
        ).scalar() or 0

        site_detail = SiteDetail(
            address=site.address,
            telephone=site.telephone,
            telephone2=site.telephone2,
            telephonePolice=site.telephone_police,
            telephoneFire=site.telephone_fire,
            notes=site.notes,
            latLong=site.lat_long,
            site_id=site.id,
            name=site.name,
            camera_count=camera_count
        )
        site_details.append(site_detail)

    return GetSitesResponse(
        customer_id=request.customer_id,
        sites=site_details
    )


@router.post("/get_all_sites", response_model=List[CustomerSitesGroup])
async def get_all_sites(
    db: DBSession,
    current_user: CurrentUser
):
    """
    Get all sites grouped by customer_id.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        List of customer groups with their sites
    """
    # Get all sites with customer_id
    sites = db.query(Site).filter(Site.customer_id.isnot(None)).all()

    # Group sites by customer_id
    customer_groups = {}
    for site in sites:
        customer_id = site.customer_id

        if customer_id not in customer_groups:
            customer_groups[customer_id] = []

        # Count cameras for this site
        camera_count = db.query(func.count(Camera.id)).filter(
            Camera.site_id == site.id
        ).scalar() or 0

        customer_site = CustomerSiteSummary(
            customer_id=customer_id,
            site_id=site.id,
            name=site.name,
            camera_count=camera_count
        )
        customer_groups[customer_id].append(customer_site)

    # Build response
    response = []
    for customer_id, customer_sites in customer_groups.items():
        response.append(
            CustomerSitesGroup(
                customer_id=customer_id,
                customer_sites=customer_sites
            )
        )

    return response


@router.post("/get_cameras", response_model=List[CameraDetail])
async def get_cameras(
    request: GetCamerasRequest,
    db: DBSession,
    current_user: CurrentUser
):
    """
    Get all cameras for a specific site.

    Args:
        request: Request containing site_id
        db: Database session
        current_user: Current authenticated user

    Returns:
        List of cameras for the site

    Raises:
        HTTPException: If site not found
    """
    # Verify site exists
    site = db.query(Site).filter(Site.id == request.site_id).first()
    if not site:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Site with id {request.site_id} not found"
        )

    # Get cameras for the site
    cameras = db.query(Camera).filter(Camera.site_id == request.site_id).all()

    # Build response
    camera_details = []
    for camera in cameras:
        camera_detail = CameraDetail(
            camera_id=camera.id,
            camera_name=camera.name,
            rtsp_url=camera.rtsp_url
        )
        camera_details.append(camera_detail)

    return camera_details
