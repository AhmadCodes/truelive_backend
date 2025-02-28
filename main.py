# main.py
import os
from dataclasses import dataclass
import time
import threading
import streamlit as st
from utils.sureview_devices import run_in_background

# Initialize Streamlit app configuration 
st.set_page_config(
    page_title="Live View Camera Configuration System",
    page_icon="🎥",
    layout="wide"
)

# Initialize session state variables
if 'last_fetch_time' not in st.session_state:
    st.session_state['last_fetch_time'] = 0
if 'fetch_running' not in st.session_state:
    st.session_state['fetch_running'] = False
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = True

# Global variable to track if the background thread is already running
# This needs to be outside session_state as it's accessed by the background thread
fetch_thread = None

@dataclass
class Config:
    DB_PATH = os.getenv('DB_PATH', 'config.db')
    STREAM_APP_WS_URL = os.getenv('STREAM_APP_WS_URL', 'ws://localhost:8765')

def background_fetch():
    """Function to be run in a separate thread"""
    # Need to use a global variable since we can't reliably access session_state from a thread
    global fetch_thread
    
    try:
        # Actually run the fetch operation
        run_in_background()  # This is blocking
    finally:
        # Reset the thread when done
        fetch_thread = None

def login():
    st.sidebar.title("Login")
    username = st.sidebar.text_input("Username")
    password = st.sidebar.text_input("Password", type="password")
    if st.sidebar.button("Login"):
        if username == "admin" and password == "password":
            st.session_state['logged_in'] = True
            st.sidebar.success("Logged in successfully")
            st.rerun()
        else:
            st.sidebar.error("Invalid credentials")

def main_app():
    st.title("Camera Configuration System")
    
    # Check if it's time to run the background task
    global fetch_thread
    current_time = time.time()
    
    # Only start a new fetch if enough time has passed AND no fetch is already running
    if (current_time - st.session_state['last_fetch_time'] >= 120 and 
            fetch_thread is None):
        
        # Update the last fetch time
        st.session_state['last_fetch_time'] = current_time
        
        # Start the background thread
        fetch_thread = threading.Thread(target=background_fetch, daemon=True)
        fetch_thread.start()
    
    # Display status in the sidebar (remove in production if desired)
    st.sidebar.text(f"Last refresh: {time.strftime('%H:%M:%S', time.localtime(st.session_state['last_fetch_time']))}")
    st.sidebar.text(f"Fetch active: {'Yes' if fetch_thread else 'No'}")
    
    st.write("""
    This SQL application manages camera configurations and viewing layouts through SQLite database.
    Use the sidebar to navigate between different sections:
    - Sites: Manage your site locations and NVR credentials
    - Cameras: Configure cameras and their RTSP streams
    - PCs: Manage viewing stations and their capabilities
    - Screen Layout: Configure viewing layouts and communicate with streaming application
    """)

# Main application flow
if st.session_state['logged_in']:
    main_app()
else:
    login()