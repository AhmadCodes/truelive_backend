"""
SureView device synchronization background tasks.
"""
import logging
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
    3. Fetches devices for each server
    4. Updates database with sites and cameras
    5. Removes stale entries

    Returns:
        dict: Summary of operations performed
    """
    logger.info("Starting SureView device sync task")
    db = SessionLocal()

    try:
        # Run sync
        result = sync_sureview_devices(db=db)

        logger.info(f"SureView sync task completed: {result}")
        return result

    except Exception as e:
        logger.error(f"Error in SureView sync task: {e}")
        raise

    finally:
        db.close()


@celery_app.task(name='app.tasks.sureview_tasks.sync_single_server')
def sync_single_server(server_id: int):
    """
    Sync devices for a single SureView server.

    This can be called on-demand for immediate server sync.

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
            get_group_details
        )
        from app.models.site import Site
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

        # Fetch group details for additional site information
        group_data = None
        if "groupID" in target_server and target_server["groupID"]:
            group_data = get_group_details(cookies, target_server["groupID"])

        # Update or create site
        site_id = str(target_server["serverID"])
        site = db.query(Site).filter(Site.id == site_id).first()

        if site:
            # Update existing
            site.name = target_server["title"]
            site.nvr_username = target_server["username"]
            site.nvr_password = target_server["password"]
            site.sureview_site = True

            # Update group details if available
            if group_data:
                site.customer_id = group_data.get("referenceId")
                site.address = group_data.get("address")
                site.telephone = group_data.get("telephone")
                site.telephone2 = group_data.get("telephone2")
                site.telephone_police = group_data.get("telephonePolice")
                site.telephone_fire = group_data.get("telephoneFire")
                site.notes = group_data.get("notes")
                site.lat_long = group_data.get("latLong")
        else:
            # Create new
            site = Site(
                id=site_id,
                name=target_server["title"],
                nvr_username=target_server["username"],
                nvr_password=target_server["password"],
                sureview_site=True,
                new=True
            )

            # Add group details if available
            if group_data:
                site.customer_id = group_data.get("referenceId")
                site.address = group_data.get("address")
                site.telephone = group_data.get("telephone")
                site.telephone2 = group_data.get("telephone2")
                site.telephone_police = group_data.get("telephonePolice")
                site.telephone_fire = group_data.get("telephoneFire")
                site.notes = group_data.get("notes")
                site.lat_long = group_data.get("latLong")

            db.add(site)

        # Get devices for this server
        devices = get_devices_by_server_id(cookies, server_id) or []

        for device in devices:
            try:
                # Only process devices that belong to this server
                if device["serverID"] != target_server["serverID"]:
                    continue

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
                        site_id=site_id,
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
