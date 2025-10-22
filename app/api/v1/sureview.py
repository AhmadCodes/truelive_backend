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
from app.models.sync_job import SyncJob, SyncJobStatus
from app.schemas.sureview import (
    GetSitesRequest,
    GetSitesResponse,
    SiteDetail,
    CustomerSitesGroup,
    CustomerSiteSummary,
    GetCamerasRequest,
    CameraDetail
)
from app.schemas.sync_job import SyncJobResponse, SyncJobStartResponse
from app.services.sureview_service import sync_sureview_devices
from app.tasks.sureview_tasks import sync_devices_async
import uuid

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


@router.post("/sync/async", response_model=SyncJobStartResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_async_sync(
    current_user: AdminUser,
    db: DBSession
):
    """
    Start asynchronous SureView device synchronization.

    This endpoint creates a sync job and returns immediately with a job_id.
    The sync runs in the background via Celery. Use the status endpoint to
    check progress and completion.

    Process:
    1. Creates SyncJob record in database
    2. Queues Celery task to run sync
    3. Returns job_id for status polling

    Only admins and super admins can trigger sync.

    Args:
        current_user: Current authenticated admin user
        db: Database session

    Returns:
        Job ID and initial status (pending)
    """
    logger.info(f"Async SureView sync triggered by user {current_user.username}")

    try:
        # Create sync job record
        job_id = str(uuid.uuid4())
        sync_job = SyncJob(
            id=job_id,
            status=SyncJobStatus.PENDING,
            progress=0,
            progress_message="Sync job queued",
            triggered_by=str(current_user.user_id)
        )
        db.add(sync_job)
        db.commit()

        logger.info(f"Created sync job {job_id}")

        # Queue Celery task
        sync_devices_async.delay(job_id)

        logger.info(f"Queued async sync task for job {job_id}")

        return SyncJobStartResponse(
            job_id=job_id,
            status=SyncJobStatus.PENDING,
            message="Sync job started successfully. Use job_id to check status."
        )

    except Exception as e:
        logger.error(f"Error starting async sync: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start sync job: {str(e)}"
        )


@router.get("/sync/status/{job_id}", response_model=SyncJobResponse)
async def get_sync_status(
    job_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get status of an async sync job.

    This endpoint allows frontend to poll for sync progress and results.
    Recommended polling interval: 2-5 seconds while job is in progress.

    Args:
        job_id: UUID of the sync job
        current_user: Current authenticated user
        db: Database session

    Returns:
        Complete job status including progress, results, and errors

    Raises:
        404: If job_id not found
    """
    sync_job = db.query(SyncJob).filter(SyncJob.id == job_id).first()

    if not sync_job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sync job {job_id} not found"
        )

    return SyncJobResponse(
        id=str(sync_job.id),
        status=sync_job.status,
        progress=sync_job.progress,
        progress_message=sync_job.progress_message,
        started_at=sync_job.started_at,
        completed_at=sync_job.completed_at,
        created_at=sync_job.created_at,
        result=sync_job.result,
        error_message=sync_job.error_message,
        triggered_by=str(sync_job.triggered_by) if sync_job.triggered_by else None
    )


@router.get("/sync/last", response_model=SyncJobResponse)
async def get_last_sync(
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get the most recent completed sync job.

    This endpoint returns the timestamp and details of the last successful
    sync operation, useful for displaying "Last synced: X minutes ago" in UI.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        Most recent completed sync job with timestamp

    Raises:
        404: If no completed sync jobs found
    """
    # Get most recent completed sync (either COMPLETED or FAILED status)
    last_sync = db.query(SyncJob).filter(
        SyncJob.status.in_([SyncJobStatus.COMPLETED, SyncJobStatus.FAILED])
    ).order_by(
        SyncJob.completed_at.desc()
    ).first()

    if not last_sync:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed sync jobs found"
        )

    return SyncJobResponse(
        id=str(last_sync.id),
        status=last_sync.status,
        progress=last_sync.progress,
        progress_message=last_sync.progress_message,
        started_at=last_sync.started_at,
        completed_at=last_sync.completed_at,
        created_at=last_sync.created_at,
        result=last_sync.result,
        error_message=last_sync.error_message,
        triggered_by=str(last_sync.triggered_by) if last_sync.triggered_by else None
    )


@router.get("/sync/jobs", response_model=List[SyncJobResponse])
async def get_user_sync_jobs(
    current_user: CurrentUser,
    db: DBSession,
    limit: int = 10,
    include_completed: bool = True
):
    """
    Get sync jobs for the current user.

    This endpoint returns recent sync jobs started by the current user,
    allowing the frontend to:
    - Recover active jobs after page refresh
    - Display sync history
    - Show currently running syncs

    Args:
        current_user: Current authenticated user
        db: Database session
        limit: Maximum number of jobs to return (default: 10)
        include_completed: Include completed/failed jobs (default: true)

    Returns:
        List of sync jobs ordered by creation time (newest first)
    """
    query = db.query(SyncJob).filter(
        SyncJob.triggered_by == current_user.user_id
    )

    # If not including completed, only show pending/in_progress
    if not include_completed:
        query = query.filter(
            SyncJob.status.in_([SyncJobStatus.PENDING, SyncJobStatus.IN_PROGRESS])
        )

    jobs = query.order_by(
        SyncJob.created_at.desc()
    ).limit(limit).all()

    return [
        SyncJobResponse(
            id=str(job.id),
            status=job.status,
            progress=job.progress,
            progress_message=job.progress_message,
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
            result=job.result,
            error_message=job.error_message,
            triggered_by=str(job.triggered_by) if job.triggered_by else None
        )
        for job in jobs
    ]
