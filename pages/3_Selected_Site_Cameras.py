import streamlit as st
import logging
import time
import uuid
from typing import Dict, List, Any, Optional, Tuple
from PIL import Image
import cv2
import numpy as np

from database import Database, SiteCamerasLayoutConfig, SiteCamerasLayout
from utils.logging_utils import setup_logging
from streamlit_modal import Modal
from utils.config_loader import load_camera_config

# Set up logging
logger = setup_logging(logging.DEBUG)

st.set_page_config(
            page_title="Site Cameras Layout Configuration",
            page_icon="assets/Logomark.png",
            layout="wide",
            initial_sidebar_state="auto",
        )

# Display logo
st.logo(
    "assets/Horizontal-Logo.png", 
    size="large",
    icon_image="assets/Logomark.png"
)

@st.cache_resource
def get_db_instance() -> Database:
    try:
        return Database()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        st.error("Database connection failed. Please check your configuration.")
        return None

def validate_camera_config(config: Dict[str, Any]) -> bool:
    """Validate camera configuration structure"""
    if not isinstance(config, dict):
        logger.error("Camera config must be a dictionary")
        return False
    if "sites" not in config:
        logger.error("Camera config must contain 'sites' key")
        return False
    if not isinstance(config["sites"], dict):
        logger.error("Sites must be a dictionary")
        return False
    return True

def get_site_cameras(camera_config: Dict[str, Any], site_id: str) -> List[Dict[str, Any]]:
    """Get cameras for a specific site with validation"""
    try:
        if not site_id or site_id not in camera_config.get("sites", {}):
            logger.error(f"Site {site_id} not found in camera config")
            return []

        site_info = camera_config["sites"][site_id]
        if not isinstance(site_info, dict) or "cameras" not in site_info:
            logger.error(f"Invalid site info structure for site {site_id}")
            return []

        cameras = []
        for camera_id, camera_info in site_info["cameras"].items():
            if not isinstance(camera_info, dict):
                logger.warning(f"Invalid camera info for camera {camera_id}")
                continue

            cameras.append({
                "camera_id": camera_id,
                "name": camera_info.get("name", f"Camera {camera_id}"),
                "rtsp_url": camera_info.get("rtsp_url", ""),
                "site_id": site_id
            })

        return cameras
    except Exception as e:
        logger.error(f"Error in get_site_cameras: {e}")
        return []

def convert_cv2_to_pil(cv2_img):
    """Convert a CV2 image to PIL format for Streamlit display"""
    if cv2_img is None:
        return None
    
    try:
        # Convert from BGR to RGB
        rgb_img = cv2.cvtColor(cv2_img, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb_img)
    except Exception as e:
        logger.error(f"Error converting image: {e}")
        return None

def create_empty_layout(rows: int, cols: int) -> Dict[str, Dict[str, Any]]:
    """Create an empty grid layout"""
    layout = {}
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            slot_name = f"slot_{row}_{col}"
            layout[slot_name] = None
    return layout

def check_user_permission(required_role=None):
    """
    Check if the current user has the required role.
    If required_role is None, just check if the user is logged in.
    """
    if 'user_id' not in st.session_state or not st.session_state['user_id']:
        st.warning("Please log in to access this page")
        return False
    
    if required_role is None:
        return True
    
    user_role = st.session_state.get('user_role', '')
    if user_role == required_role or user_role == 'super_admin':
        return True
    else:
        if required_role == 'admin' and user_role != 'user':
            return True
        if user_role != 'super_admin':
            st.error("You don't have permission to access this feature")
            return False
    
    return True

def site_cameras_page():
    try:
        # Check if user is logged in
        if not check_user_permission():
            st.stop()
        
        st.title("Site Cameras Layout Configuration")
        
        
        # Initialize camera modal
        camera_modal = Modal(key="camera_select_modal", title="Select Camera")
        
        # Initialize database
        db = get_db_instance()
        if not db:
            st.error("Failed to initialize database. Please refresh the page.")
            return
        
        # Load camera configuration
        try:
            camera_config = load_camera_config()
            if not validate_camera_config(camera_config):
                st.error("Invalid camera configuration. Please check your settings.")
                return
        except Exception as e:
            logger.error(f"Failed to load camera config: {e}")
            import traceback
            traceback.print_exc()
            st.error("Failed to load camera configuration. Please check your settings.")
            return
        
        # Initialize session states
        session_states = {
            "selected_site": None,
            "current_layout_config": None,
            "edit_slot": None,
            "open_camera_modal": False,
            "last_edited_slot": None,
            "modal_camera_index": 0,
            "layout_changed": False,
            "layout_rows": 2,
            "layout_cols": 2,
        }
        
        for key, default_value in session_states.items():
            if key not in st.session_state:
                st.session_state[key] = default_value
        
        # Site selection
        site_options = [(site_id, site_info["name"]) 
                        for site_id, site_info in camera_config["sites"].items()]
        
        if not site_options:
            st.error("No sites found in the configuration.")
            return
        
        col1, col2 = st.columns([3, 1])
        with col1:
            selected_site_index = st.selectbox(
                "Select Site",
                range(len(site_options)),
                format_func=lambda x: site_options[x][1],
                key="site_selector"
            )
            
            site_id, site_name = site_options[selected_site_index]
            st.session_state.selected_site = site_id
        
        with col2:
            if st.button("Refresh", use_container_width=True):
                st.rerun()
        
        # Layout configuration options
        if st.session_state.selected_site:
            st.subheader("Layout Configuration")
            
            # Get existing layout config if available
            layout_config = db.get_site_cameras_layout_config(st.session_state.selected_site)
            
            # Layout dimensions with sliders
            col1, col2 = st.columns(2)
            with col1:
                layout_rows = st.slider(
                    "Number of Rows", 
                    min_value=1, 
                    max_value=4, 
                    value=layout_config.n_rows if layout_config else st.session_state.layout_rows,
                    key="rows_slider"
                )
            
            with col2:
                layout_cols = st.slider(
                    "Number of Columns", 
                    min_value=1, 
                    max_value=4, 
                    value=layout_config.n_cols if layout_config else st.session_state.layout_cols,
                    key="cols_slider"
                )
            
            # Update session state
            if layout_rows != st.session_state.layout_rows or layout_cols != st.session_state.layout_cols:
                st.session_state.layout_rows = layout_rows
                st.session_state.layout_cols = layout_cols
                st.session_state.layout_changed = True
            
            # Save configuration button
            if st.button("Save Layout Configuration", type="primary"):
                try:
                    # Create or update layout config
                    config = SiteCamerasLayoutConfig(
                        site_id=st.session_state.selected_site,
                        site_name=site_name,
                        n_rows=layout_rows,
                        n_cols=layout_cols
                    )
                    
                    # Save to database
                    if db.add_site_cameras_layout_config(config):
                        # If rows or columns were reduced, remove the extra slots
                        if layout_config:
                            # Clean up slots outside new dimensions
                            existing_layout = db.get_site_cameras_layout(st.session_state.selected_site)
                            for slot in existing_layout:
                                if slot.slot_row > layout_rows or slot.slot_col > layout_cols:
                                    db.delete_site_cameras_layout(
                                        slot.site_id, slot.slot_row, slot.slot_col
                                    )
                        
                        st.success("Layout configuration saved successfully!")
                        st.session_state.layout_changed = False
                        time.sleep(1)
                        st.rerun()
                    else:
                        st.error("Failed to save layout configuration.")
                except Exception as e:
                    logger.error(f"Error saving layout config: {e}")
                    st.error(f"Error: {str(e)}")
            
            # Show current layout
            st.subheader("Camera Layout")
            
            # Get the current layout configuration
            current_layout = db.get_site_cameras_layout(st.session_state.selected_site)
            
            # Create a mapping for easy lookup
            layout_mapping = {}
            for item in current_layout:
                slot_key = f"slot_{item.slot_row}_{item.slot_col}"
                layout_mapping[slot_key] = item
            
            # Create grid layout
            for row in range(1, layout_rows + 1):
                cols = st.columns(layout_cols)
                for col in range(1, layout_cols + 1):
                    with cols[col - 1]:
                        slot_name = f"slot_{row}_{col}"
                        slot_data = layout_mapping.get(slot_name)
                        
                        with st.container():
                            st.markdown("----------------")
                            
                            if slot_data:
                                camera_id = slot_data.camera_id
                                
                                # Get camera details from config
                                camera_name = "Unknown Camera"
                                for camera in get_site_cameras(camera_config, st.session_state.selected_site):
                                    if camera["camera_id"] == camera_id:
                                        camera_name = camera["name"]
                                        break
                                
                                st.markdown(f"### Slot {row}-{col}")
                                
                                # Display screenshot for the camera if available
                                screenshot = db.get_screenshot(camera_id)
                                if screenshot is not None:
                                    # Convert CV2 image to format suitable for Streamlit
                                    pil_image = convert_cv2_to_pil(screenshot)
                                    if pil_image:
                                        st.image(pil_image, caption=f"Camera: {camera_name}", use_container_width=True)
                                else:
                                    st.markdown(f"Camera: {camera_name}")
                                    st.info("📷 Screenshot not available. It will be captured and available within a few minutes.")
                            else:
                                st.markdown(f"### Slot {row}-{col}\n\nEmpty")
                            
                            # Button to select camera for this slot
                            if st.button("Select Camera", key=f"select_{slot_name}"):
                                st.session_state.edit_slot = slot_name
                                st.session_state.open_camera_modal = True
                                st.rerun()
        
        # Camera Selection Modal
        if st.session_state.open_camera_modal and st.session_state.edit_slot:
            st.session_state.open_camera_modal = False  # Reset the flag
            st.session_state.last_edited_slot = st.session_state.edit_slot  # Remember which slot we're editing
            st.session_state.modal_camera_index = 0
            modal_key = f"camera_modal_{st.session_state.edit_slot}"
            camera_modal = Modal(key=modal_key, title="Select Camera")
            camera_modal.open()
        else:
            modal_key = f"camera_modal_{st.session_state.last_edited_slot}" if st.session_state.last_edited_slot else "camera_modal_default"
            camera_modal = Modal(key=modal_key, title="Select Camera")
        
        # Handle the modal content if it's open
        if camera_modal.is_open():
            with camera_modal.container():
                try:
                    logger.debug(f"Modal open for slot: {st.session_state.edit_slot}")
                    
                    # Get cameras for selected site
                    cameras = get_site_cameras(camera_config, st.session_state.selected_site)
                    
                    if not cameras:
                        st.warning(f"No cameras available for this site")
                        if st.button("Close", key="close_no_cameras"):
                            camera_modal.close()
                            st.session_state.edit_slot = None
                        return
                    
                    # Get a stable unique ID for this modal instance
                    modal_id = st.session_state.last_edited_slot if st.session_state.last_edited_slot else "default"
                    
                    # Ensure camera index is valid for the current camera list
                    camera_index = min(st.session_state.modal_camera_index, len(cameras) - 1)
                    
                    # Camera selection
                    selected_camera_index = st.selectbox(
                        "Select Camera",
                        range(len(cameras)),
                        index=camera_index,
                        format_func=lambda x: cameras[x]["name"],
                        key=f"camera_select_{modal_id}"
                    )
                    
                    # Update the stored camera index
                    st.session_state.modal_camera_index = selected_camera_index
                    
                    selected_camera = cameras[selected_camera_index]
                    
                    # Display selected camera details
                    st.info(f"Selected Camera: {selected_camera['name']}")
                    st.info(f"RTSP URL: {selected_camera['rtsp_url']}")
                    
                    # Display camera screenshot if available
                    camera_id = selected_camera["camera_id"]
                    screenshot = db.get_screenshot(camera_id)
                    if screenshot is not None:
                        # Convert CV2 image to format suitable for Streamlit
                        pil_image = convert_cv2_to_pil(screenshot)
                        if pil_image:
                            st.image(pil_image, caption=f"Camera Preview: {selected_camera['name']}", use_container_width=True)
                    else:
                        st.info("📷 Screenshot not available. It will be captured and available within a few minutes.")
                    
                    # Action buttons
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("Confirm", use_container_width=True, key=f"confirm_{modal_id}"):
                            try:
                                # Only proceed if we still have a valid edit_slot
                                if not st.session_state.edit_slot:
                                    st.error("Selection slot lost. Please try again.")
                                    camera_modal.close()
                                    return
                                
                                row, col = map(int, st.session_state.edit_slot.split("_")[1:])
                                
                                # Create layout item
                                layout_item = SiteCamerasLayout(
                                    site_id=st.session_state.selected_site,
                                    site_name=site_options[selected_site_index][1],
                                    slot_row=row,
                                    slot_col=col,
                                    camera_id=selected_camera["camera_id"]
                                )
                                
                                # Save to database
                                db.add_site_cameras_layout(layout_item)
                                
                                # Clear edit slot AFTER processing
                                camera_modal.close()
                                st.session_state.edit_slot = None
                                st.rerun()
                            except Exception as e:
                                logger.error(f"Failed to add camera to layout: {e}")
                                st.error("Failed to save camera configuration.")
                    
                    with col2:
                        if st.button("Clear Slot", use_container_width=True, key=f"clear_{modal_id}"):
                            try:
                                # Only proceed if we still have a valid edit_slot
                                if not st.session_state.edit_slot:
                                    st.error("Selection slot lost. Please try again.")
                                    camera_modal.close()
                                    return
                                
                                row, col = map(int, st.session_state.edit_slot.split("_")[1:])
                                
                                # Delete from database
                                db.delete_site_cameras_layout(
                                    st.session_state.selected_site, row, col
                                )
                                
                                # Clear edit slot AFTER processing
                                camera_modal.close()
                                st.session_state.edit_slot = None
                                st.rerun()
                            except Exception as e:
                                logger.error(f"Failed to clear slot: {e}")
                                st.error("Failed to clear camera slot.")
                
                except Exception as e:
                    logger.error(f"Error in camera selection modal: {e}")
                    st.error("An error occurred while selecting a camera.")
                    camera_modal.close()
                    st.session_state.edit_slot = None
    
    except Exception as e:
        logger.error(f"Unexpected error in site_cameras_page: {e}")
        st.error("An unexpected error occurred. Please try refreshing the page.")

if __name__ == "__main__":
    try:
        site_cameras_page()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        st.error("A fatal error occurred. Please contact support.")
        import traceback
        traceback.print_exc()
