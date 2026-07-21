"""
SureView device synchronization background tasks.

Naming caution: "device" in this module's task names follows SureView's
vocabulary, where a *server* is an NVR/DVR and a *device* is a camera. Since
migration 008 our own ``Device`` model is the NVR/DVR, so ``sync_devices`` and
``sync_devices_async`` actually sync SureView servers -> our Devices AND
SureView devices -> our Cameras. The task names are NOT renamed: they are
registered strings referenced by the beat schedule in
``app/tasks/celery_app.py:58``.
"""
import logging
from datetime import datetime, timezone
from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.services.sureview_service import sync_sureview_devices

logger = logging.getLogger(__name__)


@celery_app.task(name='app.tasks.sureview_tasks.sync_devices')
def sync_devices():
    """
    Celery task to sync devices from SureView.

    This task runs every 10 minutes (configured in celery_app.py).
    It:
    1. Authenticates to SureView via Selenium
    2. Fetches server list
    3. Fetches devices (cameras) for each server
    4. Updates database with Devices and Cameras
    5. Removes stale entries

    Creates a SyncJob record for tracking, with triggered_by='system'
    to indicate automatic background sync.

    Returns:
        dict: Summary of operations performed
    """
    from app.models.sync_job import SyncJob, SyncJobStatus
    import uuid

    logger.info("Starting scheduled SureView device sync task")
    db = SessionLocal()

    try:
        # Create sync job record for tracking
        sync_job = SyncJob(
            status=SyncJobStatus.IN_PROGRESS,
            progress=0,
            progress_message="Scheduled sync started",
            triggered_by=None,  # NULL indicates automatic background sync
            started_at=datetime.now(timezone.utc)
        )
        db.add(sync_job)
        db.commit()
        db.refresh(sync_job)  # Refresh to get the auto-generated UUID

        logger.info(f"Created scheduled sync job {sync_job.id}")

        # Run sync
        sync_job.progress = 20
        sync_job.progress_message = "Authenticating to SureView..."
        db.commit()

        result = sync_sureview_devices(db=db)

        # Update job with results
        sync_job.status = SyncJobStatus.COMPLETED if result.get("errors", 0) == 0 else SyncJobStatus.FAILED
        sync_job.progress = 100
        sync_job.progress_message = "Scheduled sync completed successfully" if sync_job.status == SyncJobStatus.COMPLETED else "Scheduled sync completed with errors"
        sync_job.completed_at = datetime.now(timezone.utc)
        sync_job.result = result

        if result.get("errors", 0) > 0:
            sync_job.error_message = f"Sync completed with {result['errors']} errors"

        db.commit()

        logger.info(f"Scheduled sync task completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Error in scheduled sync task: {e}")

        # Update job status to failed
        try:
            if sync_job and sync_job.id:
                sync_job.status = SyncJobStatus.FAILED
                sync_job.progress = 100
                sync_job.progress_message = "Scheduled sync failed with error"
                sync_job.completed_at = datetime.now(timezone.utc)
                sync_job.error_message = str(e)
                db.commit()
        except Exception as update_error:
            logger.error(f"Failed to update job status: {update_error}")

        raise

    finally:
        db.close()


@celery_app.task(name='app.tasks.sureview_tasks.sync_single_server')
def sync_single_server(server_id: int):
    """
    Sync one SureView server (== one of our Devices) and its cameras.

    This can be called on-demand for immediate server sync.

    Parent Site handling mirrors ``sync_sureview_devices`` (AC-25): a brand-new
    Device gets a fresh 1:1 parent Site built from the SureView group data; a
    Device that already has a parent Site keeps that Site's name and all 8
    location fields completely untouched.

    Args:
        server_id: Server identifier

    Returns:
        dict: Result of operation
    """
    logger.info(f"Syncing devices for server {server_id}")
    db = SessionLocal()

    try:
        from app.services.sureview_service import (
            automate_login,
            get_server_list,
            get_devices_by_server_id,
            get_group_details,
            create_parent_site
        )
        from app.models.device import Device
        from app.models.camera import Camera

        result = {
            "cameras_updated": 0,
            "errors": 0
        }

        # Login and get cookies
        cookies = automate_login()

        if not cookies:
            logger.error("Failed to authenticate to SureView")
            result["errors"] += 1
            return result

        # Get server list to find this specific server's details
        servers = get_server_list(cookies)

        if not servers:
            logger.error("Failed to fetch server list")
            result["errors"] += 1
            return result

        # Find the target server in the list
        target_server = None
        for server in servers:
            if server["serverID"] == server_id:
                target_server = server
                break

        if not target_server:
            logger.error(f"Server {server_id} not found in SureView API")
            result["errors"] += 1
            return result

        # Fetch group details — only ever consumed when minting a NEW parent Site
        group_data = None
        if "groupID" in target_server and target_server["groupID"]:
            group_data = get_group_details(cookies, target_server["groupID"])

        # Update or create device
        device_id = str(target_server["serverID"])
        device_record = db.query(Device).filter(Device.id == device_id).first()

        if device_record:
            # Update the Device's OWN fields only. AC-25: an already-parented
            # Device keeps its parent Site exactly as the operator left it —
            # name and all 8 location fields are NOT rewritten from group_data.
            device_record.name = target_server["title"]
            device_record.nvr_username = target_server["username"]
            device_record.nvr_password = target_server["password"]
            device_record.sureview_site = True

            if not device_record.site_id:
                logger.warning(
                    f"Device {device_id} has no parent site; minting one"
                )
                parent_site = create_parent_site(
                    db, target_server["title"], group_data
                )
                device_record.site_id = parent_site.id
        else:
            # Brand-new Device: mint its 1:1 parent Site as well.
            parent_site = create_parent_site(
                db, target_server["title"], group_data
            )
            device_record = Device(
                id=device_id,
                name=target_server["title"],
                site_id=parent_site.id,
                nvr_username=target_server["username"],
                nvr_password=target_server["password"],
                sureview_site=True,
                new=True
            )
            db.add(device_record)

        db.flush()

        # Get SureView devices (== our cameras) for this server
        sureview_devices = get_devices_by_server_id(cookies, server_id) or []

        for device in sureview_devices:
            try:
                camera_id = str(device["deviceID"])

                # Build RTSP URL using server and device data
                rtsp_url = (
                    f"rtsp://{target_server['username']}:{target_server['password']}@"
                    f"{target_server['host']}:{target_server['port']}/"
                    f"{target_server['extraValue'].replace('{#}', str(device['input1']))}"
                )

                # Update or create camera
                camera = db.query(Camera).filter(Camera.id == camera_id).first()

                if camera:
                    camera.name = device["title"]
                    camera.rtsp_url = rtsp_url
                    camera.sureview_camera = True
                else:
                    camera = Camera(
                        id=camera_id,
                        device_id=device_id,
                        name=device["title"],
                        rtsp_url=rtsp_url,
                        sureview_camera=True,
                        new=True
                    )
                    db.add(camera)

                result["cameras_updated"] += 1

            except Exception as e:
                logger.error(f"Error processing device {device.get('deviceID')}: {e}")
                result["errors"] += 1

        db.commit()

        logger.info(f"Server {server_id} sync completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Error syncing server {server_id}: {e}")
        db.rollback()
        raise

    finally:
        db.close()


@celery_app.task(name='app.tasks.sureview_tasks.sync_devices_async', bind=True)
def sync_devices_async(self, job_id: str):
    """
    Async Celery task to sync devices from SureView with job status tracking.

    This task updates the SyncJob model with progress and results.

    Args:
        self: Celery task instance (bind=True provides access to task info)
        job_id: UUID of the SyncJob tracking this operation

    Returns:
        dict: Summary of operations performed
    """
    from app.models.sync_job import SyncJob, SyncJobStatus

    logger.info(f"Starting async SureView sync for job {job_id}")
    db = SessionLocal()

    try:
        # Get the job record
        job = db.query(SyncJob).filter(SyncJob.id == job_id).first()

        if not job:
            logger.error(f"Job {job_id} not found")
            return {"error": "Job not found"}

        # Update job status to in_progress
        job.status = SyncJobStatus.IN_PROGRESS
        job.started_at = datetime.now(timezone.utc)
        job.progress = 10
        job.progress_message = "Starting SureView authentication..."
        db.commit()

        # Run the actual sync
        job.progress = 20
        job.progress_message = "Fetching servers from SureView API..."
        db.commit()

        result = sync_sureview_devices(db=db)

        # Update job with results
        job.status = SyncJobStatus.COMPLETED if result.get("errors", 0) == 0 else SyncJobStatus.FAILED
        job.progress = 100
        job.progress_message = "Sync completed successfully" if job.status == SyncJobStatus.COMPLETED else "Sync completed with errors"
        job.completed_at = datetime.now(timezone.utc)
        job.result = result

        if result.get("errors", 0) > 0:
            job.error_message = f"Sync completed with {result['errors']} errors"

        db.commit()

        logger.info(f"Async sync job {job_id} completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Error in async sync job {job_id}: {e}")

        # Update job status to failed
        try:
            job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
            if job:
                job.status = SyncJobStatus.FAILED
                job.progress = 100
                job.progress_message = "Sync failed with error"
                job.completed_at = datetime.now(timezone.utc)
                job.error_message = str(e)
                db.commit()
        except Exception as update_error:
            logger.error(f"Failed to update job status: {update_error}")

        raise

    finally:
        db.close()
