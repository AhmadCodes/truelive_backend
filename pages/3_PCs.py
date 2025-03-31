import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_site_config
import uuid
import time
from database import Database, PC, Screen
from utils.background_task import initialize_background_task, get_background_status
from utils.token_generator import generate_token
import qrcode
from io import BytesIO
import json
import base64
import requests
import logging
import os

# Set up logging
logging.basicConfig(level=logging.DEBUG,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('pcs_page')

# Initialize the background task system
initialize_background_task()

db = Database()

def get_websocket_server_url():
    """Get the websocket server URL from environment variable"""
    return os.getenv('AWS_WS_URL', 'http://18.204.201.19:8080')

def get_connected_clients():
    """Get list of connected clients from websocket server"""
    try:
        server_url = get_websocket_server_url()
        response = requests.get(f"{server_url}/clients", timeout=2)
        if response.status_code == 200:
            return response.json().get('clients', [])
        else:
            logger.error(f"Failed to get connected clients: {response.status_code}")
            st.warning("Unable to fetch connection status from WebSocket server")
            return []
    except requests.Timeout:
        logger.warning("Timeout while connecting to WebSocket server")
        return []
    except Exception as e:
        logger.error(f"Error getting connected clients: {e}")
        # Don't show warning to user for every status check
        return []

def update_pc_connection_status():
    """Update PC connection status in the database"""
    try:
        connected_clients = get_connected_clients()
        
        if connected_clients:
            logger.info(f"Connected clients: {connected_clients}")
            
            # Update all PCs' connection status
            all_pcs = db.get_pcs()
            for pc in all_pcs:
                is_connected = pc.id in connected_clients
                # Fix string vs boolean comparison
                current_status = pc.last_connected == "True" if isinstance(pc.last_connected, str) else pc.last_connected
                
                if current_status != is_connected:
                    # Only update if changed to reduce DB load
                    db.update_pc_connection_status(pc.id, is_connected)
                    logger.info(f"Updated connection status for {pc.id}: {is_connected}")
                    
        return connected_clients
    except Exception as e:
        logger.error(f"Error updating PC connection status: {e}")
        return []
        
def check_websocket_server_status():
    """Check if the websocket server is running"""
    try:
        server_url = get_websocket_server_url()
        response = requests.get(f"{server_url}/", timeout=3)
        if response.status_code == 200:
            return True
        logger.warning(f"WebSocket server returned non-200 status: {response.status_code}")
        return False
    except requests.RequestException as e:
        logger.warning(f"Failed to connect to WebSocket server: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking WebSocket server: {e}")
        return False

def get_next_monitor_number(screens):
    """Get the next available monitor number after sorting existing screens."""
    if not screens:
        return 1
        
    # Try to extract numeric part from monitor names
    numbers = []
    for screen_id, screen_info in screens.items():
        try:
            name_parts = screen_info["name"].split()
            if len(name_parts) >= 2 and name_parts[0] == "Monitor":
                numbers.append(int(name_parts[1]))
        except (ValueError, IndexError):
            continue
    
    if numbers:
        return max(numbers) + 1
    return len(screens) + 1

def create_screen_structure(screen_id, screen_order, rows=3, cols=3, switching_interval=10):
    return {
        "name": f"Monitor {screen_order}",
        "id": screen_id,
        "layout": {
            "rows": rows,
            "columns": cols
        },
        "switching_interval": switching_interval
    }

def generate_qr_code(data):
    """Generate QR code for connection data"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = BytesIO()
    img.save(buffered)
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return f"data:image/png;base64,{img_str}"

def display_screen_layout_controls(screen_id, screen_info, screens_dict, key_prefix=""):
    """Display layout controls for a screen with live preview."""
    col1, col2, col3, col4, col5 = st.columns([3, 2, 2, 2, 1])
    
    # Screen name
    col1.write(screen_info["name"])
    
    # Layout controls
    current_rows = screen_info["layout"]["rows"]
    current_cols = screen_info["layout"]["columns"]
    
    new_rows = col2.select_slider(
        "Rows",
        options=[1, 2, 3, 4],
        value=current_rows,
        key=f"{key_prefix}rows_{screen_id}"
    )
    
    new_cols = col3.select_slider(
        "Columns",
        options=[1, 2, 3, 4],
        value=current_cols,
        key=f"{key_prefix}cols_{screen_id}"
    )

    # Switching interval for this screen
    current_interval = screen_info.get('switching_interval', 10)
    
    new_interval = col4.number_input(
        "Interval (s)",
        min_value=1,
        value=current_interval,
        key=f"{key_prefix}interval_{screen_id}"
    )
    
    # Update screen info
    if new_interval != current_interval:
        screen_info['switching_interval'] = new_interval
    
    # Update layout if changed
    if new_rows != current_rows or new_cols != current_cols:
        screen_info["layout"]["rows"] = new_rows
        screen_info["layout"]["columns"] = new_cols
        screens_dict[screen_id] = screen_info
    
    # Delete button
    if col5.button("🗑️", key=f"{key_prefix}delete_{screen_id}"):
        return True
    
    # Display layout preview
    with st.expander("View Layout Preview", expanded=False):
        preview_cols = st.columns(new_cols)
        for row in range(new_rows):
            for col in range(new_cols):
                with preview_cols[col]:
                    st.markdown(
                        f"""
                        <div style="
                            border: 1px solid #ccc;
                            padding: 10px;
                            margin: 2px;
                            text-align: center;
                            background-color: #f0f0f0;
                            border-radius: 5px;
                        ">
                            Slot {row + 1},{col + 1}
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
    
    return False

def reorder_monitor_names(screens):
    """Reorder monitor names to be sequential."""
    try:
        # Sort screens by their existing number if possible
        def get_monitor_number(screen_info):
            try:
                # Extract number from "Monitor X" format
                return int(screen_info["name"].split(" ")[1])
            except (IndexError, ValueError):
                # Return a large number for screens without proper naming format
                return 9999
                
        sorted_screens = sorted(screens.items(), key=lambda x: get_monitor_number(x[1]))
        
        # Rename screens sequentially
        for i, (screen_id, screen_info) in enumerate(sorted_screens, 1):
            screens[screen_id]["name"] = f"Monitor {i}"
        return screens
    except Exception as e:
        logger.error(f"Error reordering monitor names: {e}")
        # Return original screens if error occurs
        return screens

def pcs_page():
    st.set_page_config(page_title="PC Management", page_icon="🎥", layout="wide")
    st.title("PC Management")

    config = load_site_config()
    
    # Initialize session state variables
    if "edit_pc_id" not in st.session_state:
        st.session_state["edit_pc_id"] = None
    
    if "token_pc_id" not in st.session_state:
        st.session_state["token_pc_id"] = None
        
    if "current_tab" not in st.session_state:
        st.session_state["current_tab"] = "All PCs"
        
    if "confirm_delete" not in st.session_state:
        st.session_state["confirm_delete"] = None
        
    if "temp_screens" not in st.session_state:
        st.session_state["temp_screens"] = {}
        
    if "show_relationships" not in st.session_state:
        st.session_state["show_relationships"] = False

    # Get all PCs from database
    all_pcs = db.get_pcs()
    manager_pcs = [pc for pc in all_pcs if pc.role == "manager"]

    cols = st.columns([8, 2])
    with cols[1]:
        add_pc_clicked = st.button("Add PC", key="add_pc_btn")

    add_pc_modal = Modal(key="add_pc_modal", title="Add New PC")
    if add_pc_clicked:
        add_pc_modal.open()

    if add_pc_modal.is_open():
        with add_pc_modal.container():
            st.subheader("Add New PC")
            
            col1, col2 = st.columns(2)
            with col1:
                pc_name = st.text_input("PC Name (required)")
                ip_address = st.text_input("IP Address (optional)")
            with col2:
                gpu_type = st.text_input("GPU Type (optional)")
                # Add role selection
                role = st.selectbox(
                    "Role", 
                    options=["controller", "manager"],
                    index=0,
                    help="Manager PCs can control multiple controller PCs"
                )
                
                # Show manager selection only if role is controller
                manager_id = None
                if role == "controller" and manager_pcs:
                    manager_id = st.selectbox(
                        "Assign to Manager",
                        options=["None"] + [f"{pc.name} ({pc.id})" for pc in manager_pcs],
                        format_func=lambda x: x if x == "None" else x.split(" (")[0]
                    )
                    if manager_id != "None":
                        manager_id = manager_id.split("(")[1].split(")")[0]
                    else:
                        manager_id = None
            
            if not st.session_state.get("add_pc_id"):
                st.session_state["add_pc_id"] = f"pc{uuid.uuid4().hex[:4]}"
            
            st.markdown("### Screens")
            if st.button("+ Add Screen"):
                pc_id = st.session_state.get("add_pc_id") or f"pc{uuid.uuid4().hex[:4]}"
                screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                next_monitor_num = get_next_monitor_number(st.session_state.temp_screens)
                st.session_state.temp_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
                st.rerun()
            
            if st.session_state.temp_screens:
                st.markdown("#### Configure Screen Layouts")
                for screen_id, screen_info in list(st.session_state.temp_screens.items()):
                    should_delete = display_screen_layout_controls(
                        screen_id,
                        screen_info,
                        st.session_state.temp_screens,
                        "temp_"
                    )
                    if should_delete:
                        del st.session_state.temp_screens[screen_id]
                        st.session_state.temp_screens = reorder_monitor_names(st.session_state.temp_screens)
                        st.rerun()
            
            if st.button("Save PC", key="save_pc_btn", type="primary"):
                if pc_name:
                    pc_id = st.session_state.get("add_pc_id") or f"pc{str(uuid.uuid4().hex[:4])}"
                    
                    # Create PC with role and manager_id
                    pc = PC(pc_id, pc_name, ip_address, gpu_type, role, manager_id)
                    db.add_pc(pc)
                    
                    # Add screens if any
                    for screen_id, screen_info in st.session_state.temp_screens.items():
                        screen = Screen(
                            screen_id,
                            pc_id,
                            screen_info["name"],
                            screen_info["layout"]["rows"],
                            screen_info["layout"]["columns"],
                            screen_info["switching_interval"]
                        )
                        db.add_screen(screen)
                    
                    # Generate token after PC is created
                    st.session_state["token_pc_id"] = pc_id
                    
                    st.success(f"PC '{pc_name}' added successfully.")
                    st.session_state.temp_screens = {}
                    st.session_state["add_pc_id"] = None
                    add_pc_modal.close()
                    # Allow the success message to be visible briefly
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Please enter a PC name.")

    edit_pc_modal = Modal(key="edit_pc_modal", title="Edit PC")
    
    if st.session_state["edit_pc_id"] and not edit_pc_modal.is_open():
        edit_pc_modal.open()

    if edit_pc_modal.is_open():
        with edit_pc_modal.container():
            pc_id = st.session_state["edit_pc_id"]
            pc = db.get_pc_by_id(pc_id)
            
            if pc:
                st.subheader(f"Edit PC: {pc.name}")
                st.warning("Please note that changing the layout of existing screens may wipe existing configurations.")
                
                col1, col2 = st.columns(2)
                with col1:
                    new_name = st.text_input("PC Name (required)", value=pc.name)
                    new_ip = st.text_input("IP Address (optional)", value=pc.ip_address or "")
                with col2:
                    new_gpu = st.text_input("GPU Type (optional)", value=pc.gpu_type or "")
                    # Add role selection with current role as default
                    new_role = st.selectbox(
                        "Role", 
                        options=["controller", "manager"],
                        index=0 if pc.role == "controller" else 1,
                        help="Manager PCs can control multiple controller PCs"
                    )
                    
                    # Show manager selection only if role is controller
                    new_manager_id = None
                    if new_role == "controller":
                        # Filter out self from manager options
                        available_managers = [m for m in manager_pcs if m.id != pc_id]
                        
                        manager_options = ["None"] + [f"{m.name} ({m.id})" for m in available_managers]
                        default_index = 0  # "None" by default
                        
                        # Find current manager in the list if it exists
                        if pc.manager_id:
                            for i, opt in enumerate(manager_options):
                                if pc.manager_id in opt:
                                    default_index = i
                                    break
                        
                        new_manager = st.selectbox(
                            "Assign to Manager",
                            options=manager_options,
                            index=default_index,
                            format_func=lambda x: x if x == "None" else x.split(" (")[0]
                        )
                        
                        if new_manager != "None":
                            new_manager_id = new_manager.split("(")[1].split(")")[0]
                
                # Load screens for this PC
                if "edit_screens" not in st.session_state:
                    # Get screens from database
                    screen_rows = db.get_screens_by_pc(pc_id)
                    st.session_state.edit_screens = {}
                    for screen in screen_rows:
                        st.session_state.edit_screens[screen.id] = {
                            "name": screen.name,
                            "id": screen.id,
                            "layout": {
                                "rows": screen.rows,
                                "columns": screen.columns
                            },
                            "switching_interval": screen.switching_interval
                        }
                
                st.markdown("### Screens")
                if st.button("+ Add Screen"):
                    screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                    next_monitor_num = get_next_monitor_number(st.session_state.edit_screens)
                    st.session_state.edit_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
                    st.rerun()
                
                if st.session_state.edit_screens:
                    st.markdown("#### Configure Screen Layouts")
                    for screen_id, screen_info in list(st.session_state.edit_screens.items()):
                        should_delete = display_screen_layout_controls(
                            screen_id,
                            screen_info,
                            st.session_state.edit_screens,
                            "edit_"
                        )
                        if should_delete:
                            st.session_state.deleted_screens = st.session_state.get("deleted_screens", [])
                            st.session_state.deleted_screens.append(screen_id)
                            del st.session_state.edit_screens[screen_id]
                            st.session_state.edit_screens = reorder_monitor_names(st.session_state.edit_screens)
                            st.rerun()
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Save Changes", key="save_pc_changes", use_container_width=True, type="primary"):
                        if new_name:
                            try:
                                # Handle role change from controller to manager
                                if pc.role == "controller" and new_role == "manager":
                                    # Clear manager_id as this PC is now a manager
                                    new_manager_id = None
                                
                                # Handle role change from manager to controller
                                if pc.role == "manager" and new_role == "controller":
                                    # Update any controllers that had this PC as manager
                                    db.clear_manager_from_controllers(pc_id)
                                
                                # Update PC info
                                updated_pc = PC(pc_id, new_name, new_ip, new_gpu, new_role, new_manager_id, 
                                              pc.auth_token, pc.token_expiry, pc.last_connected)
                                db.update_pc(updated_pc)
                                
                                # Update screens
                                for screen_id, screen_info in st.session_state.edit_screens.items():
                                    screen = Screen(
                                        screen_id,
                                        pc_id,
                                        screen_info["name"],
                                        screen_info["layout"]["rows"],
                                        screen_info["layout"]["columns"],
                                        screen_info["switching_interval"]
                                    )
                                    db.update_screen(screen)
                                
                                # Handle deleted screens if any
                                if hasattr(st.session_state, "deleted_screens"):
                                    for screen_id in st.session_state.deleted_screens:
                                        db.delete_screen(screen_id)
                                    del st.session_state.deleted_screens
                                
                                st.success("PC updated successfully.")
                                # Clear state before rerun
                                clear_edit_state = st.session_state["edit_pc_id"]
                                del st.session_state.edit_screens
                                st.session_state["edit_pc_id"] = None
                                edit_pc_modal.close()
                                # Allow success message to be visible briefly
                                time.sleep(0.5)
                                st.rerun()
                            except Exception as e:
                                logger.error(f"Error updating PC {pc_id}: {e}")
                                st.error(f"Failed to update PC: {str(e)}")
                        else:
                            st.error("Please check required fields.")
                with col2:
                    if st.button("Cancel", key="cancel_edit", use_container_width=True):
                        st.session_state["edit_pc_id"] = None
                        if "edit_screens" in st.session_state:
                            del st.session_state.edit_screens
                        edit_pc_modal.close()
                        st.rerun()

    # Token generation modal
    token_modal = Modal(key="token_modal", title="PC Authentication Token")
    
    if st.session_state["token_pc_id"] and not token_modal.is_open():
        token_modal.open()
    
    if token_modal.is_open():
        with token_modal.container():
            pc_id = st.session_state["token_pc_id"]
            pc = db.get_pc_by_id(pc_id)
            
            if pc:
                st.subheader(f"Connection Token for {pc.name}")
                
                # Check if token already exists
                if pc.auth_token and pc.token_expiry:
                    st.info(f"This PC already has a token that expires on {pc.token_expiry}")
                    regenerate = st.button("Regenerate Token")
                    
                    if regenerate:
                        try:
                            # Generate new token
                            token, expiry = generate_token(pc.id, pc.name, pc.role, pc.manager_id, expiry_hours=8760)  # 1 year
                            db.update_pc_token(pc.id, token, expiry)
                            st.success("Token regenerated successfully!")
                            st.rerun()
                        except Exception as e:
                            logger.error(f"Error regenerating token: {e}")
                            st.error(f"Failed to regenerate token: {str(e)}")
                else:
                    try:
                        # Generate new token
                        token, expiry = generate_token(pc.id, pc.name, pc.role, pc.manager_id, expiry_hours=8760)  # 1 year
                        db.update_pc_token(pc.id, token, expiry)
                        st.success("Token generated successfully!")
                    except Exception as e:
                        logger.error(f"Error generating token: {e}")
                        st.error(f"Failed to generate token: {str(e)}")
                
                # Display token and connection info
                pc = db.get_pc_by_id(pc_id)  # Refresh PC data to get the token
                
                st.markdown("### Manual Configuration")
                st.code(pc.auth_token, language=None, wrap_lines=True)
                
                # Close button
                if st.button("Close", key="close_token_modal"):
                    st.session_state["token_pc_id"] = None
                    token_modal.close()
                    st.rerun()

    # Relationship management modal
    rel_modal = Modal(key="relationship_modal", title="Manage PC Relationships")
    
    if st.session_state["show_relationships"] and not rel_modal.is_open():
        rel_modal.open()
    
    if rel_modal.is_open():
        with rel_modal.container():
            st.subheader("Manager-Controller Relationships")
            
            # Get managers
            managers = db.get_manager_pcs()
            
            if not managers:
                st.warning("No manager PCs are configured. Add PCs with the 'manager' role first.")
            else:
                # For each manager, show controllers
                for manager in managers:
                    with st.expander(f"Manager: {manager.name} ({manager.id})", expanded=True):
                        controllers = db.get_controllers_by_manager(manager.id)
                        
                        if not controllers:
                            st.info(f"No controllers assigned to {manager.name}")
                            
                            # Add controller button
                            if st.button(f"Assign Controller to {manager.name}", key=f"add_ctrl_{manager.id}"):
                                # Get unassigned controllers
                                unassigned = db.get_controllers_by_manager(None)
                                if unassigned:
                                    options = [f"{pc.name} ({pc.id})" for pc in unassigned]
                                    
                                    selected = st.selectbox(
                                        "Select Controller to Assign",
                                        options=options,
                                        key=f"assign_{manager.id}"
                                    )
                                    
                                    if st.button("Confirm Assignment", key=f"confirm_{manager.id}"):
                                        controller_id = selected.split("(")[1].split(")")[0]
                                        db.update_controller_manager(controller_id, manager.id)
                                        st.success("Controller assigned successfully!")
                                        time.sleep(0.5)
                                        st.rerun()
                                else:
                                    st.warning("No unassigned controllers available.")
                        else:
                            # Display controllers in a table
                            for i, controller in enumerate(controllers):
                                cols = st.columns([3, 2, 2])
                                with cols[0]:
                                    st.write(f"{i+1}. {controller.name}")
                                with cols[1]:
                                    st.write(f"ID: {controller.id}")
                                with cols[2]:
                                    if st.button("Unassign", key=f"unassign_{controller.id}"):
                                        db.update_controller_manager(controller.id, None)
                                        st.success(f"{controller.name} unassigned from {manager.name}")
                                        time.sleep(0.5)
                                        st.rerun()
                        
                        # Add horizontal rule
                        st.markdown("---")
            
            # Display unassigned controllers
            unassigned = db.get_controllers_by_manager(None)
            if unassigned:
                with st.expander("Unassigned Controllers", expanded=True):
                    for i, controller in enumerate(unassigned):
                        cols = st.columns([3, 2, 2])
                        with cols[0]:
                            st.write(f"{i+1}. {controller.name}")
                        with cols[1]:
                            st.write(f"ID: {controller.id}")
                        with cols[2]:
                            # Dropdown to select manager
                            manager_options = [f"{m.name} ({m.id})" for m in managers]
                            selected_manager = st.selectbox(
                                "Assign to Manager",
                                options=["Select..."] + manager_options,
                                key=f"assign_manager_{controller.id}"
                            )
                            
                            if selected_manager != "Select...":
                                manager_id = selected_manager.split("(")[1].split(")")[0]
                                if st.button("Assign", key=f"do_assign_{controller.id}"):
                                    db.update_controller_manager(controller.id, manager_id)
                                    st.success(f"{controller.name} assigned to {selected_manager.split(' (')[0]}")
                                    time.sleep(0.5)
                                    st.rerun()
            
            # Close button
            if st.button("Close", key="close_rel_modal"):
                st.session_state["show_relationships"] = False
                rel_modal.close()
                st.rerun()

    # Main content - Display existing PCs with enhanced information
    st.markdown("### Existing PCs")
    
    # Action buttons
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Manage PC Relationships", key="show_rel"):
            st.session_state["show_relationships"] = True
            rel_modal.open()
    
    # Get all PCs from database
    pcs = db.get_pcs()
    
    if pcs:
        # Create tabs for different role views
        tab1, tab2, tab3 = st.tabs(["All PCs", "Managers", "Controllers"])
        
        # Use session state to keep track of which tab we're in
        # This helps generate unique keys for each tab
        
        with tab1:
            if st.session_state["current_tab"] != "All PCs":
                st.session_state["current_tab"] = "All PCs"
            display_pc_table(pcs, db, "all_")
        
        with tab2:
            managers = [pc for pc in pcs if pc.role == "manager"]
            if st.session_state["current_tab"] != "Managers":
                st.session_state["current_tab"] = "Managers"
            if managers:
                display_pc_table(managers, db, "mgr_")
            else:
                st.info("No manager PCs configured.")
        
        with tab3:
            controllers = [pc for pc in pcs if pc.role == "controller"]
            if st.session_state["current_tab"] != "Controllers":
                st.session_state["current_tab"] = "Controllers"
            if controllers:
                display_pc_table(controllers, db, "ctrl_")
            else:
                st.info("No controller PCs configured.")
    else:
        st.info("No PCs available.")

def display_pc_table(pcs, db, prefix=""):
    """Display PC table with unique keys for each tab view"""
    # Update connection status before displaying
    try:
        connected_clients = update_pc_connection_status()
        server_running = check_websocket_server_status()
    except Exception as e:
        logger.error(f"Error checking connection status: {e}")
        connected_clients = []
        server_running = False
        st.warning("Error checking connection status. WebSocket server may be down.")
    
    # Display server status
    if not server_running:
        st.warning("⚠️ WebSocket server not reachable - connection status may be inaccurate")
    
    # Display header
    header_cols = st.columns([2, 1, 1, 1, 1, 1, 2])
    header_cols[0].write("Name")
    header_cols[1].write("Role")
    header_cols[2].write("Screens")
    header_cols[3].write("IP Address")
    header_cols[4].write("Manager")
    header_cols[5].write("Connection")
    header_cols[6].write("Actions")

    # Display PC rows
    for pc in pcs:
        row_cols = st.columns([2, 1, 1, 1, 1, 1, 2])
        
        # Name
        row_cols[0].write(pc.name)
        
        # Role
        role_color = "green" if pc.role == "manager" else "blue"
        row_cols[1].markdown(f"<span style='color:{role_color};'>{pc.role}</span>", unsafe_allow_html=True)
        
        # Screens
        screens = db.get_screens_by_pc(pc.id)
        row_cols[2].write(len(screens))
        
        # IP Address
        row_cols[3].write(pc.ip_address or "—")
        
        # Manager information
        if pc.role == "controller" and pc.manager_id:
            manager = db.get_pc_by_id(pc.manager_id)
            if manager:
                row_cols[4].write(manager.name)
            else:
                row_cols[4].write("Unknown")
        else:
            row_cols[4].write("—")
        
        # Connection status with real-time check
        if server_running and pc.id in connected_clients:
            row_cols[5].markdown("<span style='color:green;'>✅ Connected</span>", unsafe_allow_html=True)
        elif pc.last_connected == "True" and not server_running:
            row_cols[5].markdown("<span style='color:yellow;'>❓ Unknown</span>", unsafe_allow_html=True)
        else:
            row_cols[5].markdown("<span style='color:red;'>❌ Offline</span>", unsafe_allow_html=True)
        
        # Actions - with unique keys based on tab prefix
        with row_cols[6]:
            col1, col2, col3 = st.columns(3, gap="small")
            with col1:
                if st.button("✏️", key=f"{prefix}edit_{pc.id}"):
                    st.session_state["edit_pc_id"] = pc.id
                    st.rerun()
            with col2:
                if st.button("🔑", key=f"{prefix}token_{pc.id}"):
                    st.session_state["token_pc_id"] = pc.id
                    st.rerun()
            with col3:
                if st.button("🗑️", key=f"{prefix}delete_{pc.id}"):
                    if st.session_state.get("confirm_delete") == pc.id:
                        try:
                            db.delete_pc(pc.id)
                            st.session_state.pop("confirm_delete", None)
                            st.success("PC deleted.")
                            time.sleep(0.5)
                            st.rerun()
                        except Exception as e:
                            logger.error(f"Error deleting PC {pc.id}: {e}")
                            st.error(f"Failed to delete PC: {str(e)}")
                    else:
                        st.session_state["confirm_delete"] = pc.id
                        st.warning("Click delete again to confirm.")
                        time.sleep(0.5)
                        st.rerun()
        
        # Expandable details section
        with st.expander(f"Details for {pc.name}", expanded=False):
            # PC details
            st.markdown(f"**PC ID:** {pc.id}")
            st.markdown(f"**GPU Type:** {pc.gpu_type or 'Not specified'}")
            
            # Token information
            if pc.auth_token:
                expiry_status = "Valid" if pc.token_expiry else "Unknown"
                st.markdown(f"**Token Status:** {expiry_status}")
                st.markdown(f"**Token Expiry:** {pc.token_expiry or 'Not set'}")
            else:
                st.markdown("**Token Status:** Not generated")
                if st.button("Generate Token", key=f"{prefix}gen_token_{pc.id}"):
                    st.session_state["token_pc_id"] = pc.id
                    st.rerun()
            
            # Screen information
            if screens:
                st.markdown("#### Screens")
                for screen in screens:
                    st.markdown(f"**{screen.name}**")
                    cols = st.columns(3)
                    cols[0].write(f"Layout: {screen.rows}x{screen.columns}")
                    cols[1].write(f"Switching Interval: {screen.switching_interval}s")

if __name__ == "__main__":
    pcs_page()