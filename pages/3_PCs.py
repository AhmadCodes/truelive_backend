import streamlit as st
import pandas as pd
import time
from streamlit_modal import Modal
from utils.config_loader import load_site_config, save_site_config
import uuid

# Helper functions (unchanged)
def create_empty_slot_mapping(rows, cols):
    """Create an empty mapping structure based on the specified layout"""
    empty_slots = {}
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            slot_name = f"slot_{row}_{col}"
            empty_slots[slot_name] = None
    return empty_slots

def create_screen_structure(screen_id, screen_order, rows=3, cols=3, switching_interval=10):
    """Create the screen structure for a new screen with layout information"""
    return {
        "name": f"Monitor {screen_order}",
        "id": screen_id,
        "layout": {
            "rows": rows,
            "columns": cols
        },
        "switching_interval": switching_interval
    }

def create_screen_camera_mapping(screen_id, rows=3, cols=3):
    """Create empty camera mappings for a single screen"""
    return {
        "view_1": create_empty_slot_mapping(rows, cols)
    }

def get_next_monitor_number(screens):
    """Get the next available monitor number"""
    return len(screens) + 1

def reorder_monitor_names(screens):
    """Reorder monitor names to be sequential"""
    sorted_screens = sorted(screens.items(), key=lambda x: x[1]["name"])
    for i, (screen_id, screen_info) in enumerate(sorted_screens, 1):
        screens[screen_id]["name"] = f"Monitor {i}"
    return screens

def display_screen_layout_controls(screen_id, screen_info, screens_dict, key_prefix=""):
    """Display layout controls for a screen with live preview"""
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

def pcs_page():
    st.set_page_config(
        page_title="Live View Camera Configuration System",
        page_icon="🎥",
        layout="wide"
    )
    st.title("PC Management")

    config = load_site_config()
    if "edit_pc_id" not in st.session_state:
        st.session_state["edit_pc_id"] = None

    # Check if a PC already exists
    existing_pcs = len(config.get("pcs", {}))

    # Top-right "Add PC" button
    cols = st.columns([8, 2])
    with cols[1]:
        add_pc_clicked = st.button("Add PC", key="add_pc_btn")

    # ----- Add PC Modal -----
    add_pc_modal = Modal(key="add_pc_modal", title="Add New PC")
    if add_pc_clicked:
        if existing_pcs > 0:
            st.error("Multiple PC configuration is not currently supported in this version.")
            return
        add_pc_modal.open()

    if add_pc_modal.is_open():
        with add_pc_modal.container():
            st.subheader("Add New PC")
            
            # Basic PC information
            col1, col2 = st.columns(2)
            with col1:
                pc_name = st.text_input("PC Name (required)")
                ip_address = st.text_input("IP Address (optional)")
            with col2:
                gpu_type = st.text_input("GPU Type (optional)")
            
            # Initialize screens list in session state
            if "temp_screens" not in st.session_state:
                st.session_state.temp_screens = {}
            
            # if st.session_state.get("edit_pc_id")  is not set, then it is a new PC so set it
            if not st.session_state.get("add_pc_id"):
                st.session_state["add_pc_id"] = f"pc{uuid.uuid4().hex[:4]}"
            
            # Add screen button
            st.markdown("### Screens")
            if st.button("+ Add Screen"):
                pc_id = st.session_state.get("add_pc_id") or f"pc{uuid.uuid4().hex[:4]}"
                screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                next_monitor_num = get_next_monitor_number(st.session_state.temp_screens)
                st.session_state.temp_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
            
            # Display existing screens with layout controls
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
            
            # Save button
            if st.button("Save PC", key="save_pc_btn", type="primary"):
                if pc_name and st.session_state.temp_screens:
                    pc_id = st.session_state.get("add_pc_id") or f"pc{str(uuid.uuid4().hex[:4])}"
                    
                    # Create PC entry
                    config['pcs'][pc_id] = {
                        "name": pc_name,
                        "ip_address": ip_address,
                        "gpu_type": gpu_type,
                        "screens": st.session_state.temp_screens.copy(),
                        "n_screens": len(st.session_state.temp_screens)
                    }
                    
                    # Initialize mappings
                    if 'mappings' not in config:
                        config['mappings'] = {}
                    if 'screen_to_cameras' not in config['mappings']:
                        config['mappings']['screen_to_cameras'] = {}
                    
                    # Create mappings for each screen with proper layout
                    config['mappings']['screen_to_cameras'][pc_id] = {}
                    for screen_id, screen_info in st.session_state.temp_screens.items():
                        rows = screen_info['layout']['rows']
                        cols = screen_info['layout']['columns']
                        config['mappings']['screen_to_cameras'][pc_id][screen_id] = create_screen_camera_mapping(screen_id, rows, cols)
                    
                    save_site_config(config)
                    st.success(f"PC '{pc_name}' added successfully.")
                    st.session_state.temp_screens = {}
                    time.sleep(0.5)
                    st.session_state["add_pc_id"] = None
                    add_pc_modal.close()
                    st.rerun()
                else:
                    st.error("Please enter a PC name and add at least one screen.")

    # ----- Edit PC Modal -----
    edit_pc_modal = Modal(key="edit_pc_modal", title="Edit PC")
    
    if st.session_state["edit_pc_id"] and not edit_pc_modal.is_open():
        edit_pc_modal.open()

    if edit_pc_modal.is_open():
        with edit_pc_modal.container():
            pc_id = st.session_state["edit_pc_id"]
            pc_info = config["pcs"].get(pc_id, {})
            
            if pc_info:
                st.subheader(f"Edit PC: {pc_info.get('name', '')}")
                st.warning("Please note that changing the layout of existing screens may wipe existing configurations.")
                
                # Basic PC information
                col1, col2 = st.columns(2)
                with col1:
                    new_name = st.text_input("PC Name (required)", value=pc_info.get("name", ""))
                    new_ip = st.text_input("IP Address (optional)", value=pc_info.get("ip_address", ""))
                with col2:
                    new_gpu = st.text_input("GPU Type (optional)", value=pc_info.get("gpu_type", ""))
                
                # Initialize edit screens in session state
                if "edit_screens" not in st.session_state:
                    st.session_state.edit_screens = pc_info.get("screens", {}).copy()
                
                # Add screen button
                st.markdown("### Screens")
                if st.button("+ Add Screen"):
                    screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                    next_monitor_num = get_next_monitor_number(st.session_state.edit_screens)
                    st.session_state.edit_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
                
                # Display existing screens with layout controls
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
                
                # Save and Cancel buttons
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Save Changes", key="save_pc_changes", use_container_width=True, type="primary"):
                        if new_name and st.session_state.edit_screens:
                            pc_info.update({
                                "name": new_name,
                                "ip_address": new_ip,
                                "gpu_type": new_gpu,
                                "screens": st.session_state.edit_screens.copy(),
                                "n_screens": len(st.session_state.edit_screens)
                            })
                            
                            # Update screen mappings
                            for screen_id, screen_info in st.session_state.edit_screens.items():
                                rows = screen_info['layout']['rows']
                                cols = screen_info['layout']['columns']
                                
                                if screen_id not in config['mappings']['screen_to_cameras'][pc_id]:
                                    config['mappings']['screen_to_cameras'][pc_id][screen_id] = create_screen_camera_mapping(screen_id, rows, cols)
                                else:
                                    # Update existing mapping if layout changed
                                    current_mapping = config['mappings']['screen_to_cameras'][pc_id][screen_id]
                                    current_mapping['view_1'] = create_empty_slot_mapping(rows, cols)
                            
                            # Remove deleted screens from mappings
                            if "deleted_screens" in st.session_state:
                                for screen_id in st.session_state.deleted_screens:
                                    if screen_id in config['mappings']['screen_to_cameras'][pc_id]:
                                        del config['mappings']['screen_to_cameras'][pc_id][screen_id]
                                del st.session_state.deleted_screens
                            
                            config["pcs"][pc_id] = pc_info
                            save_site_config(config)
                            st.success("PC updated successfully.")
                            del st.session_state.edit_screens
                            time.sleep(0.5)
                            st.session_state["edit_pc_id"] = None
                            edit_pc_modal.close()
                            st.rerun()
                        else:
                            st.error("Please check required fields and ensure at least one screen exists.")
                with col2:
                    if st.button("Cancel", key="cancel_edit", use_container_width=True):
                        st.session_state["edit_pc_id"] = None
                        if "edit_screens" in st.session_state:
                            del st.session_state.edit_screens
                        edit_pc_modal.close()
                        st.rerun()

    # ----- List Existing PCs -----
    st.markdown("### Existing PCs")
    if config.get("pcs"):
        header_cols = st.columns([3, 2, 2, 2, 2])
        header_cols[0].write("Name")
        header_cols[1].write("No. of Screens")
        header_cols[2].write("IP Address")
        header_cols[3].write("GPU Type")
        header_cols[4].write("Actions")

        for pc_id, pc_info in config["pcs"].items():
            row_cols = st.columns([3, 2, 2, 2, 2])
            row_cols[0].write(pc_info.get("name", ""))
            row_cols[1].write(len(pc_info.get("screens", {})))
            row_cols[2].write(pc_info.get("ip_address", ""))
            row_cols[3].write(pc_info.get("gpu_type", ""))

            with row_cols[4]:
                col_edit, col_delete = st.columns([1, 1], gap="small")
                with col_edit:
                    if st.button("✏️", key=f"edit_{pc_id}"):
                        st.session_state["edit_pc_id"] = pc_id
                        edit_pc_modal.open()
                with col_delete:
                    if st.button("🗑️", key=f"delete_{pc_id}"):
                        del config["pcs"][pc_id]
                        if pc_id in config.get('mappings', {}).get('screen_to_cameras', {}):
                            del config['mappings']['screen_to_cameras'][pc_id]
                        save_site_config(config)
                        st.success("PC deleted.")
                        time.sleep(0.5)
                        st.rerun()
            
            # Display screen details in an expander
            with st.expander(f"Screen Details for {pc_info.get('name', '')}"):
                for screen_id, screen_info in pc_info.get("screens", {}).items():
                    st.markdown(f"**{screen_info['name']}**")
                    cols = st.columns(3)
                    cols[0].write(f"Layout: {screen_info['layout']['rows']}x{screen_info['layout']['columns']}")
                    
                    # Get switching interval from mappings
                    switching_interval = config.get('mappings', {}).get('screen_to_cameras', {}).get(pc_id, {}).get(screen_id, {}).get('switching_interval', 10)
                    cols[1].write(f"Switching Interval: {switching_interval}s")
    else:
        st.info("No PCs available.")

pcs_page()