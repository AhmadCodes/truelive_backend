import threading
import time
import streamlit as st
from utils.sureview_devices import run_in_background
from utils.screenshot_utils import update_camera_screenshots

# Global thread object outside of Streamlit's session state
_background_thread = None
_last_run_time = 0
_interval = 600  # 10 minutes in seconds

def initialize_background_task():
    """Initialize background task tracking in session state"""
    if 'background_task_initialized' not in st.session_state:
        st.session_state['background_task_initialized'] = True
        st.session_state['last_fetch_time'] = 0
    
    # Start the background check on every page load
    check_and_start_background_task()

def background_worker():
    """Function that runs in a separate thread"""
    global _background_thread, _last_run_time
    
    try:
        # Run the function for SureView devices
        run_in_background()
        
        # Also update camera screenshots
        update_camera_screenshots()
        
        # Update the last run time
        _last_run_time = time.time()
        st.session_state['last_fetch_time'] = _last_run_time
    finally:
        # Reset the thread when done
        _background_thread = None

def check_and_start_background_task():
    """Check if it's time to start a background task and start it if needed"""
    global _background_thread, _last_run_time, _interval
    
    current_time = time.time()
    
    # Only start a new task if:
    # 1. Enough time has passed since the last run
    # 2. No background thread is currently running
    if (current_time - _last_run_time >= _interval and _background_thread is None):
        _background_thread = threading.Thread(target=background_worker, daemon=True)
        _background_thread.start()

def get_background_status():
    """Return the status of the background task for display purposes"""
    global _background_thread, _last_run_time
    
    is_running = _background_thread is not None
    last_run_time_str = time.strftime('%H:%M:%S', time.localtime(_last_run_time)) if _last_run_time > 0 else "Never"
    
    return {
        "is_running": is_running,
        "last_run_time": last_run_time_str
    }