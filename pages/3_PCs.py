import streamlit as st
import pandas as pd
import time
from streamlit_modal import Modal
from utils.config_loader import load_site_config, save_site_config
import uuid

def create_empty_slot_mapping():
    """Create an empty mapping structure for 9 slots in a 3x3 grid"""
    empty_slots = {}
    for row in range(1, 4):
        for col in range(1, 4):
            slot_name = f"slot_{row}_{col}"
            empty_slots[slot_name] = None
    return empty_slots

def create_screen_structure(screen_id, screen_order):
    """Create the screen structure for a new screen"""
    return {
        "name": f"Monitor {screen_order}",
        "id": screen_id
    }

def create_screen_camera_mapping(screen_id):
    """Create empty camera mappings for a single screen"""
    return {
        "view_1": create_empty_slot_mapping()
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

    # Top-right "Add PC" button
    cols = st.columns([8, 2])
    with cols[1]:
        add_pc_clicked = st.button("Add PC", key="add_pc_btn")

    # ----- Add PC Modal -----
    add_pc_modal = Modal(key="add_pc_modal", title="Add New PC")
    if add_pc_clicked:
        add_pc_modal.open()

    if add_pc_modal.is_open():
        with add_pc_modal.container():
            st.subheader("Add New PC")
            pc_name = st.text_input("PC Name (required)")
            ip_address = st.text_input("IP Address (optional)")
            gpu_type = st.text_input("GPU Type (optional)")
            
            # Initialize screens list in session state
            if "temp_screens" not in st.session_state:
                st.session_state.temp_screens = {}
            
            # Add screen button
            if st.button("Add Screen"):
                pc_id = st.session_state["edit_pc_id"]
                screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                next_monitor_num = get_next_monitor_number(st.session_state.temp_screens)
                st.session_state.temp_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
            
            # Display existing screens
            st.markdown("### Screens")
            for screen_id, screen_info in st.session_state.temp_screens.items():
                col1, col2 = st.columns([4, 1])
                col1.write(screen_info["name"])
                if col2.button("🗑️", key=f"delete_temp_{screen_id}"):
                    del st.session_state.temp_screens[screen_id]
                    st.session_state.temp_screens = reorder_monitor_names(st.session_state.temp_screens)
                    st.rerun()
            
            if st.button("Save PC", key="save_pc_btn"):
                if pc_name and st.session_state.temp_screens:
                    pc_id = f"pc{str(uuid.uuid4().hex[:4])}"
                    
                    # Create PC entry
                    config['pcs'][pc_id] = {
                        "name": pc_name,
                        "ip_address": ip_address,
                        "gpu_type": gpu_type,
                        "screens": st.session_state.temp_screens.copy()
                    }
                    
                    # Initialize screen_to_cameras mapping
                    if 'mappings' not in config:
                        config['mappings'] = {}
                    if 'screen_to_cameras' not in config['mappings']:
                        config['mappings']['screen_to_cameras'] = {}
                    
                    # Create mappings for each screen
                    config['mappings']['screen_to_cameras'][pc_id] = {}
                    for screen_id in st.session_state.temp_screens.keys():
                        config['mappings']['screen_to_cameras'][pc_id][screen_id] = create_screen_camera_mapping(screen_id)
                    
                    save_site_config(config)
                    st.success(f"PC '{pc_name}' added successfully.")
                    st.session_state.temp_screens = {}  # Clear temporary screens
                    time.sleep(0.5)
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
                new_name = st.text_input("PC Name (required)", value=pc_info.get("name", ""))
                new_ip = st.text_input("IP Address (optional)", value=pc_info.get("ip_address", ""))
                new_gpu = st.text_input("GPU Type (optional)", value=pc_info.get("gpu_type", ""))
                
                # Initialize edit screens in session state
                if "edit_screens" not in st.session_state:
                    st.session_state.edit_screens = pc_info.get("screens", {}).copy()
                
                # Add screen button
                if st.button("Add Screen"):
                    pc_id = st.session_state["edit_pc_id"]
                    screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                    next_monitor_num = get_next_monitor_number(st.session_state.edit_screens)
                    st.session_state.edit_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
                
                # Display existing screens
                st.markdown("### Screens")
                for screen_id, screen_info in st.session_state.edit_screens.items():
                    col1, col2 = st.columns([4, 1])
                    col1.write(screen_info["name"])
                    if col2.button("🗑️", key=f"delete_edit_{screen_id}"):
                        # Delete screen and its mappings
                        st.session_state.deleted_screens = st.session_state.get("deleted_screens", [])
                        st.session_state.deleted_screens.append(screen_id)
                        del st.session_state.edit_screens[screen_id]
                        if screen_id in config['mappings']['screen_to_cameras'][pc_id]:
                            del config['mappings']['screen_to_cameras'][pc_id][screen_id]
                            
                        st.session_state.edit_screens = reorder_monitor_names(st.session_state.edit_screens)
                        st.rerun()
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Save Changes", key="save_pc_changes", use_container_width=True):
                        if new_name and st.session_state.edit_screens:
                            pc_info["name"] = new_name
                            pc_info["ip_address"] = new_ip
                            pc_info["gpu_type"] = new_gpu
                            pc_info["screens"] = st.session_state.edit_screens.copy()
                            pc_info["n_screens"] = len(st.session_state.edit_screens)
                            
                            # Update screen mappings for any new screens
                            for screen_id in st.session_state.edit_screens.keys():
                                if screen_id not in config['mappings']['screen_to_cameras'][pc_id]:
                                    config['mappings']['screen_to_cameras'][pc_id][screen_id] = create_screen_camera_mapping(screen_id)
                            
                            # remove deleted screens from mappings
                            if "deleted_screens" in st.session_state:
                                for screen_id in st.session_state.deleted_screens:
                                    if screen_id in config['mappings']['screen_to_cameras'][pc_id]:
                                        del config['mappings']['screen_to_cameras'][pc_id][screen_id]
                                del st.session_state.deleted_screens
                                
                            
                            
                            config["pcs"][pc_id] = pc_info
                            save_site_config(config)
                            st.success("PC updated successfully.")
                            del st.session_state.edit_screens  # Clear edit screens
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
        header_cols = st.columns([3, 2, 3, 2, 2])
        header_cols[0].write("Name")
        header_cols[1].write("No. of Screens")
        header_cols[2].write("IP Address")
        header_cols[3].write("GPU Type")
        header_cols[4].write("Actions")

        for pc_id, pc_info in config["pcs"].items():
            row_cols = st.columns([3, 2, 3, 2, 2])
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
    else:
        st.info("No PCs available.")

pcs_page()