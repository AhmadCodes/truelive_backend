# main.py
import os
from dataclasses import dataclass
import streamlit as st

@dataclass
class Config:
    DB_PATH = os.getenv('DB_PATH', 'config.db')
    STREAM_APP_WS_URL = os.getenv('STREAM_APP_WS_URL', 'ws://localhost:8765')

# Initialize session state
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = True

def login():
    st.sidebar.title("Login")
    username = st.sidebar.text_input("Username")
    password = st.sidebar.text_input("Password", type="password")
    if st.sidebar.button("Login"):
        if username == "admin" and password == "password":
            st.session_state.logged_in = True
            st.sidebar.success("Logged in successfully")
            st.rerun()
        else:
            st.sidebar.error("Invalid credentials")

def main_app():
    st.set_page_config(
        page_title="Live View Camera Configuration System",
        page_icon="🎥",
        layout="wide"
    )
    
    st.title("Camera Configuration System")
    st.write("""
    This SQL application manages camera configurations and viewing layouts through SQLite database.
    Use the sidebar to navigate between different sections:
    - Sites: Manage your site locations and NVR credentials
    - Cameras: Configure cameras and their RTSP streams
    - PCs: Manage viewing stations and their capabilities
    - Screen Layout: Configure viewing layouts and communicate with streaming application
    """)

if st.session_state.logged_in:
    main_app()
else:
    login()