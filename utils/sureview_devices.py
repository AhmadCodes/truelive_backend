

# %%
from collections import defaultdict
from functools import partial
import os
import time
import logging
import requests
import threading
import sqlite3
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv
import sys
import os
try:
    from database import Database, Site, Camera
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    from database import Database, Site, Camera
# try:
#     from rtsp_checker import check_rtsp_stream
# except ImportError:
#     sys.path.append(os.path.dirname(os.path.abspath(__file__)))
#     sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#     from rtsp_checker import check_rtsp_stream

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# Get credentials from .env
USERNAME = os.getenv("SUREVIEW_USERNAME")
PASSWORD = os.getenv("SUREVIEW_PASSWORD")

# Validate environment variables
if not USERNAME or not PASSWORD:
    logging.error("Username or password not set in .env file!")
    exit(1)

# Set up WebDriver options for headless execution
chrome_options = Options()
chrome_options.add_argument("--disable-extensions")
chrome_options.add_argument("--log-level=3")
chrome_options.add_argument("--disable-software-rasterizer")
chrome_options.add_argument("--headless")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--remote-debugging-port=9222")


def is_docker():
    if os.getenv('AM_I_IN_A_DOCKER_CONTAINER') == 'True':
        logging.info("Running in Docker container.")
        return True
    else:
        logging.info("Running locally.")
        return False


# Initialize the database
db = Database()


def automate_login():
    """Automates login and retrieves authentication cookies with retry logic."""
    if is_docker():
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 30)

    try:
        logging.info("Opening SureView login page...")
        driver.get("https://suite.sureviewops.com/login")

        logging.info("Entering username...")
        email_field = wait.until(
            EC.presence_of_element_located((By.ID, "username")))
        email_field.send_keys(USERNAME)

        next_button = driver.find_element(By.ID, "next")
        next_button.click()

        logging.info("Waiting for password field...")
        password_field = wait.until(
            EC.presence_of_element_located((By.ID, "password")))
        password_field.send_keys(PASSWORD)

        submit_button = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, 'button[type="submit"]')))
        submit_button.click()

        logging.info("Navigating to US SureView dashboard...")
        grid_button = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".btn.dropdown-toggle.btn-secondary")))
        grid_button.click()

        response_us = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(@href, 'us.sureviewops.com')]")))
        response_us.click()

        driver.switch_to.window(driver.window_handles[-1])
        wait.until(EC.presence_of_element_located((By.ID, "menuBarParent")))

        cookies = driver.get_cookies()
        logging.info("Login successful, cookies retrieved.")
        return cookies

    except Exception as e:
        logging.error(f"Error during login: {e}")
        return None

    finally:
        driver.quit()
        logging.info("WebDriver closed.")


def get_server_list(cookies):
    """Fetches the list of servers from SureView API."""
    if not cookies:
        logging.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = "https://us.sureviewops.com/api/servers/GetServerList?PageSize=200"

    try:
        logging.info("Fetching server list from SureView API...")
        response = requests.get(url, headers=headers,
                                cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logging.info("Server list retrieved successfully.")
            return response.json().get("data", [])
        else:
            logging.error(
                f"API request failed. Status Code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
        return None


def get_devices_by_type(cookies, device_type_id=1):
    """Makes an API request to get devices by type using authentication cookies."""
    if not cookies:
        logging.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = f"https://us.sureviewops.com/api/Devices/GetDevicesByType/{device_type_id}"

    try:
        logging.info(
            f"Fetching devices of type {device_type_id} from SureView API...")
        response = requests.get(url, headers=headers,
                                cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logging.info("Device data retrieved successfully.")
            return response.json()
        else:
            logging.error(
                f"API request failed. Status Code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
        return None


# https://us.sureviewops.com/api/devices/GetByServerId?serverId=10207
def get_devices_by_server_id(cookies, server_id):
    """Makes an API request to get devices by server ID using authentication cookies."""
    if not cookies:
        logging.error("No cookies available, aborting request.")
        return None

    cookie_dict = {cookie["name"]: cookie["value"] for cookie in cookies}
    headers = {"Accept": "application/json"}
    url = f"https://us.sureviewops.com/api/devices/GetByServerId?serverId={server_id}"

    try:
        logging.info(
            f"Fetching devices by server ID {server_id} from SureView API...")
        response = requests.get(url, headers=headers,
                                cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logging.info("Device data retrieved successfully.")
            return response.json()
        else:
            logging.error(
                f"API request failed. Status Code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
        return None


def save_to_database(servers, cookies):
    """Process servers and cameras with optimized database access to prevent locking."""
    # Create connection with appropriate settings to reduce locking
    with sqlite3.connect(db.db_path, timeout=30) as conn:
        # WAL mode reduces locking conflicts
        conn.execute("PRAGMA journal_mode = WAL")
        # These settings can help with concurrent access
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = 10000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 30000000000")
        conn.isolation_level = None  # Use autocommit mode
        cursor = conn.cursor()

        try:
            # Begin transaction manually for better control
            cursor.execute("BEGIN TRANSACTION")

            # Load existing data efficiently with one query
            cursor.execute("SELECT id, name FROM sites")
            existing_sites = {row[0]: row[1] for row in cursor.fetchall()}

            cursor.execute("SELECT id, name FROM cameras")
            existing_cameras = {row[0]: (row[1])
                                for row in cursor.fetchall()}

            # Process servers and collect devices
            all_devices = []
            new_site_ids = set()
            current_cameras = set()

            # Batch site updates for a single operation
            site_batch = []

            # Process servers first and prepare batched operations
            for server in servers:
                site_id = str(server["serverID"])
                new_site_ids.add(site_id)

                # Add to site batch instead of immediate insert
                site_batch.append((
                    site_id,
                    server["title"],
                    server["username"],
                    server["password"]
                ))

                # Collect devices for batch processing
                devices = get_devices_by_server_id(
                    cookies, server["serverID"]) or []
                for device in devices:
                    if device["serverID"] != server["serverID"]:
                        continue

                    camera_id = str(device["deviceID"])
                    rtsp_url = f"rtsp://{server['username']}:{server['password']}@{server['host']}:{server['port']}/{server['extraValue'].replace('{#}', str(device['input1']))}"
                    all_devices.append((server, device, rtsp_url, camera_id))
                    current_cameras.add(camera_id)

            # Bulk insert sites with a single query
            if site_batch:
                cursor.executemany("""
                    INSERT INTO sites (id, name, nvr_username, nvr_password) 
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        nvr_username = excluded.nvr_username,
                        nvr_password = excluded.nvr_password
                """, site_batch)

            

            # Commit the site changes before starting parallel work
            cursor.execute("COMMIT")

            # Begin a new transaction for camera updates
            with sqlite3.connect(db.db_path) as conn2:
                conn2.execute("PRAGMA journal_mode = WAL")
                cursor2 = conn2.cursor()
                cursor2.execute("BEGIN TRANSACTION")

                # Prepare camera batch for bulk update
                camera_batch = []
                for server, device, rtsp_url, camera_id in all_devices:
                    site_id = str(server["serverID"])
                    # Prepare camera data
                    camera_batch.append((
                        camera_id,
                        site_id,
                        device["title"],
                        rtsp_url,
                        1,  # sureview_camera
                        1 if camera_id not in existing_cameras else 0  # new
                    ))

                # Bulk camera updates in a single query
                if camera_batch:
                    cursor2.executemany("""
                        INSERT INTO cameras 
                        (id, site_id, name, rtsp_url, sureview_camera, new)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name,
                            rtsp_url = excluded.rtsp_url,
                            new = excluded.new
                    """, camera_batch)

                # Cleanup obsolete entries - use efficient IN queries
                stale_sites = set(existing_sites.keys()) - new_site_ids
                if stale_sites:
                    # Delete with parametrized query instead of multiple single deletes
                    placeholders = ','.join(['?'] * len(stale_sites))
                    cursor2.execute(f"DELETE FROM sites WHERE id IN ({placeholders})",
                                    list(stale_sites))

                stale_cameras = set(existing_cameras.keys()) - current_cameras
                if stale_cameras:
                    placeholders = ','.join(['?'] * len(stale_cameras))
                    cursor2.execute(f"DELETE FROM cameras WHERE id IN ({placeholders})",
                                    list(stale_cameras))
                # Commit all changes
                cursor2.execute("COMMIT")

        except Exception as e:
            # Ensure we rollback on error
            cursor.execute("ROLLBACK")
            logging.error(f"Database operation failed: {str(e)}")
            import traceback
            traceback.print_exc()
            logging.info("Database operation failed: %s", str(e))


def task():
    cookies = automate_login()
    if cookies:
        servers = get_server_list(cookies)
        if servers:
            save_to_database(servers, cookies=cookies)
            logging.info("Data saved to database successfully.")


def run_in_background():
    """Runs the login and API requests in a separate thread."""

    thread = threading.Thread(target=task, daemon=True)
    thread.start()
    logging.info("Script is running in the background...")


if __name__ == "__main__":
    task()
    time.sleep(10)
    logging.info("Main process continues running...")


# %%
