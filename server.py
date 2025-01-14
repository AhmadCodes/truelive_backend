import streamlit.web.bootstrap
import os
import sys
import json
import threading
import webbrowser
import logging
import socket
import time
from pathlib import Path
from http.client import HTTPConnection

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def get_application_path():
    """Get the correct application path whether running as script or frozen exe"""
    if getattr(sys, 'frozen', False):
        # If the application is run as a bundle, the PyInstaller bootloader
        # extends the sys module by a flag frozen=True and sets the app 
        # path into variable _MEIPASS'.
        return Path(os.path.dirname(sys.executable))
    else:
        return Path(os.path.dirname(os.path.abspath(__file__)))

def is_server_running(port):
    """Check if Streamlit server is already running"""
    try:
        with HTTPConnection("localhost", port, timeout=1) as conn:
            conn.request("HEAD", "/")
            return conn.getresponse().status == 200
    except:
        return False

def verify_config_files(app_path):
    """Verify all required files exist"""
    required_files = ['site_config.json', 'camera_config.json', 'main.py']
    missing_files = []
    
    for file in required_files:
        if not (app_path / file).exists():
            missing_files.append(file)
    
    if missing_files:
        logger.error(f"Missing required files: {', '.join(missing_files)}")
        return False
    return True

def run_streamlit(app_path):
    """Run the Streamlit application"""
    try:
        # Change to the application directory
        os.chdir(app_path)
        
        # Set up the Streamlit command
        main_script = str(app_path / "main.py")
        sys.argv = ["streamlit", "run", main_script]
        
        # Run Streamlit
        streamlit.web.bootstrap.run(main_script, '', [], [])
    except Exception as e:
        logger.error(f"Error running Streamlit: {str(e)}")
        raise

def main():
    try:
        # Get the application path
        app_path = get_application_path()
        port = 8501

        # Check if server is already running
        if is_server_running(port):
            logger.info("Server already running, opening browser")
            webbrowser.open(f'http://localhost:{port}')
            return

        # Verify files
        if not verify_config_files(app_path):
            input("Press Enter to exit...")
            return

        # Start server in a separate thread
        logger.info("Starting Streamlit server...")
        server_thread = threading.Thread(
            target=run_streamlit,
            args=(app_path,)
        )
        server_thread.daemon = True
        server_thread.start()

        # Wait for server to start
        retries = 0
        while not is_server_running(port) and retries < 30:
            time.sleep(0.5)
            retries += 1

        if retries >= 30:
            logger.error("Server failed to start")
            input("Press Enter to exit...")
            return

        # Open browser
        logger.info("Opening browser...")
        webbrowser.open(f'http://localhost:{port}')

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        input("Press Enter to exit...")

if __name__ == '__main__':
    main()