"""
SureView integration service.

Syncs camera and device data from SureView API using Selenium for authentication.

Naming caution: SureView's own API vocabulary is the inverse of ours. A SureView
*server* (``serverID``) is one NVR/DVR and maps 1:1 to our :class:`Device`; a
SureView *device* (``deviceID``) is a single camera and maps to our
:class:`Camera`. Function names below that say "device" in the SureView sense
(``get_devices_by_server_id``) are returning cameras.
"""
import os
import time
import uuid
import logging
import requests
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import func
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from app.core.config import settings
from app.models.site import Site
from app.models.device import Device
from app.models.camera import Camera

logger = logging.getLogger(__name__)

# Location fields copied from SureView group data onto a *newly minted* parent
# Site. Once a Device has a parent Site, sync must never rewrite these (AC-25):
# operators consolidate several NVRs under one Site and hand-edit the address.
_GROUP_TO_SITE_FIELDS = {
    "customer_id": "referenceId",
    "address": "address",
    "telephone": "telephone",
    "telephone2": "telephone2",
    "telephone_police": "telephonePolice",
    "telephone_fire": "telephoneFire",
    "notes": "notes",
    "lat_long": "latLong",
}


def create_parent_site(
    db: Session,
    name: str,
    group_data: Optional[Dict[str, Any]]
) -> Site:
    """
    Mint a fresh 1:1 parent Site for a brand-new Device.

    Only ever called when a Device has no parent yet. Existing parents are left
    completely untouched by sync (AC-25).

    Args:
        db: Database session
        name: Site name (mirrors the Device name at creation time)
        group_data: SureView group details, or None if unavailable

    Returns:
        The newly created (flushed) Site
    """
    site = Site(
        # Must match the scheme used by POST /api/v1/sites and by migration
        # 008's backfill ('SITE_' || upper(substr(md5(d.id), 1, 12))), so every
        # parent Site id has the same shape regardless of how it was created.
        id=f"SITE_{uuid.uuid4().hex[:12].upper()}",
        name=name,
    )

    if group_data:
        for site_attr, group_key in _GROUP_TO_SITE_FIELDS.items():
            setattr(site, site_attr, group_data.get(group_key))

    db.add(site)
    db.flush()
    logger.info(f"Created parent site {site.id} ('{name}') for new device")
    return site


def is_docker() -> bool:
    """
    Check if running inside a Docker container.

    Returns:
        True if running in Docker, False otherwise
    """
    return (
        os.path.exists('/.dockerenv')
        or os.getenv('AM_I_IN_A_DOCKER_CONTAINER', 'False') == 'True'
    )


def get_chrome_options() -> Options:
    """
    Get Chrome options for headless execution.

    Returns:
        Configured Chrome options
    """
    chrome_options = Options()
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-port=9222")

    # Use system-installed Chromium if CHROME_BIN is set (e.g. in Docker)
    chrome_bin = os.getenv('CHROME_BIN')
    if chrome_bin:
        chrome_options.binary_location = chrome_bin

    return chrome_options


def automate_login() -> Optional[List[Dict[str, str]]]:
    """
    Automate login to SureView and retrieve authentication cookies.

    Returns:
        List of cookie dicts if successful, None otherwise
    """
    chrome_options = get_chrome_options()
    driver = None

    try:
        # Initialize WebDriver
        if is_docker():
            chromedriver_path = os.getenv('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(options=chrome_options)

        wait = WebDriverWait(driver, 30)

        # Navigate to login page
        logger.info("Opening SureView login page...")
        driver.get("https://suite.sureviewops.com/login")

        # Enter username
        logger.info("Entering username...")
        email_field = wait.until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        email_field.send_keys(settings.SUREVIEW_USERNAME)

        # Click next button
        next_button = driver.find_element(By.ID, "next")
        next_button.click()

        # Enter password
        logger.info("Waiting for password field...")
        password_field = wait.until(
            EC.presence_of_element_located((By.ID, "password"))
        )
        password_field.send_keys(settings.SUREVIEW_PASSWORD)

        # Submit login form
        submit_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"]'))
        )
        submit_button.click()

        # Navigate to US SureView dashboard
        logger.info("Navigating to US SureView dashboard...")
        grid_button = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn.dropdown-toggle.btn-secondary"))
        )
        grid_button.click()

        response_us = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, 'us.sureviewops.com')]"))
        )
        response_us.click()

        # Switch to new window
        driver.switch_to.window(driver.window_handles[-1])
        wait.until(EC.presence_of_element_located((By.ID, "menuBarParent")))

        # Get cookies
        cookies = driver.get_cookies()
        logger.info("Login successful, cookies retrieved.")
        return cookies

    except Exception as e:
        logger.error(f"Error during login: {e}")
        return None

    finally:
        if driver:
            driver.quit()
            logger.info("WebDriver closed.")


def get_server_list(cookies: List[Dict[str, str]], page_size: int = 200) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch the list of servers from SureView API with pagination support.

    Args:
        cookies: Authentication cookies from login
        page_size: Number of servers per page (default: 200)

    Returns:
        List of server dicts if successful, None otherwise
    """
    if not cookies:
        logger.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}

    all_servers = []
    page = 1

    try:
        while True:
            url = f"https://us.sureviewops.com/api/servers/GetServerList?PageSize={page_size}&Page={page}"
            logger.info(f"Fetching server list page {page} from SureView API (PageSize={page_size})...")

            response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)

            if response.status_code == 200:
                data = response.json()
                servers = data.get("data", [])
                total_count = data.get("totalCount", 0)

                logger.info(f"Page {page}: Retrieved {len(servers)} servers (Total available: {total_count})")

                if not servers:
                    logger.info("No more servers to fetch, pagination complete.")
                    break

                all_servers.extend(servers)

                # Check if we've fetched all servers
                if len(all_servers) >= total_count:
                    logger.info(f"All servers fetched: {len(all_servers)}/{total_count}")
                    break

                page += 1
            else:
                logger.error(f"API request failed. Status Code: {response.status_code}")
                logger.error(f"Response: {response.text}")
                return None if page == 1 else all_servers  # Return partial results if not first page

        logger.info(f"Total servers fetched across all pages: {len(all_servers)}")
        return all_servers

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed on page {page}: {e}")
        return None if page == 1 else all_servers  # Return partial results if not first page


def get_devices_by_server_id(
    cookies: List[Dict[str, str]],
    server_id: int
) -> Optional[List[Dict[str, Any]]]:
    """
    Get devices by server ID from SureView API.

    Args:
        cookies: Authentication cookies
        server_id: Server identifier

    Returns:
        List of device dicts if successful, None otherwise
    """
    if not cookies:
        logger.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = f"https://us.sureviewops.com/api/devices/GetByServerId?serverId={server_id}"

    try:
        logger.info(f"Fetching devices for server ID {server_id}...")
        response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            devices = response.json()
            device_count = len(devices) if devices else 0
            logger.info(f"Server {server_id}: Retrieved {device_count} devices")
            return devices
        else:
            logger.error(f"Server {server_id}: API request failed. Status Code: {response.status_code}")
            logger.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Server {server_id}: API request failed: {e}")
        return None


def get_group_details(
    cookies: List[Dict[str, str]],
    group_id: int
) -> Optional[Dict[str, Any]]:
    """
    Get group details by group ID from SureView API.

    This fetches additional site information including:
    - referenceId (customer_id)
    - address
    - telephone, telephone2
    - telephonePolice, telephoneFire
    - notes
    - latLong

    Args:
        cookies: Authentication cookies
        group_id: SureView group identifier

    Returns:
        Group details dict if successful, None otherwise
    """
    if not cookies:
        logger.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = f"https://us.sureviewops.com/api/groups/{group_id}"
    params = {"liveData": "false"}

    try:
        logger.info(f"Fetching group details for group ID {group_id} from SureView API...")
        response = requests.get(url, headers=headers, cookies=cookie_dict, params=params, timeout=30)

        if response.status_code == 200:
            logger.info(f"Group details for {group_id} retrieved successfully.")
            return response.json()
        else:
            logger.error(f"API request failed. Status Code: {response.status_code}")
            logger.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None


def sync_sureview_devices(db: Session) -> Dict[str, int]:
    """
    Sync devices from SureView API to database.

    This function:
    1. Authenticates to SureView via Selenium
    2. Fetches server list (a SureView server == one of our Devices)
    3. Fetches devices for each server (a SureView device == one of our Cameras)
    4. Updates database with devices and cameras
    5. Removes stale entries not present in SureView

    Parent Site handling (AC-20/21/25): a brand-new Device also gets a fresh 1:1
    parent Site carrying the SureView location data. A Device that already has a
    parent Site keeps it untouched — neither its name nor any of its 8 location
    fields are rewritten, so operator-consolidated multi-NVR Sites survive the
    10-minute beat.

    Args:
        db: Database session

    Returns:
        Summary dict with counts:
            {
                "devices_updated": int,
                "cameras_updated": int,
                "devices_removed": int,
                "cameras_removed": int,
                "errors": int
            }
    """
    results = {
        "devices_updated": 0,
        "cameras_updated": 0,
        "devices_removed": 0,
        "cameras_removed": 0,
        # Parent Sites left with no devices after a prune. Sync never deletes
        # a Site; these are surfaced for manual review.
        "sites_left_childless": 0,
        "errors": 0
    }

    try:
        # Step 1: Login and get cookies
        logger.info("Starting SureView sync...")
        cookies = automate_login()

        if not cookies:
            logger.error("Failed to authenticate to SureView")
            results["errors"] += 1
            return results

        # Step 2: Get server list with pagination
        servers = get_server_list(cookies)

        if not servers:
            logger.error("Failed to fetch server list")
            results["errors"] += 1
            return results

        logger.info(f"=== SYNC SUMMARY: Fetched {len(servers)} servers from SureView ===")

        # Track current devices and cameras from SureView
        current_device_ids = set()
        current_camera_ids = set()
        cameras_per_server = {}

        # Step 3: Process each server
        for idx, server in enumerate(servers, 1):
            logger.info(f"Processing server {idx}/{len(servers)}: {server.get('title', 'Unknown')} (ID: {server.get('serverID')})")
            try:
                device_id = str(server["serverID"])
                current_device_ids.add(device_id)

                # Fetch group details for this server to get additional location
                # information (only ever used when minting a NEW parent Site).
                group_data = None
                if "groupID" in server and server["groupID"]:
                    group_data = get_group_details(cookies, server["groupID"])

                # Update or create device
                device_record = db.query(Device).filter(
                    Device.id == device_id
                ).first()

                if device_record:
                    # Update the Device's OWN fields only.
                    device_record.name = server["title"]
                    device_record.nvr_username = server["username"]
                    device_record.nvr_password = server["password"]
                    device_record.sureview_site = True

                    # AC-25: an already-parented Device keeps its parent Site
                    # exactly as the operator left it — name and all 8 location
                    # fields are NOT rewritten from group_data. Only heal the
                    # pathological case of a Device with no parent at all.
                    if not device_record.site_id:
                        logger.warning(
                            f"Device {device_id} has no parent site; "
                            f"minting one"
                        )
                        parent_site = create_parent_site(
                            db, server["title"], group_data
                        )
                        device_record.site_id = parent_site.id
                else:
                    # Brand-new Device: mint its 1:1 parent Site as well.
                    parent_site = create_parent_site(
                        db, server["title"], group_data
                    )
                    device_record = Device(
                        id=device_id,
                        name=server["title"],
                        site_id=parent_site.id,
                        nvr_username=server["username"],
                        nvr_password=server["password"],
                        sureview_site=True,
                        new=True
                    )
                    db.add(device_record)

                # Flush to database to avoid bulk insert conflicts
                db.flush()

                results["devices_updated"] += 1

                # Step 4: Get SureView devices (== our cameras) for this server
                sureview_devices = get_devices_by_server_id(
                    cookies, server["serverID"]
                ) or []
                device_count = len(sureview_devices)
                cameras_per_server[device_id] = device_count

                if device_count == 0:
                    logger.warning(f"Server {device_id} ({server.get('title')}) has 0 devices")

                for device in sureview_devices:
                    try:
                        camera_id = str(device["deviceID"])
                        current_camera_ids.add(camera_id)

                        # Build RTSP URL
                        rtsp_url = (
                            f"rtsp://{server['username']}:{server['password']}@"
                            f"{server['host']}:{server['port']}/"
                            f"{server['extraValue'].replace('{#}', str(device['input1']))}"
                        )

                        # Update or create camera using merge for proper upsert
                        camera = db.query(Camera).filter(Camera.id == camera_id).first()

                        if camera:
                            # Update existing
                            camera.name = device["title"]
                            camera.rtsp_url = rtsp_url
                            camera.sureview_camera = True
                        else:
                            # Create new
                            camera = Camera(
                                id=camera_id,
                                device_id=device_id,
                                name=device["title"],
                                rtsp_url=rtsp_url,
                                sureview_camera=True,
                                new=True
                            )
                            db.add(camera)

                        # Flush to database to avoid bulk insert conflicts
                        db.flush()

                        results["cameras_updated"] += 1

                    except Exception as e:
                        logger.error(f"Error processing device {device.get('deviceID')}: {e}")
                        results["errors"] += 1

            except Exception as e:
                logger.error(f"Error processing server {server.get('serverID')}: {e}")
                results["errors"] += 1

        # Log comprehensive sync statistics
        logger.info("=" * 80)
        logger.info(f"=== SYNC STATISTICS ===")
        logger.info(f"Total servers processed: {len(servers)}")
        logger.info(f"Total devices updated: {results['devices_updated']}")
        logger.info(f"Total cameras from SureView API: {len(current_camera_ids)}")
        logger.info(f"Total cameras processed: {results['cameras_updated']}")
        logger.info(f"Errors encountered: {results['errors']}")

        # Log servers with no cameras (potential issues)
        empty_servers = [
            dev_id for dev_id, count in cameras_per_server.items() if count == 0
        ]
        if empty_servers:
            logger.warning(f"Servers with 0 cameras: {len(empty_servers)} servers")
            logger.warning(f"Empty server IDs: {empty_servers[:10]}{'...' if len(empty_servers) > 10 else ''}")

        # Log top 10 servers by camera count
        if cameras_per_server:
            sorted_servers = sorted(cameras_per_server.items(), key=lambda x: x[1], reverse=True)
            logger.info("Top 10 servers by camera count:")
            for dev_id, count in sorted_servers[:10]:
                logger.info(f"  - Server {dev_id}: {count} cameras")

        logger.info("=" * 80)

        # Commit all updates
        db.commit()

        # Step 5: Remove stale devices and cameras (bulk delete for performance)
        # CRITICAL: Only perform deletion if sync was successful and fetched data
        # This prevents deleting cameras when API partially fails or returns incomplete data
        logger.info("=" * 80)
        logger.info("=== STALE ENTRY CLEANUP ===")

        sync_successful = results["errors"] == 0
        has_data = len(current_camera_ids) > 0

        logger.info(f"Sync successful: {sync_successful}")
        logger.info(f"Has data from API: {has_data} ({len(current_camera_ids)} cameras)")

        if not sync_successful:
            logger.warning(
                f"Skipping deletion of stale entries due to errors during sync. "
                f"Errors: {results['errors']}"
            )
        elif not has_data:
            logger.warning(
                "Skipping deletion of stale entries - no cameras fetched from SureView. "
                "This may indicate API issues or authentication failure."
            )
        else:
            # Get all SureView device IDs
            existing_sureview_device_ids = {
                device.id for device in db.query(Device.id).filter(
                    Device.sureview_site == True
                ).all()
            }

            logger.info(f"Existing SureView devices in DB: {len(existing_sureview_device_ids)}")
            logger.info(f"Current devices from API: {len(current_device_ids)}")

            # Find stale devices
            stale_device_ids = existing_sureview_device_ids - current_device_ids

            if stale_device_ids:
                logger.info(
                    f"Removing {len(stale_device_ids)} stale devices: "
                    f"{list(stale_device_ids)[:5]}"
                    f"{'...' if len(stale_device_ids) > 5 else ''}"
                )

                # Capture the parent sites BEFORE deleting the devices so we
                # can report which ones are left childless.
                candidate_parent_site_ids = {
                    row.site_id for row in db.query(Device.site_id).filter(
                        Device.id.in_(stale_device_ids)
                    ).all() if row.site_id
                }

                db.query(Device).filter(
                    Device.id.in_(stale_device_ids)
                ).delete(synchronize_session=False)
                results["devices_removed"] = len(stale_device_ids)
                db.flush()

                # Sync NEVER deletes a Site.
                #
                # A Site is a place. It carries the address, phone numbers and
                # notes an operator typed in, and it may have been curated to
                # hold several NVRs. SureView knows nothing about any of that,
                # so a server disappearing from its inventory — whether that is
                # a genuine decommission or a partial API response — must not
                # destroy the record of the location.
                #
                # This mirrors the rule applied on the update path, where an
                # already-parented Site is never rewritten from group_data.
                # Protecting a Site's fields from being overwritten while
                # allowing the same sync to delete it outright would be
                # incoherent.
                #
                # Childless Sites are reported for a human to review and remove
                # through DELETE /api/v1/sites/{id}.
                childless_site_ids = {
                    parent_id for parent_id in candidate_parent_site_ids
                    if db.query(func.count(Device.id)).filter(
                        Device.site_id == parent_id
                    ).scalar() == 0
                }

                results["sites_left_childless"] = len(childless_site_ids)
                if childless_site_ids:
                    logger.info(
                        f"{len(childless_site_ids)} parent site(s) now have no "
                        f"devices and were RETAINED for manual review: "
                        f"{sorted(childless_site_ids)[:5]}"
                        f"{'...' if len(childless_site_ids) > 5 else ''}"
                    )
            else:
                logger.info("No stale devices to remove")

            # Get all SureView camera IDs
            existing_sureview_camera_ids = {
                camera.id for camera in db.query(Camera.id).filter(
                    Camera.sureview_camera == True
                ).all()
            }

            logger.info(f"Existing SureView cameras in DB: {len(existing_sureview_camera_ids)}")
            logger.info(f"Current cameras from API: {len(current_camera_ids)}")

            # Find stale cameras
            stale_camera_ids = existing_sureview_camera_ids - current_camera_ids

            if stale_camera_ids:
                # Additional safety check: warn if deleting more than 50% of cameras
                deletion_percentage = (len(stale_camera_ids) / len(existing_sureview_camera_ids)) * 100
                logger.info(f"Stale cameras identified: {len(stale_camera_ids)} ({deletion_percentage:.1f}% of total)")
                logger.info(f"Sample stale camera IDs: {list(stale_camera_ids)[:10]}{'...' if len(stale_camera_ids) > 10 else ''}")

                if deletion_percentage > 50:
                    logger.error(
                        f"SAFETY CHECK FAILED: Attempting to delete {len(stale_camera_ids)} cameras "
                        f"({deletion_percentage:.1f}% of total). This likely indicates a sync issue. "
                        f"Skipping deletion to prevent data loss."
                    )
                    results["errors"] += 1
                else:
                    logger.info(f"Removing {len(stale_camera_ids)} stale cameras")
                    db.query(Camera).filter(Camera.id.in_(stale_camera_ids)).delete(synchronize_session=False)
                    results["cameras_removed"] = len(stale_camera_ids)
                    logger.info(f"Successfully removed {len(stale_camera_ids)} stale cameras")
            else:
                logger.info("No stale cameras to remove")

        # Commit deletions (if any were performed)
        db.commit()

        # Final summary with complete database state
        logger.info("=" * 80)
        logger.info("=== SYNC COMPLETED ===")
        logger.info(f"Devices updated: {results['devices_updated']}")
        logger.info(f"Devices removed: {results['devices_removed']}")
        logger.info(f"Cameras updated: {results['cameras_updated']}")
        logger.info(f"Cameras removed: {results['cameras_removed']}")
        logger.info(f"Errors: {results['errors']}")

        # Query final database state
        final_device_count = db.query(func.count(Device.id)).filter(
            Device.sureview_site == True
        ).scalar()
        final_camera_count = db.query(func.count(Camera.id)).filter(Camera.sureview_camera == True).scalar()

        logger.info(f"Final database state:")
        logger.info(f"  - SureView devices in DB: {final_device_count}")
        logger.info(f"  - SureView cameras in DB: {final_camera_count}")
        logger.info("=" * 80)

        return results

    except Exception as e:
        logger.error(f"Error in sync_sureview_devices: {e}")
        db.rollback()
        results["errors"] += 1
        return results
