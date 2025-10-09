"""
SureView integration service.

Syncs camera and site data from SureView API using Selenium for authentication.
"""
import os
import time
import logging
import requests
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from app.core.config import settings
from app.models.site import Site
from app.models.camera import Camera

logger = logging.getLogger(__name__)


def is_docker() -> bool:
    """
    Check if running inside a Docker container.

    Returns:
        True if running in Docker, False otherwise
    """
    return os.getenv('AM_I_IN_A_DOCKER_CONTAINER', 'False') == 'True'


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
            service = Service(ChromeDriverManager().install())
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


def get_server_list(cookies: List[Dict[str, str]]) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch the list of servers from SureView API.

    Args:
        cookies: Authentication cookies from login

    Returns:
        List of server dicts if successful, None otherwise
    """
    if not cookies:
        logger.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = "https://us.sureviewops.com/api/servers/GetServerList?PageSize=200"

    try:
        logger.info("Fetching server list from SureView API...")
        response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logger.info("Server list retrieved successfully.")
            return response.json().get("data", [])
        else:
            logger.error(f"API request failed. Status Code: {response.status_code}")
            logger.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None


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
        logger.info(f"Fetching devices by server ID {server_id} from SureView API...")
        response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logger.info("Device data retrieved successfully.")
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
    2. Fetches server list
    3. Fetches devices for each server
    4. Updates database with sites and cameras
    5. Removes stale entries not present in SureView

    Args:
        db: Database session

    Returns:
        Summary dict with counts:
            {
                "sites_updated": int,
                "cameras_updated": int,
                "sites_removed": int,
                "cameras_removed": int,
                "errors": int
            }
    """
    results = {
        "sites_updated": 0,
        "cameras_updated": 0,
        "sites_removed": 0,
        "cameras_removed": 0,
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

        # Step 2: Get server list
        servers = get_server_list(cookies)

        if not servers:
            logger.error("Failed to fetch server list")
            results["errors"] += 1
            return results

        # Track current sites and cameras from SureView
        current_site_ids = set()
        current_camera_ids = set()

        # Step 3: Process each server
        for server in servers:
            try:
                site_id = str(server["serverID"])
                current_site_ids.add(site_id)

                # Update or create site
                site = db.query(Site).filter(Site.id == site_id).first()

                if site:
                    # Update existing
                    site.name = server["title"]
                    site.nvr_username = server["username"]
                    site.nvr_password = server["password"]
                    site.sureview_site = True
                else:
                    # Create new
                    site = Site(
                        id=site_id,
                        name=server["title"],
                        nvr_username=server["username"],
                        nvr_password=server["password"],
                        sureview_site=True,
                        new=True
                    )
                    db.add(site)

                results["sites_updated"] += 1

                # Step 4: Get devices for this server
                devices = get_devices_by_server_id(cookies, server["serverID"]) or []

                for device in devices:
                    try:
                        # Only process devices that belong to this server
                        if device["serverID"] != server["serverID"]:
                            continue

                        camera_id = str(device["deviceID"])
                        current_camera_ids.add(camera_id)

                        # Build RTSP URL
                        rtsp_url = (
                            f"rtsp://{server['username']}:{server['password']}@"
                            f"{server['host']}:{server['port']}/"
                            f"{server['extraValue'].replace('{#}', str(device['input1']))}"
                        )

                        # Update or create camera
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
                                site_id=site_id,
                                name=device["title"],
                                rtsp_url=rtsp_url,
                                sureview_camera=True,
                                new=True
                            )
                            db.add(camera)

                        results["cameras_updated"] += 1

                    except Exception as e:
                        logger.error(f"Error processing device {device.get('deviceID')}: {e}")
                        results["errors"] += 1

            except Exception as e:
                logger.error(f"Error processing server {server.get('serverID')}: {e}")
                results["errors"] += 1

        # Commit all updates
        db.commit()

        # Step 5: Remove stale sites and cameras (bulk delete for performance)
        # Get all SureView site IDs
        existing_sureview_site_ids = {
            site.id for site in db.query(Site.id).filter(Site.sureview_site == True).all()
        }

        # Find stale sites
        stale_site_ids = existing_sureview_site_ids - current_site_ids

        if stale_site_ids:
            logger.info(f"Removing {len(stale_site_ids)} stale sites")
            db.query(Site).filter(Site.id.in_(stale_site_ids)).delete(synchronize_session=False)
            results["sites_removed"] = len(stale_site_ids)

        # Get all SureView camera IDs
        existing_sureview_camera_ids = {
            camera.id for camera in db.query(Camera.id).filter(
                Camera.sureview_camera == True
            ).all()
        }

        # Find stale cameras
        stale_camera_ids = existing_sureview_camera_ids - current_camera_ids

        if stale_camera_ids:
            logger.info(f"Removing {len(stale_camera_ids)} stale cameras")
            db.query(Camera).filter(Camera.id.in_(stale_camera_ids)).delete(synchronize_session=False)
            results["cameras_removed"] = len(stale_camera_ids)

        # Commit deletions
        db.commit()

        logger.info(f"SureView sync completed: {results}")
        return results

    except Exception as e:
        logger.error(f"Error in sync_sureview_devices: {e}")
        db.rollback()
        results["errors"] += 1
        return results
