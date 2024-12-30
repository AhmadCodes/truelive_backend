import streamlit as st
import pandas as pd
import time
from streamlit_modal import Modal
from utils.config_loader import load_site_config, save_site_config

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
                    pc_id = f"pc_{len(config['pcs']) + 1}"
                    config['pcs'][pc_id] = {
                        "name": pc_name,
                        "screens": num_screens,
                        "ip_address": ip_address,
                        "gpu_type": gpu_type
                    }
                    save_site_config(config)
                    st.success(f"PC '{pc_name}' added successfully.")
                    time.sleep(0.5)
                    add_pc_modal.close()
                    st.rerun()
                else:
                    st.error("Please enter a valid PC name and number of screens.")

    # ----- Edit PC Modal -----
    edit_pc_modal = Modal(key="edit_pc_modal", title="Edit PC")
    
    # Move modal opening logic here, before the modal content
    if st.session_state["edit_pc_id"] and not edit_pc_modal.is_open():
        edit_pc_modal.open()

    if edit_pc_modal.is_open():  # Remove the additional condition here
        with edit_pc_modal.container():
            pc_id = st.session_state["edit_pc_id"]
            pc_info = config["pcs"].get(pc_id, {})
            if pc_info:
                st.subheader(f"Edit PC: {pc_info.get('name', '')}")
                new_name = st.text_input("PC Name (required)", value=pc_info.get("name", ""))
                new_screens = st.number_input("Number of Screens (required)", min_value=1, step=1, value=pc_info.get("screens", 1))
                new_ip = st.text_input("IP Address (optional)", value=pc_info.get("ip_address", ""))
                new_gpu = st.text_input("GPU Type (optional)", value=pc_info.get("gpu_type", ""))
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Save Changes", key="save_pc_changes", use_container_width=True):
                        if new_name and new_screens > 0:
                            pc_info["name"] = new_name
                            pc_info["screens"] = new_screens
                            pc_info["ip_address"] = new_ip
                            pc_info["gpu_type"] = new_gpu
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
        # Create a header row
        header_cols = st.columns([3, 2, 3, 2, 2])
        header_cols[0].write("Name")
        header_cols[1].write("No. of Screens")
        header_cols[2].write("IP Address")
        header_cols[3].write("GPU Type")
        header_cols[4].write("Actions")

        # Populate rows
        for pc_id, pc_info in config["pcs"].items():
            row_cols = st.columns([3, 2, 3, 2, 2])
            row_cols[0].write(pc_info.get("name", ""))
            row_cols[1].write(pc_info.get("screens", ""))
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
                        save_site_config(config)
                        st.success("PC deleted.")
                        time.sleep(0.5)
                        st.rerun()
    else:
        st.info("No PCs available.")

pcs_page()