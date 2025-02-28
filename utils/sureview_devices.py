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
from dotenv import load_dotenv
import sys
try:
    from database import Database, Site, Camera
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import Database, Site, Camera

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Get credentials from .env
USERNAME = os.getenv("SUREVIEW_USERNAME")
PASSWORD = os.getenv("SUREVIEW_PASSWORD")

# Validate environment variables
if not USERNAME or not PASSWORD:
    logging.error("Username or password not set in .env file!")
    exit(1)

# Set up WebDriver options for headless execution
chrome_options = Options()
chrome_options.add_argument("--headless")
chrome_options.add_argument("--disable-extensions")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--log-level=3")

db = Database()

def automate_login():
    """Automates login and retrieves authentication cookies with retry logic."""
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 30)

    try:
        logging.info("Opening SureView login page...")
        driver.get("https://suite.sureviewops.com/login")
        
        logging.info("Entering username...")
        email_field = wait.until(EC.presence_of_element_located((By.ID, "username")))
        email_field.send_keys(USERNAME)

        next_button = driver.find_element(By.ID, "next")
        next_button.click()

        logging.info("Waiting for password field...")
        password_field = wait.until(EC.presence_of_element_located((By.ID, "password")))
        password_field.send_keys(PASSWORD)

        submit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[type="submit"]')))
        submit_button.click()

        logging.info("Navigating to US SureView dashboard...")
        grid_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn.dropdown-toggle.btn-secondary")))
        grid_button.click()

        response_us = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(@href, 'us.sureviewops.com')]")))
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
        response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)
        
        if response.status_code == 200:
            logging.info("Server list retrieved successfully.")
            return response.json().get("data", [])
        else:
            logging.error(f"API request failed. Status Code: {response.status_code}")
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
        logging.info(f"Fetching devices of type {device_type_id} from SureView API...")
        response = requests.get(url, headers=headers, cookies=cookie_dict, timeout=30)

        if response.status_code == 200:
            logging.info("Device data retrieved successfully.")
            return response.json()
        else:
            logging.error(f"API request failed. Status Code: {response.status_code}")
            logging.error(f"Response: {response.text}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
        return None

def save_to_database(servers, devices):
    """Saves sites and cameras to the database in a thread-safe manner."""
    with sqlite3.connect(db.db_path) as conn:
        cursor = conn.cursor()
        
        for server in servers:
            site = Site(
                id=str(server["serverID"]),
                name=server["title"],
                nvr_username=server["username"],
                nvr_password=server["password"],
                sureview_site=True,
                new=True
            )
            cursor.execute("INSERT OR IGNORE INTO sites VALUES (?, ?, ?, ?, ?, ?)", (site.id, site.name, site.nvr_username, site.nvr_password, site.sureview_site, site.new))
        
        for device in devices:
            rtsp_url = f"rtsp://{server['username']}:{server['password']}@{server['host']}:{server['port']}/{server['extraValue'].replace('{#}', str(device['input1']))}"
            camera = Camera(
                id=str(device["deviceID"]),
                site_id=str(device["serverID"]),
                name=device["title"],
                rtsp_url=rtsp_url,
                main_stream_url=None,
                sureview_camera=True,
                new=True
            )
            cursor.execute("INSERT OR IGNORE INTO cameras VALUES (?, ?, ?, ?, ?, ?, ?)", (camera.id, camera.site_id, camera.name, camera.rtsp_url, camera.main_stream_url, camera.sureview_camera, camera.new))
        
        conn.commit()

def run_in_background():
    """Runs the login and API requests in a separate thread."""
    def task():
        cookies = automate_login()
        if cookies:
            servers = get_server_list(cookies)
            devices = get_devices_by_type(cookies)
            if servers and devices:
                save_to_database(servers, devices)
                logging.info("Data saved to database successfully.")

    thread = threading.Thread(target=task, daemon=True)
    thread.start()
    logging.info("Script is running in the background...")

if __name__ == "__main__":
    run_in_background()
    time.sleep(10)
    logging.info("Main process continues running...")
