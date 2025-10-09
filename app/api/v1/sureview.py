"""
SureView API endpoints for site and camera management.
"""

from fastapi import APIRouter, HTTPException, status, BackgroundTasks
from typing import List
from sqlalchemy import func
import logging

from app.api.deps import DBSession, CurrentUser, AdminUser
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
from app.services.sureview_service import sync_sureview_devices

logger = logging.getLogger(__name__)
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


@router.post("/sync")
async def trigger_sync(
    current_user: AdminUser,
    db: DBSession
):
    """
    Manually trigger SureView device synchronization.

    This endpoint triggers an immediate sync of all SureView sites and cameras.
    The sync process:
    1. Authenticates to SureView via Selenium
    2. Fetches all servers and their devices
    3. Updates or creates sites and cameras in the database
    4. Fetches group details for additional site information

    Only admins and super admins can trigger sync.

    Args:
        current_user: Current authenticated admin user
        db: Database session

    Returns:
        Sync result summary with counts of updated/removed items
    """
    logger.info(f"Manual SureView sync triggered by user {current_user.username}")

    try:
        # Execute sync synchronously
        result = sync_sureview_devices(db=db)

        logger.info(f"Manual sync completed: {result}")

        return {
            "success": result.get("errors", 0) == 0,
            "message": "SureView sync completed",
            "result": result
        }

    except Exception as e:
        logger.error(f"Error during manual sync: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sync failed: {str(e)}"
        )
