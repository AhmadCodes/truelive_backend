# pages/2_Cameras.py
import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config, save_camera_config
import uuid
import cv2
import numpy as np
from threading import Thread
import queue
from utils.url_processor import encode_rtsp_password
import time
from utils.background_task import initialize_background_task, get_background_status

# Initialize the background task system
initialize_background_task()

def check_user_permission(required_role=None):
    """
    Check if the current user has the required role.
    If required_role is None, just check if the user is logged in.
    """
    if 'user_id' not in st.session_state or not st.session_state['user_id']:
        st.warning("You must be logged in to access this page")
        st.stop()
    
    if required_role is None:
        return True
    
    user_role = st.session_state.get('user_role', '')
    
    if required_role == 'admin':
        if user_role not in ['admin', 'super_admin']:
            st.error("You don't have permission to access this feature")
            return False
    elif required_role == 'super_admin':
        if user_role != 'super_admin':
            st.error("You don't have permission to access this feature")
            return False
    
    return True

def get_camera_snapshot(rtsp_url, result_queue):
    """Get a single frame from RTSP stream after 1 second buffer"""
    rtsp_url = encode_rtsp_password(rtsp_url)
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        result_queue.put(None)
        return

    # Buffer for 1 second
    start_time = time.time()
    while time.time() - start_time < 1:
        ret = cap.grab()
        if not ret:
            break

    # Get the last frame
    ret, frame = cap.retrieve()
    cap.release()

    if ret:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result_queue.put(frame)
    else:
        result_queue.put(None)

def cameras_page():
    st.set_page_config(page_title="Camera Management", page_icon="🎥", layout="wide")

    # Check if user is logged in
    if 'user_id' not in st.session_state or not st.session_state['user_id']:
        st.warning("Please log in to access this page")
        st.stop()
    
    user_role = st.session_state.get('user_role', '')
    is_read_only = user_role == 'user'

    st.title("Camera Management")
    
    if is_read_only:
        st.info("You have read-only access to camera information. Contact an administrator to make changes.")

    # Load config
    config = load_camera_config()
    
    # Initialize session state variables
    if "view_camera_id" not in st.session_state:
        st.session_state["view_camera_id"] = None
    if "CAMPAGE_edit_camera_id" not in st.session_state:
        st.session_state["CAMPAGE_edit_camera_id"] = None
    if "CAMPAGE_edit_camera_site_id" not in st.session_state:
        st.session_state["CAMPAGE_edit_camera_site_id"] = None
    if "selected_camera_rtsp" not in st.session_state:
        st.session_state["selected_camera_rtsp"] = None
    if "stream_modal_open" not in st.session_state:
        st.session_state["stream_modal_open"] = False
    if "snapshot_thread" not in st.session_state:
        st.session_state["snapshot_thread"] = None
    if "snapshot_queue" not in st.session_state:
        st.session_state["snapshot_queue"] = queue.Queue()

    # Custom CSS
    st.markdown(
        """
        <style>
        .big-blue-button > div > button {
            background-color: #007BFF;
            color: white;
            font-size: 1.1rem;
            padding: 0.75em 1.5em;
            border: none;
            border-radius: 0.25em;
            cursor: pointer;
            font-weight: bold;
        }
        .big-blue-button > div > button:hover {
            background-color: #0056b3;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Add Camera button (only for admin and super_admin)
    if not is_read_only:
        btn_cols = st.columns([9, 1])
        with btn_cols[1]:
            add_cam_clicked = st.button("Add Camera", key="add_cam_btn")
    else:
        add_cam_clicked = False

    # Add Camera Modal
    add_camera_modal = Modal(key="add_camera_modal", title="Add New Camera")
    if add_cam_clicked:
        add_camera_modal.open()

    if add_camera_modal.is_open():
        with add_camera_modal.container():
            st.subheader("Add New Camera")
            sites = list(config.get("sites", {}).keys())
            if sites:
                site_selected = st.selectbox(
                    "Select Site",
                    options=sites,
                    format_func=lambda x: config["sites"][x]["name"],
                )
                camera_name = st.text_input("Camera Name", key="new_cam_name")
                rtsp_url = st.text_input("RTSP URL", key="new_cam_rtsp")
                if st.button("Save Camera", key="save_cam_btn"):
                    if site_selected and camera_name and rtsp_url:
                        new_cam_id = "CAM_" + str(uuid.uuid4())
                        config["sites"][site_selected]["cameras"][new_cam_id] = {
                            "name": camera_name,
                            "rtsp_url": rtsp_url,
                        }
                        save_camera_config(config)
                        st.success(
                            f"Added camera: {camera_name} to site: {config['sites'][site_selected]['name']}"
                        )
                        time.sleep(0.5)
                        add_camera_modal.close()
                        st.rerun()
                    else:
                        st.error("Please fill in all fields.")
            else:
                st.warning("No sites available. Please add a site first.")

    # Edit Camera Modal
    edit_camera_modal = Modal(key="CAMPAGE_edit_camera_modal", title="Edit Camera")
    
    if st.session_state.get("CAMPAGE_edit_camera_id") and st.session_state.get("CAMPAGE_edit_camera_site_id"):
        site_id = st.session_state["CAMPAGE_edit_camera_site_id"]
        cam_id = st.session_state["CAMPAGE_edit_camera_id"]
        
        if cam_id in config["sites"][site_id]["cameras"]:
            cam_info = config["sites"][site_id]["cameras"][cam_id]
            with edit_camera_modal.container():
                st.subheader(f"Edit Camera: {cam_info['name']}")
                new_cam_name = st.text_input(
                    "Camera Name", value=cam_info["name"], key="edit_cam_name"
                )
                new_rtsp_url = st.text_input(
                    "RTSP URL", value=cam_info["rtsp_url"], key="edit_cam_rtsp"
                )
                if st.button("Save Changes", key="save_cam_changes"):
                    if new_cam_name and new_rtsp_url:
                        config["sites"][site_id]["cameras"][cam_id]["name"] = new_cam_name
                        config["sites"][site_id]["cameras"][cam_id]["rtsp_url"] = new_rtsp_url
                        save_camera_config(config)
                        st.success("Camera details updated")
                        time.sleep(0.5)
                        st.session_state["CAMPAGE_edit_camera_id"] = None
                        st.session_state["CAMPAGE_edit_camera_site_id"] = None
                        edit_camera_modal.close()
                        st.rerun()
                    else:
                        st.error("Please fill in all fields.")

    # Display Cameras
    st.markdown("### Existing Cameras")
    if config.get("sites"):
        for site_id, site_info in config["sites"].items():
            with st.expander(site_info["name"], expanded=False):
                cameras = site_info.get("cameras", {})
                if cameras:
                    cam_header = st.columns([3, 3, 2])
                    cam_header[0].markdown("**Camera Name**")
                    cam_header[1].markdown("**RTSP URL**")
                    cam_header[2].markdown("**Actions**")

                    for cam_id, cam_info in cameras.items():
                        row_cam = st.columns([3, 3, 2])
                        with row_cam[0]:
                            if st.button(cam_info["name"], key=f"view_cam_{cam_id}_{site_id}"):
                                st.session_state["selected_camera_rtsp"] = cam_info["rtsp_url"]
                                st.session_state["stream_modal_open"] = True
                                # Clear previous queue
                                st.session_state["snapshot_queue"] = queue.Queue()
                                # Start new snapshot thread
                                st.session_state["snapshot_thread"] = Thread(
                                    target=get_camera_snapshot,
                                    args=(cam_info["rtsp_url"], st.session_state["snapshot_queue"])
                                )
                                st.session_state["snapshot_thread"].start()
                                st.rerun()
                        
                        with row_cam[1]:
                            st.write(cam_info["rtsp_url"])
                        
                        # Show edit/delete buttons only for admin and super_admin
                        if not is_read_only:
                            with row_cam[2]:
                                col_edit, col_delete = st.columns([1, 1], gap="small")
                                with col_edit:
                                    if st.button("✏️", key=f"CAMPAGE_edit_cam_{cam_id}_{site_id}"):
                                        st.session_state["CAMPAGE_edit_camera_id"] = cam_id
                                        st.session_state["CAMPAGE_edit_camera_site_id"] = site_id
                                        edit_camera_modal.open()
                                
                                with col_delete:
                                    if st.button("🗑️", key=f"delete_cam_{cam_id}_{site_id}"):
                                        config["sites"][site_id]["cameras"].pop(cam_id)
                                        save_camera_config(config)
                                        st.success("Camera deleted")
                                        time.sleep(0.5)
                                        st.rerun()
                else:
                    st.info("No cameras available for this site.")

    # Snapshot Modal
    stream_modal = Modal(key="stream_modal", title="Camera Snapshot")
    
    if st.session_state["stream_modal_open"]:
        with stream_modal.container():
            st.subheader("Camera Snapshot")
            
            # Create placeholder for image
            image_placeholder = st.empty()
            
            loading_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            loading_frame = cv2.putText(
                loading_frame,
                "Loading...",
                (200, 240),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            
            # Show loading message
            image_placeholder.image(
                loading_frame,
                caption="Receiving Snapshot...",
                use_container_width=True
            )

            # Check if thread is running and hasn't exceeded timeout
            if st.session_state.get("snapshot_thread"):
                if st.session_state["snapshot_thread"].is_alive():
                    # Thread is still running, keep showing loading message
                    time.sleep(0.1)  # Small delay to prevent UI freeze
                    st.rerun()
                else:
                    # Thread finished, get the result
                    try:
                        frame = st.session_state["snapshot_queue"].get_nowait()
                        if frame is not None:
                            image_placeholder.image(
                                frame,
                                caption="Camera Snapshot",
                                use_container_width=True
                            )
                        else:
                            error_frame = np.zeros((480, 640, 3), dtype=np.uint8)
                            error_frame = cv2.putText(
                                error_frame,
                                "Failed to connect to camera",
                                (100, 240),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                1,
                                (255, 255, 255),
                                2,
                                cv2.LINE_AA,
                            )
                            image_placeholder.image(
                                error_frame,
                                caption="Connection Failed",
                                use_container_width=True
                            )
                    except queue.Empty:
                        image_placeholder.image(
                            loading_frame,
                            caption="No response from camera",
                            use_container_width=True
                        )
                    
                    # Clean up thread
                    st.session_state["snapshot_thread"] = None
            
            if st.button("Close", key="close_snapshot"):
                st.session_state["stream_modal_open"] = False
                st.session_state["selected_camera_rtsp"] = None
                st.session_state["snapshot_thread"] = None  # Clean up thread reference
                stream_modal.close()
                st.rerun()

cameras_page()