# pages/1_Sites.py
import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config, save_camera_config
import uuid
import time
def sites_page():
    st.set_page_config(page_title="Site Management", page_icon="🎥", layout="wide")
    
    st.title("Site Management")

    # Load config
    config = load_camera_config()
    if "edit_site_id" not in st.session_state:
        st.session_state["edit_site_id"] = None
    if "view_site_id" not in st.session_state:
        st.session_state["view_site_id"] = None
    if "edit_camera_site_id" not in st.session_state:
        st.session_state["edit_camera_site_id"] = None
    if "edit_camera_id" not in st.session_state:
        st.session_state["edit_camera_id"] = None

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

    # Place the "Add New Site" button on the right side
    btn_cols = st.columns([9, 1])
    with btn_cols[1]:
        add_site_clicked = st.button("Add New Site", key="add_site_btn", help="Add a new site")

    # ----- Add Site Modal -----
    add_site_modal = Modal(key="add_site_modal", title="Add New Site")
    if add_site_clicked:
        add_site_modal.open()

    if add_site_modal.is_open():
        with add_site_modal.container():
            st.subheader("Add New Site")
            site_name = st.text_input("Site Name", key="new_site_name")
            nvr_username = st.text_input("NVR Username", key="new_site_nvr_username")
            nvr_password = st.text_input("NVR Password", type="password", key="new_site_nvr_password")
            if st.button("Submit", key="submit_new_site"):
                if site_name and nvr_username and nvr_password:
                    site_id = "SITE_" + str(uuid.uuid4())
                    config["sites"][site_id] = {
                        "name": site_name,
                        "nvr_username": nvr_username,
                        "nvr_password": nvr_password,
                        "cameras": {}
                    }
                    save_camera_config(config)
                    st.success(f"Added site: {site_name}")
                    time.sleep(0.5)
                    add_site_modal.close()
                    st.rerun()
                else:
                    st.error("Please fill in all fields.")

    # ----- View Site Modal -----
    view_site_modal = Modal(key="view_site_modal", title="Site Details")
    config = load_camera_config()
    if view_site_modal.is_open() and st.session_state["view_site_id"] in config["sites"]:
        sid = st.session_state["view_site_id"]
        
        config = load_camera_config()
        site_data = config["sites"][sid]
        with view_site_modal.container():
            st.subheader(site_data["name"])
            st.write(f"**NVR Username:** {site_data['nvr_username']}")
            st.write(f"**NVR Password:** {site_data['nvr_password']}")
            
            # Button to add cameras
            add_cam_clicked = st.button("Add Camera", key="add_cam_btn")
            
            st.markdown("### Cameras")
            cam_header = st.columns([3, 3, 2])
            cam_header[0].markdown("**Camera Name**")
            cam_header[1].markdown("**RTSP URL**")
            cam_header[2].markdown("**Actions**")
            
            for cam_id, cam_info in site_data.get("cameras", {}).items():
                row_cam = st.columns([3, 3, 2])
                row_cam[0].write(cam_info["name"])                
                row_cam[1].write(cam_info["rtsp_url"])
                with row_cam[2]:
                    edit_cam_col, del_cam_col = st.columns(2)
                    with edit_cam_col:
                        if st.button("✏️", key=f"edit_cam_{cam_id}"):
                            st.session_state["edit_camera_site_id"] = sid
                            st.session_state["edit_camera_id"] = cam_id
                            edit_camera_modal.open()
                    with del_cam_col:
                        if st.button("🗑️", key=f"delete_cam_{cam_id}"):
                            config = load_camera_config()
                            config["sites"][sid]["cameras"].pop(cam_id)
                            save_camera_config(config)
                            st.success("Camera deleted")
                            time.sleep(0.5)
                            view_site_modal.close()
                            st.rerun()

            # ----- Add Camera Modal -----
            add_camera_modal = Modal(key="add_camera_modal", title="Add Camera")
            if add_cam_clicked:
                add_camera_modal.open()
            config = load_camera_config()
            site_data = config["sites"][sid]
            if add_camera_modal.is_open():
                with add_camera_modal.container():
                    st.subheader("Add Camera")
                    new_cam_name = st.text_input("Camera Name", key="new_cam_name")
                    new_cam_rtsp = st.text_input("RTSP URL", key="new_cam_rtsp")
                    config = load_camera_config()
                    site_data = config["sites"][sid]
                    if st.button("Save New Camera", key="save_new_cam"):
                        if new_cam_name and new_cam_rtsp:
                            new_cam_id = "CAM_" + str(uuid.uuid4())
                            site_data["cameras"][new_cam_id] = {
                                "name": new_cam_name,
                                "rtsp_url": new_cam_rtsp
                            }
                            save_camera_config(config)
                            st.success("New camera added")
                            time.sleep(0.5)
                            add_camera_modal.close()
                            view_site_modal.close()
                            st.rerun()
                        else:
                            st.error("Please fill in all fields.")

    # ----- Edit Site Modal -----
    edit_site_modal = Modal(key="edit_site_modal", title="Edit Site")
    config = load_camera_config()
    if edit_site_modal.is_open() and st.session_state["edit_site_id"] in config["sites"]:
        site_id = st.session_state["edit_site_id"]
        config = load_camera_config()
        site_info = config["sites"][site_id]
        with edit_site_modal.container():
            st.subheader(f"Edit: {site_info['name']}")
            new_name = st.text_input("Site Name", value=site_info["name"], key="edit_site_name")
            new_nvr_username = st.text_input("NVR Username", value=site_info["nvr_username"], key="edit_site_nvr_username")
            new_nvr_password = st.text_input("NVR Password", value=site_info["nvr_password"], type="password", key="edit_site_nvr_password")
            if st.button("Save Changes", key="save_site_changes"):
                if new_name and new_nvr_username and new_nvr_password:
                    site_info["name"] = new_name
                    site_info["nvr_username"] = new_nvr_username
                    site_info["nvr_password"] = new_nvr_password
                    save_camera_config(config)
                    st.success("Changes saved")
                    time.sleep(0.5)
                    edit_site_modal.close()
                    st.rerun()
                else:
                    st.error("Please fill in all fields.")

    # ----- Edit Camera Modal -----
    edit_camera_modal = Modal(key="edit_camera_modal", title="Edit Camera")
    if edit_camera_modal.is_open():
        site_id = st.session_state.get("edit_camera_site_id")
        cam_id = st.session_state.get("edit_camera_id")
        config = load_camera_config()
        if site_id and cam_id and site_id in config["sites"]:
            config = load_camera_config()
            site_info = config["sites"][site_id]
            cam_info = site_info["cameras"].get(cam_id, {})
            with edit_camera_modal.container():
                st.subheader(f"Edit Camera: {cam_info.get('name', '')}")
                cam_name = st.text_input("Camera Name", value=cam_info.get("name", ""), key="edit_cam_name")
                cam_rtsp = st.text_input("RTSP URL", value=cam_info.get("rtsp_url", ""), key="edit_cam_rtsp")
                if st.button("Save Camera", key="save_cam_changes"):
                    if cam_name and cam_rtsp:
                        cam_info["name"] = cam_name
                        cam_info["rtsp_url"] = cam_rtsp
                        save_camera_config(config)
                        st.success("Camera changes saved")
                        time.sleep(0.5)
                        edit_camera_modal.close()
                        st.rerun()
                    else:
                        st.error("Please fill in all fields.")

    # ----- Display Sites -----
    config = load_camera_config()
    if config["sites"]:
        st.markdown("### All Sites")
        header_cols = st.columns([3, 2, 3])
        header_cols[0].markdown("**Site Name**")
        header_cols[1].markdown("**No. of Cameras**")
        header_cols[2].markdown("**Actions**")
        config = load_camera_config()
        for sid, info in config["sites"].items():
            row_cols = st.columns([3, 2, 3])
            with row_cols[0]:
                view_clicked = st.button(info["name"], key=f"view_{sid}", help="View Site Details")
            row_cols[1].write(len(info["cameras"]))
            with row_cols[2]:
                icon_edit_col, icon_delete_col = st.columns(2)
                with icon_edit_col:
                    edit_clicked = st.button("✏️", key=f"edit_{sid}", help="Edit Site")
                with icon_delete_col:
                    delete_clicked = st.button("🗑️", key=f"delete_{sid}", help="Delete Site")

            if view_clicked:
                st.session_state["view_site_id"] = sid
                view_site_modal.open()

            if edit_clicked:
                st.session_state["edit_site_id"] = sid
                edit_site_modal.open()

            if delete_clicked:
                config = load_camera_config()
                config["sites"].pop(sid)
                save_camera_config(config)
                st.success("Site deleted")
                time.sleep(0.5)
                st.rerun()

sites_page()