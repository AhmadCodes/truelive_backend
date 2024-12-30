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

def create_screen_structure(pc_id, num_screens):
    """Create the screen structure for a new PC"""
    screens = {}
    for i in range(1, num_screens + 1):
        screen_id = f"{pc_id}_screen_{i}"
        screens[screen_id] = {
            "name": f"Monitor {i}"
        }
    return screens

def create_screen_camera_mapping(pc_id, num_screens):
    """Create empty camera mappings for all screens with views"""
    screen_mappings = {}
    for i in range(1, num_screens + 1):
        screen_id = f"{pc_id}_screen_{i}"
        screen_mappings[screen_id] = {
            "view_1": create_empty_slot_mapping()
        }
    return screen_mappings

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
            num_screens = st.number_input("Number of Screens (required)", min_value=1, step=1)
            ip_address = st.text_input("IP Address (optional)")
            gpu_type = st.text_input("GPU Type (optional)")
            if st.button("Save PC", key="save_pc_btn"):
                if pc_name and num_screens > 0:
                    pc_id = f"pc{str(uuid.uuid4().hex[:4])}"  # Shorter PC ID
                    
                    # Create PC entry
                    config['pcs'][pc_id] = {
                        "name": pc_name,
                        "n_screens": num_screens,
                        "ip_address": ip_address,
                        "gpu_type": gpu_type,
                        "screens": create_screen_structure(pc_id, num_screens)
                    }
                    
                    # Initialize screen_to_cameras mapping
                    if 'mappings' not in config:
                        config['mappings'] = {}
                    if 'screen_to_cameras' not in config['mappings']:
                        config['mappings']['screen_to_cameras'] = {}
                    
                    config['mappings']['screen_to_cameras'][pc_id] = create_screen_camera_mapping(pc_id, num_screens)
                    
                    save_site_config(config)
                    st.success(f"PC '{pc_name}' added successfully.")
                    time.sleep(0.5)
                    add_pc_modal.close()
                    st.rerun()
                else:
                    st.error("Please enter a valid PC name and number of screens.")

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
                old_screens = pc_info.get("n_screens", 1)
                new_screens = st.number_input("Number of Screens (required)", min_value=1, step=1, value=old_screens)
                new_ip = st.text_input("IP Address (optional)", value=pc_info.get("ip_address", ""))
                new_gpu = st.text_input("GPU Type (optional)", value=pc_info.get("gpu_type", ""))
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Save Changes", key="save_pc_changes", use_container_width=True):
                        if new_name and new_screens > 0:
                            pc_info["name"] = new_name
                            pc_info["n_screens"] = new_screens
                            pc_info["ip_address"] = new_ip
                            pc_info["gpu_type"] = new_gpu
                            
                            # Update screens structure if number of screens changed
                            if new_screens != old_screens:
                                pc_info["screens"] = create_screen_structure(pc_id, new_screens)
                                config['mappings']['screen_to_cameras'][pc_id] = create_screen_camera_mapping(pc_id, new_screens)
                            
                            config["pcs"][pc_id] = pc_info
                            save_site_config(config)
                            st.success("PC updated successfully.")
                            time.sleep(0.5)
                            st.session_state["edit_pc_id"] = None
                            edit_pc_modal.close()
                            st.rerun()
                        else:
                            st.error("Please check required fields.")
                with col2:
                    if st.button("Cancel", key="cancel_edit", use_container_width=True):
                        st.session_state["edit_pc_id"] = None
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
            row_cols[1].write(pc_info.get("n_screens", ""))
            row_cols[2].write(pc_info.get("ip_address", ""))
            row_cols[3].write(pc_info.get("gpu_type", ""))

            with row_cols[4]:
                col_edit, col_delete = st.columns([1, 1], gap="small")
                with col_edit:
                    if st.button("✏️", key=f"edit_{pc_id}"):
                        st.session_state["edit_pc_id"] = pc_id
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