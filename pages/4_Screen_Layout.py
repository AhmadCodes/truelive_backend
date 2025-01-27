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


def send_screen_mapping(config: Dict[str, Any]) -> bool:
    """Send screen mapping configuration with validation"""
    if not config:
        logger.error("Empty configuration provided")
        return False

    try:
        generated_config = generate_config(config)
        return send_config_sync(generated_config)
    except Exception as e:
        logger.error(f"Failed to send screen mapping: {e}")
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


def screen_layout_page():
    try:
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
        }

        for key, default_value in session_states.items():
            if key not in st.session_state:
                st.session_state[key] = default_value

        with st.sidebar:
            st.header("Navigation")

            # Live View Configuration Button
            if st.button("Configure Live View", type="primary"):
                if not st.session_state.selected_view_id:
                    st.warning("Please select a view first.")
                    return

                view = db.get_view_by_id(st.session_state.selected_view_id)
                if not view:
                    st.error("Selected view not found.")
                    return

                # Updated: Using all three required parameters
                config = db.get_view_config(
                    st.session_state.selected_pc,
                    st.session_state.selected_screen,
                    st.session_state.selected_view_id,
                )
                if not config:
                    st.error("View configuration is empty.")
                    return

                mapping_ret = send_screen_mapping(config)
                if mapping_ret:
                    success = st.success(
                        "Live view configuration applied successfully!"
                    )
                    time.sleep(1)
                    success.empty()
                else:
                    st.error("Failed to apply live view configuration!")

            # PC Selection
            pcs = db.get_pcs()
            if not pcs:
                st.warning("No PCs configured in the system.")
                return

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
                                            db.update_view_name(
                                                view.id, new_name)
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

                        new_view = View(
                            id=f"{st.session_state.selected_screen}_{
                                next_view_num}",
                            screen_id=st.session_state.selected_screen,
                            name=new_view_name,
                            layout_rows=screen.rows,
                            layout_columns=screen.columns,
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

        # Camera Selection Modal
        if st.session_state.edit_slot and camera_modal.is_open():
            with camera_modal.container():
                site_options = [
                    (site_id, site_info.get("name", "Unknown"))
                    for site_id, site_info in camera_config["sites"].items()
                    if isinstance(site_info, dict) and "name" in site_info
                ]

                if not site_options:
                    st.error("No valid sites found in the configuration!")
                    time.sleep(2)
                    camera_modal.close()
                    return

                selected_site_index = st.selectbox(
                    "Select Site",
                    range(len(site_options)),
                    format_func=lambda x: site_options[x][1],
                )

                selected_site_id = site_options[selected_site_index][0]

                cameras = get_site_cameras(camera_config, selected_site_id)
                if not cameras:
                    st.warning("No cameras found for this site.")
                    return

                selected_camera_index = st.selectbox(
                    "Select Camera",
                    range(len(cameras)),
                    format_func=lambda x: cameras[x]["name"],
                )
                selected_camera = cameras[selected_camera_index]

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Confirm", use_container_width=True):
                        try:
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
                            # Updated: Using all three required parameters
                            st.session_state.current_view_config = db.get_view_config(
                                st.session_state.selected_pc,
                                st.session_state.selected_screen,
                                st.session_state.selected_view_id,
                            )
                            st.session_state.edit_slot = None
                            st.session_state.show_save_button = True
                            camera_modal.close()
                            st.rerun()
                        except Exception as e:
                            logger.error(f"Failed to add screen mapping: {e}")
                            st.error("Failed to save camera configuration.")

                with col2:
                    if st.button("Clear Slot", use_container_width=True):
                        try:
                            row, col = map(
                                int, st.session_state.edit_slot.split("_")[1:]
                            )

                            db.delete_screen_mapping(
                                st.session_state.selected_screen,
                                st.session_state.selected_view_id,
                                row,
                                col,
                            )

                            # Updated: Using all three required parameters
                            st.session_state.current_view_config = db.get_view_config(
                                st.session_state.selected_pc,
                                st.session_state.selected_screen,
                                st.session_state.selected_view_id,
                            )
                            st.session_state.edit_slot = None
                            st.session_state.show_save_button = True
                            camera_modal.close()
                            st.rerun()
                        except Exception as e:
                            logger.error(f"Failed to clear slot: {e}")
                            st.error("Failed to clear camera slot.")

        # Main content area
        if all(
            [
                st.session_state.selected_pc,
                st.session_state.selected_screen,
                st.session_state.selected_view_id,
            ]
        ):
            pc = db.get_pc_by_id(st.session_state.selected_pc)
            screen = db.get_screen_by_id(st.session_state.selected_screen)
            view = db.get_view_by_id(st.session_state.selected_view_id)

            # Updated: Using all three required parameters
            st.session_state.current_view_config = db.get_view_config(
                st.session_state.selected_pc,
                st.session_state.selected_screen,
                st.session_state.selected_view_id,
            )

            if not pc or not screen or not view:
                st.error("Selected PC, screen, or view not found.")
                return

            st.header("Layout Configuration")
            st.subheader(f"{pc.name} - {screen.name} - {view.name}")

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
                                site_name = current_slot.get(
                                    "site_name", "Unknown Site"
                                )
                                camera_name = current_slot.get(
                                    "camera_name", "Unknown Camera"
                                )
                                rtsp_url = current_slot.get("rtsp_url", "N/A")
                                playing_state = current_slot.get(
                                    "playing_state", False)

                                st.markdown(
                                    f"""
                                    ### Slot {row}-{col}

                                    **Site:** {site_name}
                                    **Camera:** {camera_name}
                                    

                                    **RTSP:** `{rtsp_url}`
                                """
                                ) #**Status:** {'Playing' if playing_state else 'Stopped'}
                            else:
                                st.markdown(f"### Slot {row}-{col}\n\nEmpty")

                            if not isinstance(
                                slot_name, str
                            ) or not slot_name.startswith("slot_"):
                                logger.error(
                                    f"Invalid slot name format: {slot_name}")
                                st.error("Invalid slot configuration")
                                continue

                            st.button(
                                "Select Camera",
                                key=f"select_{slot_name}",
                                on_click=lambda s=slot_name: [
                                    setattr(st.session_state, "edit_slot", s),
                                    camera_modal.open(),
                                ],
                            )

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

    except Exception as e:
        logger.error(f"Unexpected error in screen_layout_page: {e}")
        st.error("An unexpected error occurred. Please try refreshing the page.")


if __name__ == "__main__":
    try:
        screen_layout_page()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        st.error("A fatal error occurred. Please contact support.")
