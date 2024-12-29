import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config, save_camera_config
import pandas as pd
import time
from streamlit_webrtc import webrtc_streamer, VideoProcessorBase, WebRtcMode
import cv2
import numpy as np
import uuid


# if 'logged_in' not in st.session_state or not st.session_state['logged_in']:
#     st.error("You need to log in first.")
#     st.stop()

# Define a simple VideoProcessor to handle RTSP streams
class RTSPVideoProcessor(VideoProcessorBase):
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url
        self.cap = cv2.VideoCapture(self.rtsp_url)
        if not self.cap.isOpened():
            st.error(f"Failed to connect to RTSP stream: {self.rtsp_url}")

    def recv(self):
        ret, frame = self.cap.read()
        if not ret:
            # Send a black frame with "Stream not available" text
            frame = np.zeros((480, 640, 3), dtype=np.uint8)  # Black frame
            frame = cv2.putText(
                frame,
                "Stream not available",
                (10, 240),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
            return frame
        # Convert the frame to RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return frame

def cameras_page():
    st.set_page_config(
        page_title="Camera Management",
        page_icon="🎥",
        layout="wide"
    )

    st.title("Camera Management")

    # Load config
    config = load_camera_config()
    if "view_camera_id" not in st.session_state:
        st.session_state["view_camera_id"] = None
    if "edit_camera_id" not in st.session_state:
        st.session_state["edit_camera_id"] = None
    if "selected_camera_rtsp" not in st.session_state:
        st.session_state["selected_camera_rtsp"] = None
    if "stream_modal_open" not in st.session_state:
        st.session_state["stream_modal_open"] = False

    # Custom CSS for the big blue button
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
        unsafe_allow_html=True
    )

    # Place the "Add Camera" button on the right side
    btn_cols = st.columns([9, 1])
    with btn_cols[1]:
        add_cam_clicked = st.button("Add Camera", key="add_cam_btn")

    # ----- Add Camera Modal -----
    add_camera_modal = Modal(key="add_camera_modal", title="Add New Camera")
    if add_cam_clicked:
        add_camera_modal.open()

    if add_camera_modal.is_open():
        with add_camera_modal.container():
            st.subheader("Add New Camera")
            sites = list(config.get('sites', {}).keys())
            if sites:
                site_selected = st.selectbox(
                    "Select Site",
                    options=sites,
                    format_func=lambda x: config['sites'][x]['name']
                )
                camera_name = st.text_input("Camera Name", key="new_cam_name")
                rtsp_url = st.text_input("RTSP URL", key="new_cam_rtsp")
                if st.button("Save Camera", key="save_cam_btn"):
                    if site_selected and camera_name and rtsp_url:
                        # new_cam_id = f"cam_{len(config['sites'][site_selected]['cameras']) + 1}"
                        new_cam_id = "CAM_" + str(uuid.uuid4())
                        config['sites'][site_selected]['cameras'][new_cam_id] = {
                            "name": camera_name,
                            "rtsp_url": rtsp_url
                        }
                        save_camera_config(config)
                        st.success(f"Added camera: {camera_name} to site: {config['sites'][site_selected]['name']}")
                        time.sleep(0.5)
                        add_camera_modal.close()
                        st.rerun()
                    else:
                        st.error("Please fill in all fields.")
            else:
                st.warning("No sites available. Please add a site first.")

    # ----- Edit Camera Modal -----
    edit_camera_modal = Modal(key="edit_camera_modal", title="Edit Camera")
    if "edit_camera_id" in st.session_state and st.session_state["edit_camera_id"]:
        cam_id = st.session_state["edit_camera_id"]
        # Find the site containing this camera
        for site_id, site_info in config['sites'].items():
            if cam_id in site_info['cameras']:
                cam_info = site_info['cameras'][cam_id]
                with edit_camera_modal.container():
                    st.subheader(f"Edit Camera: {cam_info['name']}")
                    new_cam_name = st.text_input("Camera Name", value=cam_info['name'], key="edit_cam_name")
                    new_rtsp_url = st.text_input("RTSP URL", value=cam_info['rtsp_url'], key="edit_cam_rtsp")
                    if st.button("Save Changes", key="save_cam_changes"):
                        if new_cam_name and new_rtsp_url:
                            config['sites'][site_id]['cameras'][cam_id]['name'] = new_cam_name
                            config['sites'][site_id]['cameras'][cam_id]['rtsp_url'] = new_rtsp_url
                            save_camera_config(config)
                            st.success("Camera details updated")
                            time.sleep(0.5)
                            edit_camera_modal.close()
                            st.session_state["edit_camera_id"] = None
                            st.rerun()
                        else:
                            st.error("Please fill in all fields.")
                break

    # ----- Display Cameras -----
    st.markdown("### Existing Cameras")
    if config.get('sites'):
        for site_id, site_info in config['sites'].items():
            with st.expander(site_info['name'], expanded=False):
                cameras = site_info.get('cameras', {})
                if cameras:
                    # Table header
                    cam_header = st.columns([3, 3, 2])
                    cam_header[0].markdown("**Camera Name**")
                    cam_header[1].markdown("**RTSP URL**")
                    cam_header[2].markdown("**Actions**")

                    for cam_id, cam_info in cameras.items():
                        row_cam = st.columns([3, 3, 2])
                        with row_cam[0]:
                            if st.button(cam_info['name'], key=f"view_cam_{cam_id}_{site_id}"):
                                st.session_state["selected_camera_rtsp"] = cam_info['rtsp_url']
                        with row_cam[1]:
                            st.write(cam_info['rtsp_url'])
                        with row_cam[2]:
                            col_edit, col_delete = st.columns([1, 1], gap="small")
                            with col_edit:
                                if st.button("✏️", key=f"edit_cam_{cam_id}_{site_id}"):
                                    st.session_state["edit_camera_id"] = cam_id
                                    edit_camera_modal.open()
                            with col_delete:
                                if st.button("🗑️", key=f"delete_cam_{cam_id}_{site_id}"):
                                    config['sites'][site_id]['cameras'].pop(cam_id)
                                    save_camera_config(config)
                                    st.success("Camera deleted")
                                    time.sleep(0.5)
                                    st.rerun()
                else:
                    st.info("No cameras available for this site.")

    # ----- Stream Live Camera Modal -----
    stream_modal = Modal(key="stream_modal", title="Live Stream")
    if st.session_state["stream_modal_open"]:
        stream_modal.open()

    if stream_modal.is_open():
        with stream_modal.container():
            st.subheader("Live Stream")
            webrtc_ctx = webrtc_streamer(
                key="live_stream",
                mode=WebRtcMode.SENDONLY,
                video_processor_factory=lambda: RTSPVideoProcessor(st.session_state["selected_camera_rtsp"]),
                async_processing=True,
                media_stream_constraints={"video": True, "audio": False},
            )
            if not webrtc_ctx.state.playing:
                st.warning("Failed to start the live stream.")
            else:
                st.success("Live stream started.")
                if st.button("Stop Stream", key="stop_stream"):
                    webrtc_ctx.stop()
                    st.session_state["selected_camera_rtsp"] = None
                    stream_modal.close()
                    st.session_state["stream_modal_open"] = False
                    st.rerun()


    # ----- Edit Camera Modal Handling -----
    if edit_camera_modal.is_open():
        # The modal content is handled above
        pass

cameras_page()