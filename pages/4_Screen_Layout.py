import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config
from utils.config_generator import generate_config
import time
from utils.websocket_client import send_config_sync
from database import Database, View, ScreenMapping, Screen, PC
from typing import Dict, List, Optional, Tuple, Any
from utils.logging_utils import setup_logging
import logging
from utils.background_task import initialize_background_task, get_background_status
import uuid
import jwt
import os
import json

# Initialize the background task system
initialize_background_task()

# Set up logging
logger = setup_logging(logging.DEBUG)


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


@st.cache_resource
def get_db_instance() -> Database:
    try:
        return Database()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        st.error("Database connection failed. Please check your configuration.")
        return None


def send_screen_mapping(config: Dict[str, Any], pc_id: str) -> bool:
    """Send screen mapping configuration with validation"""
    if not config:
        logger.error("Empty configuration provided")
        return False

    try:
        # Get the PC's auth token from the database
        db = get_db_instance()
        pc = db.get_pc_by_id(pc_id)
        if not pc or not hasattr(pc, 'auth_token') or not pc.auth_token:
            # Generate a JWT token if not available
            JWT_SECRET = os.getenv('JWT_SECRET', 'your-secret-key')
            auth_token = jwt.encode({
                'pc_id': pc_id,
                'name': pc.name if pc else 'Unknown',
                'exp': int(time.time()) + 86400  # 24 hour expiry
            }, JWT_SECRET, algorithm='HS256')
            
            logger.info(f"Generated new auth token for PC {pc_id}")
            
            # Save the token if the PC exists
            if pc:
                db.update_pc_token(pc_id, auth_token)
        else:
            auth_token = pc.auth_token
            logger.info(f"Using existing auth token for PC {pc_id}")

        # Ensure we have a valid configuration
        generated_config = generate_config(config)
        
        # Log the config being sent (truncated for brevity)
        config_str = json.dumps(generated_config)
        logger.info(f"Sending config to PC {pc_id} (length: {len(config_str)} chars)")
        logger.debug(f"Config first 500 chars: {config_str[:500]}...")
        
        # Send the configuration
        success = send_config_sync(generated_config, pc_id, auth_token)
        
        if success:
            logger.info(f"Successfully sent config to PC {pc_id}")
            # Update the last_applied timestamp in the database
            db.update_pc_last_applied(pc_id)
        else:
            logger.error(f"Failed to send config to PC {pc_id}")
            
        return success
    except Exception as e:
        logger.error(f"Failed to send screen mapping: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


def get_site_cameras(
    camera_config: Dict[str, Any], site_id: str
) -> List[Dict[str, str]]:
    """Get cameras for a specific site with validation"""
    if not camera_config or not isinstance(camera_config, dict):
        logger.error("Invalid camera configuration")
        return []

    site_info = camera_config.get("sites", {}).get(site_id, {})
    if not site_info:
        logger.warning(f"No site info found for site_id: {site_id}")
        return []

    cameras = []
    for cam_id, cam_info in site_info.get("cameras", {}).items():
        if not isinstance(cam_info, dict):
            logger.warning(f"Invalid camera info for camera {cam_id}")
            continue

        camera = {
            "camera_id": cam_id,
            "name": cam_info.get("name", f"Camera {cam_id}"),
            "rtsp_url": cam_info.get("rtsp_url", ""),
        }
        cameras.append(camera)

    return cameras


def validate_view_name(name: str) -> bool:
    """Validate view name"""
    if not name or not isinstance(name, str):
        return False
    if len(name) > 50:  # Arbitrary max length
        return False
    return True


def create_empty_view(rows: int, columns: int) -> Dict[str, None]:
    """Create an empty grid view with validation"""
    if not isinstance(rows, int) or not isinstance(columns, int):
        logger.error("Invalid row or column type")
        return {}

    if rows < 1 or columns < 1 or rows > 10 or columns > 10:  # Reasonable limits
        logger.error("Invalid row or column count")
        return {}

    return {
        f"slot_{row}_{col}": None
        for row in range(1, rows + 1)
        for col in range(1, columns + 1)
    }


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


def screen_layout_page():
    try:
        # st.set_page_config(page_title="Screen Layout", page_icon="��", layout="wide")

        # Check if user is logged in
        if 'user_id' not in st.session_state or not st.session_state['user_id']:
            st.warning("Please log in to access this page")
            st.stop()
        
        user_role = st.session_state.get('user_role', '')
        st.set_page_config(
            page_title="Screen Layout Configuration", layout="wide")
        st.title("Screen Layout Configuration")

        rename_modal = Modal(key="rename_modal", title="Rename View")
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
                st.error(
                    "Invalid camera configuration. Please check your settings.")
                return
        except Exception as e:
            logger.error(f"Failed to load camera config: {e}")
            import traceback
            traceback.print_exc()
            st.error(
                "Failed to load camera configuration. Please check your settings.")
            return

        # Initialize session states
        session_states = {
            "selected_pc": None,
            "selected_screen": None,
            "selected_view_id": None,
            "edit_slot": None,
            "selected_site": None,
            "current_view_config": None,
            "show_save_button": False,
            "editing_view_id": None,
            "open_camera_modal": False,      # For tracking modal open request
            "last_edited_slot": None,        # Track which slot was last edited
            "modal_site_index": 0,           # Track selected site in modal
            "modal_camera_index": 0,         # Track selected camera in modal
        }

        for key, default_value in session_states.items():
            if key not in st.session_state:
                st.session_state[key] = default_value

        with st.sidebar:
            st.header("Navigation")

            # Live View Configuration Button - MODIFIED: Removed view selection requirement
            if st.button("Configure Live View", type="primary"):
                # Check if PC and screen are selected
                if not st.session_state.selected_pc or not st.session_state.selected_screen:
                    st.warning("Please select a PC and screen first.")
                    return

                # Get PC configuration directly without requiring view selection
                try:
                    with st.spinner("Loading configuration..."):
                        pc_config = db.get_pc_config(st.session_state.selected_pc)
                        
                    if not pc_config:
                        st.error("PC configuration is empty. Please configure at least one view.")
                        return
                        
                    if not pc_config.get("mappings", {}).get("screen_to_cameras", {}).get(st.session_state.selected_pc, {}):
                        st.warning("No camera mappings found for this PC. Please configure at least one view.")
                        return
                        
                    # Show configuration details before sending
                    st.info("Preparing to send configuration to device...")
                    
                    # Display a progress bar with steps
                    progress_bar = st.progress(0)
                    status_text = st.empty()
                    
                    # Step 1: Preparing configuration
                    status_text.text("Step 1/3: Preparing configuration...")
                    time.sleep(0.5)
                    progress_bar.progress(33)
                    
                    # Step 2: Sending to device
                    status_text.text("Step 2/3: Sending to device...")
                    
                    # Send configuration
                    mapping_ret = send_screen_mapping(pc_config, st.session_state.selected_pc)
                    progress_bar.progress(66)
                    
                    # Step 3: Finalizing
                    status_text.text("Step 3/3: Finalizing...")
                    time.sleep(0.5)
                    progress_bar.progress(100)
                    
                    # Clear progress indicators
                    time.sleep(0.5)
                    progress_bar.empty()
                    status_text.empty()
                    
                    if mapping_ret:
                        success = st.success(
                            "✅ Live view configuration applied successfully!"
                        )
                        time.sleep(2)
                        success.empty()
                    else:
                        st.error("❌ Failed to apply live view configuration!")
                        st.info("Make sure the device is online and connected to the network.")
                        st.info("Check the logs for more details on the error.")
                except Exception as e:
                    logger.error(f"Error sending configuration: {e}")
                    st.error(f"An error occurred: {str(e)}")
                    st.info("Please check the logs for more details.")
                    import traceback
                    logger.error(traceback.format_exc())

            # PC Selection
            pcs = db.get_pcs()
            if not pcs:
                st.warning("No PCs configured in the system.")
                return
                
            # Display status indicator for PC
            if st.session_state.selected_pc:
                pc = db.get_pc_by_id(st.session_state.selected_pc)
                if pc and hasattr(pc, 'last_connected') and pc.last_connected:
                    # Check if PC is recently connected (within last 5 minutes)
                    if (time.time() - pc.last_connected) < 300:  # 5 minutes
                        st.success("✅ Device online")
                    else:
                        st.warning("⚠️ Device may be offline")
                else:
                    st.warning("⚠️ Connection status unknown")
                    
                # Display last applied timestamp
                if pc and hasattr(pc, 'last_applied') and pc.last_applied:
                    time_diff = int(time.time() - pc.last_applied)
                    if time_diff < 60:  # less than a minute
                        last_applied_str = f"{time_diff} seconds ago"
                    elif time_diff < 3600:  # less than an hour
                        last_applied_str = f"{time_diff // 60} minutes ago"
                    elif time_diff < 86400:  # less than a day
                        last_applied_str = f"{time_diff // 3600} hours ago"
                    else:
                        last_applied_str = f"{time_diff // 86400} days ago"
                    
                    st.info(f"Last config applied: {last_applied_str}")
                else:
                    st.info("No configuration has been applied yet")

            pc_options = [(pc.id, pc.name)
                          for pc in pcs if pc and pc.id and pc.name]
            if not pc_options:
                st.error("Invalid PC configurations found.")
                return

            selected_pc_index = st.selectbox(
                "Select PC",
                range(len(pc_options)),
                format_func=lambda x: f"🖥️ {pc_options[x][1]}",
                key="pc_selector",
            )

            current_pc_id, current_pc_name = pc_options[selected_pc_index]
            st.session_state.selected_pc = current_pc_id

            if st.session_state.selected_pc:
                screens = db.get_screens_by_pc(current_pc_id)
                if not screens:
                    st.warning("No screens configured for this PC.")
                    return

                screen_options = [
                    (screen.id, screen.name)
                    for screen in screens
                    if screen and screen.id and screen.name
                ]

                if not screen_options:
                    st.error("Invalid screen configurations found.")
                    return

                selected_screen_index = st.selectbox(
                    "Select Screen",
                    range(len(screen_options)),
                    format_func=lambda x: f"📺 {screen_options[x][1]}",
                    key="screen_selector",
                )
                st.session_state.selected_screen = screen_options[
                    selected_screen_index
                ][0]

                # View management
                if st.session_state.selected_screen:
                    st.markdown("### Views")
                    screen = db.get_screen_by_id(
                        st.session_state.selected_screen)
                    if not screen:
                        st.error("Selected screen not found.")
                        return

                    views = db.get_views_by_screen(
                        st.session_state.selected_screen)

                    # Handle view rename modal
                    if st.session_state.editing_view_id and rename_modal.is_open():
                        with rename_modal.container():
                            try:
                                view = db.get_view_by_id(
                                    st.session_state.editing_view_id
                                )
                                if not view:
                                    st.error("View not found")
                                    return

                                new_name = st.text_input(
                                    "New Name", value=view.name)

                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("Save", use_container_width=True):
                                        if not validate_view_name(new_name):
                                            st.error("Invalid view name")
                                            return

                                        if new_name != view.name:
                                            screen_id = st.session_state.selected_screen
                                            db.update_view_name(
                                                 new_name, view.id, screen_id=screen_id)
                                            st.rerun()
                                with col2:
                                    if st.button("Cancel", use_container_width=True):
                                        st.session_state.editing_view_id = None
                                        rename_modal.close()
                            except Exception as e:
                                logger.error(f"Error in rename modal: {e}")
                                st.error("Failed to rename view")
                                rename_modal.close()

                    # Add New View button
                    if st.button("➕ Add New View", type="primary"):
                        next_view_num = len(views) + 1
                        new_view_name = f"view_{next_view_num}"

                        if not (1 <= screen.rows <= 10 and 1 <= screen.columns <= 10):
                            st.error("Invalid screen dimensions.")
                            return
                        # unique 4 digit hex string
                        view_unique_id = uuid.uuid4().hex[:4]
                        new_view = View(
                            id=f"{st.session_state.selected_screen}_{view_unique_id}",
                            screen_id=st.session_state.selected_screen,
                            name=new_view_name,
                            layout_rows=screen.rows,
                            layout_columns=screen.columns,
                            view_number=next_view_num,
                        )

                        try:
                            db.add_view(new_view)
                            st.session_state.selected_view_id = new_view.id
                            st.rerun()
                        except Exception as e:
                            logger.error(f"Failed to add new view: {e}")
                            st.error("Failed to create new view.")
                            return

                    # Display existing views
                    st.markdown("#### Available Views:")
                    for view in views:
                        if not view or not view.id:
                            continue

                        with st.container():
                            cols = st.columns([3, 1, 1])
                            with cols[0]:
                                if st.button(
                                    f"👁️ {view.name}",
                                    key=f"view_btn_{view.id}",
                                    use_container_width=True,
                                    type=(
                                        "secondary"
                                        if st.session_state.selected_view_id != view.id
                                        else "primary"
                                    ),
                                ):
                                    st.session_state.selected_view_id = view.id
                                    # Updated: Using all three required parameters
                                    st.session_state.current_view_config = (
                                        db.get_view_config(
                                            st.session_state.selected_pc,
                                            st.session_state.selected_screen,
                                            view.id,
                                        )
                                    )
                                    st.rerun()

                            with cols[1]:
                                if st.button(
                                    "✏️", key=f"edit_{view.id}", use_container_width=True
                                ):
                                    st.session_state.editing_view_id = view.id
                                    rename_modal.open()

                            with cols[2]:
                                if st.button(
                                    "🗑️",
                                    key=f"delete_{view.id}",
                                    use_container_width=True,
                                ):
                                    try:
                                        db.delete_view(view.id)
                                        st.session_state.selected_view_id = None
                                        st.rerun()
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to delete view: {e}")
                                        st.error("Failed to delete view.")

        # Camera Selection Modal - IMPROVED IMPLEMENTATION
        # Check if we need to open the modal this frame
        if st.session_state.open_camera_modal and st.session_state.edit_slot:
            st.session_state.open_camera_modal = False  # Reset the flag
            st.session_state.last_edited_slot = st.session_state.edit_slot  # Remember which slot we're editing
            # Reset selection indices when opening a new modal
            st.session_state.modal_site_index = 0
            st.session_state.modal_camera_index = 0
            modal_key = f"camera_modal_{st.session_state.edit_slot}"
            camera_modal = Modal(key=modal_key, title="Select Camera")
            camera_modal.open()  # Explicitly open the modal
        else:
            # Create a modal with the appropriate key
            modal_key = f"camera_modal_{st.session_state.last_edited_slot}" if st.session_state.last_edited_slot else "camera_modal_default"
            camera_modal = Modal(key=modal_key, title="Select Camera")
        
        # Handle the modal content if it's open
        if camera_modal.is_open():
            with camera_modal.container():
                try:
                    logger.debug(f"Modal open for slot: {st.session_state.edit_slot}")
                    
                    # Get site options
                    site_options = [(site_id, site_info['name']) 
                                for site_id, site_info in camera_config['sites'].items()]
                    
                    if not site_options:
                        st.error("No sites found in the site configuration!")
                        time.sleep(1)
                        camera_modal.close()
                        st.session_state.edit_slot = None
                        return
                    
                    # Get a stable unique ID for this modal instance
                    modal_id = st.session_state.last_edited_slot if st.session_state.last_edited_slot else "default"
                    
                    # Create a callback to track site selection changes
                    def on_site_change():
                        # Update the camera index when site changes
                        st.session_state.modal_camera_index = 0
                    
                    # Site selection with stable key and state tracking
                    selected_site_index = st.selectbox(
                        "Select Site",
                        range(len(site_options)),
                        index=st.session_state.modal_site_index,
                        format_func=lambda x: site_options[x][1],
                        key=f"site_select_{modal_id}",
                        on_change=on_site_change
                    )
                    
                    # Update the stored site index
                    st.session_state.modal_site_index = selected_site_index
                    
                    selected_site_id = site_options[selected_site_index][0]
                    selected_site_name = site_options[selected_site_index][1]
                    
                    # Get cameras for selected site
                    cameras = get_site_cameras(camera_config, selected_site_id)
                    
                    if not cameras:
                        st.warning(f"No cameras available for site: {selected_site_name}")
                        if st.button("Close", key=f"close_no_cameras_{modal_id}"):
                            camera_modal.close()
                            st.session_state.edit_slot = None
                        return
                    
                    # Ensure camera index is valid for the current camera list
                    camera_index = min(st.session_state.modal_camera_index, len(cameras) - 1)
                    
                    # Camera selection with stable key and state tracking
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
                                    
                                row, col = map(
                                    int, st.session_state.edit_slot.split("_")[1:]
                                )

                                mapping = ScreenMapping(
                                    pc_id=st.session_state.selected_pc,
                                    screen_id=st.session_state.selected_screen,
                                    view_id=st.session_state.selected_view_id,
                                    slot_row=row,
                                    slot_col=col,
                                    site_id=selected_site_id,
                                    camera_id=selected_camera["camera_id"],
                                )

                                db.add_screen_mapping(mapping)
                                
                                # Refresh the configuration
                                st.session_state.current_view_config = db.get_view_config(
                                    st.session_state.selected_pc,
                                    st.session_state.selected_screen,
                                    st.session_state.selected_view_id,
                                )
                                
                                # Update site name in the current view config
                                slot_name = f"slot_{row}_{col}"
                                if slot_name in st.session_state.current_view_config:
                                    st.session_state.current_view_config[slot_name]["site_name"] = selected_site_name
                                
                                st.session_state.show_save_button = True
                                
                                # Clear edit slot AFTER processing
                                camera_modal.close()
                                st.session_state.edit_slot = None
                            except Exception as e:
                                logger.error(f"Failed to add screen mapping: {e}")
                                st.error("Failed to save camera configuration.")

                    with col2:
                        if st.button("Clear Slot", use_container_width=True, key=f"clear_{modal_id}"):
                            try:
                                # Only proceed if we still have a valid edit_slot
                                if not st.session_state.edit_slot:
                                    st.error("Selection slot lost. Please try again.")
                                    camera_modal.close()
                                    return
                                    
                                row, col = map(
                                    int, st.session_state.edit_slot.split("_")[1:]
                                )

                                db.delete_screen_mapping(
                                    st.session_state.selected_screen,
                                    st.session_state.selected_view_id,
                                    row,
                                    col,
                                )

                                # Refresh the configuration
                                st.session_state.current_view_config = db.get_view_config(
                                    st.session_state.selected_pc,
                                    st.session_state.selected_screen,
                                    st.session_state.selected_view_id,
                                )
                                
                                st.session_state.show_save_button = True
                                
                                # Clear edit slot AFTER processing
                                camera_modal.close()
                                st.session_state.edit_slot = None
                            except Exception as e:
                                logger.error(f"Failed to clear slot: {e}")
                                st.error("Failed to clear camera slot.")
                                
                except Exception as e:
                    logger.error(f"Error in camera selection modal: {e}")
                    st.error("An error occurred while selecting a camera.")
                    camera_modal.close()
                    st.session_state.edit_slot = None

        # Main content area
        if st.session_state.selected_pc and st.session_state.selected_screen:
            pc = db.get_pc_by_id(st.session_state.selected_pc)
            screen = db.get_screen_by_id(st.session_state.selected_screen)
            
            if not pc or not screen:
                st.error("Selected PC or screen not found.")
                return
                
            st.header("Layout Configuration")
            st.subheader(f"{pc.name} - {screen.name}")
            
            # If a view is selected, show the detailed view configuration
            if st.session_state.selected_view_id:
                view = db.get_view_by_id(st.session_state.selected_view_id)
                if not view:
                    st.error("Selected view not found.")
                    return
                    
                st.subheader(f"Configuring View: {view.name}")

                # Refresh the view config to ensure we have the latest data including site names
                st.session_state.current_view_config = db.get_view_config(
                    st.session_state.selected_pc,
                    st.session_state.selected_screen,
                    st.session_state.selected_view_id,
                )

                # Create grid layout
                for row in range(1, screen.rows + 1):
                    cols = st.columns(screen.columns)
                    for col in range(1, screen.columns + 1):
                        with cols[col - 1]:
                            slot_name = f"slot_{row}_{col}"
                            current_slot = st.session_state.current_view_config.get(
                                slot_name
                            )

                            with st.container():
                                st.markdown("----------------")

                                if current_slot:
                                    # Access site name from the camera_config if possible, as a fallback
                                    site_id = current_slot.get("site_id", "")
                                    site_name = current_slot.get("site_name", "Unknown Site")
                                    
                                    # If site_name is missing or "Unknown Site", try to get it from camera_config
                                    if site_name == "Unknown Site" and site_id:
                                        site_info = camera_config.get("sites", {}).get(site_id, {})
                                        if site_info and "name" in site_info:
                                            site_name = site_info["name"]
                                            # Update the current view config with the correct site name
                                            st.session_state.current_view_config[slot_name]["site_name"] = site_name
                                    
                                    camera_name = current_slot.get(
                                        "camera_name", "Unknown Camera"
                                    )
                                    rtsp_url = current_slot.get("rtsp_url", "N/A")

                                    st.markdown(
                                        f"""
                                        ### Slot {row}-{col}

                                        **Site:** {site_name}
                                        **Camera:** {camera_name}
                                        **RTSP:** `{rtsp_url}`
                                    """
                                    )
                                else:
                                    st.markdown(f"### Slot {row}-{col}\n\nEmpty")

                                if not isinstance(
                                    slot_name, str
                                ) or not slot_name.startswith("slot_"):
                                    logger.error(
                                        f"Invalid slot name format: {slot_name}")
                                    st.error("Invalid slot configuration")
                                    continue

                                # FIXED: Use a simple button with proper state management
                                if st.button(
                                    "Select Camera",
                                    key=f"select_{slot_name}",
                                ):
                                    st.session_state.edit_slot = slot_name
                                    st.session_state.open_camera_modal = True
                                    st.rerun()

                # Action buttons
                col1, col2 = st.columns(2)
                with col1:
                    if st.session_state.show_save_button:
                        if st.button("Save View Configuration", type="primary"):
                            try:
                                if not isinstance(
                                    st.session_state.current_view_config, dict
                                ):
                                    raise ValueError(
                                        "Invalid view configuration format")

                                # Additional validation could be added here

                                st.session_state.show_save_button = False
                                st.success(
                                    "View configuration saved successfully!")
                                st.rerun()
                            except Exception as e:
                                logger.error(
                                    f"Failed to save view configuration: {e}")
                                st.error("Failed to save view configuration")
            else:
                # No view selected - show a simpler interface
                st.info("No view is currently selected. You can:")
                col1, col2 = st.columns(2)
                with col1:
                    st.write("1. Select or create a view from the sidebar to configure camera layout")
                with col2:
                    st.write("2. Click 'Configure Live View' to apply current configuration to device")
                
                # Show current PC configuration summary
                st.subheader("Current Device Configuration Summary")
                try:
                    pc_config = db.get_pc_config(st.session_state.selected_pc)
                    if pc_config and 'views' in pc_config.get('mappings', {}).get('screen_to_cameras', {}).get(st.session_state.selected_pc, {}).get(st.session_state.selected_screen, {}):
                        view_count = len(pc_config.get('mappings', {}).get('screen_to_cameras', {}).get(st.session_state.selected_pc, {}).get(st.session_state.selected_screen, {}))
                        camera_count = 0
                        
                        # Count cameras across all views
                        for view_name, view_config in pc_config.get('mappings', {}).get('screen_to_cameras', {}).get(st.session_state.selected_pc, {}).get(st.session_state.selected_screen, {}).items():
                            camera_count += len(view_config)
                        
                        # Display summary metrics
                        col1, col2 = st.columns(2)
                        with col1:
                            st.metric("Total Views", view_count)
                        with col2:
                            st.metric("Total Cameras", camera_count)
                        
                        if view_count > 0:
                            st.success("Device has active configuration that can be applied")
                            
                            # List configured views
                            st.subheader("Configured Views")
                            for view_name in pc_config.get('mappings', {}).get('screen_to_cameras', {}).get(st.session_state.selected_pc, {}).get(st.session_state.selected_screen, {}):
                                cameras_in_view = len(pc_config.get('mappings', {}).get('screen_to_cameras', {}).get(st.session_state.selected_pc, {}).get(st.session_state.selected_screen, {}).get(view_name, {}))
                                st.write(f"• **{view_name}**: {cameras_in_view} cameras configured")
                        else:
                            st.warning("No views configured yet")
                            st.info("Please create a view and configure cameras using the sidebar")
                    else:
                        st.warning("No configuration available for this device")
                        st.info("Please create a view and configure cameras using the sidebar")
                except Exception as e:
                    logger.error(f"Error getting PC config summary: {e}")
                    st.error("Failed to retrieve device configuration summary")

    except Exception as e:
        logger.error(f"Unexpected error in screen_layout_page: {e}")
        st.error("An unexpected error occurred. Please try refreshing the page.")


if __name__ == "__main__":
    try:
        screen_layout_page()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        st.error("A fatal error occurred. Please contact support.")