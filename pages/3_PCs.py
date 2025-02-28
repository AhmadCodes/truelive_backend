import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_site_config, save_site_config
import uuid
import time
from database import Database, PC, Screen
from utils.background_task import initialize_background_task, get_background_status

# Initialize the background task system
initialize_background_task()

db = Database()

def get_next_monitor_number(screens):
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
    sorted_screens = sorted(screens.items(), key=lambda x: x[1]["name"])
    for i, (screen_id, screen_info) in enumerate(sorted_screens, 1):
        screens[screen_id]["name"] = f"Monitor {i}"
    return screens

def pcs_page():
    st.set_page_config(page_title="PC Management", page_icon="🎥", layout="wide")
    st.title("PC Management")

    config = load_site_config()
    if "edit_pc_id" not in st.session_state:
        st.session_state["edit_pc_id"] = None

    existing_pcs = len(config.get("pcs", {}))

    cols = st.columns([8, 2])
    with cols[1]:
        add_pc_clicked = st.button("Add PC", key="add_pc_btn")

    add_pc_modal = Modal(key="add_pc_modal", title="Add New PC")
    if add_pc_clicked:
        if existing_pcs > 0:
            st.error("Multiple PC configuration is not currently supported in this version.")
            return
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
            
            if "temp_screens" not in st.session_state:
                st.session_state.temp_screens = {}
            
            if not st.session_state.get("add_pc_id"):
                st.session_state["add_pc_id"] = f"pc{uuid.uuid4().hex[:4]}"
            
            st.markdown("### Screens")
            if st.button("+ Add Screen"):
                pc_id = st.session_state.get("add_pc_id") or f"pc{uuid.uuid4().hex[:4]}"
                screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                next_monitor_num = get_next_monitor_number(st.session_state.temp_screens)
                st.session_state.temp_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
            
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
                if pc_name and st.session_state.temp_screens:
                    pc_id = st.session_state.get("add_pc_id") or f"pc{str(uuid.uuid4().hex[:4])}"
                    
                    pc = PC(pc_id, pc_name, ip_address, gpu_type)
                    db.add_pc(pc)
                    
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
                    
                    st.success(f"PC '{pc_name}' added successfully.")
                    st.session_state.temp_screens = {}
                    time.sleep(1)
                    st.session_state["add_pc_id"] = None
                    add_pc_modal.close()
                    st.rerun()
                else:
                    st.error("Please enter a PC name and add at least one screen.")

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
                
                col1, col2 = st.columns(2)
                with col1:
                    new_name = st.text_input("PC Name (required)", value=pc_info.get("name", ""))
                    new_ip = st.text_input("IP Address (optional)", value=pc_info.get("ip_address", ""))
                with col2:
                    new_gpu = st.text_input("GPU Type (optional)", value=pc_info.get("gpu_type", ""))
                
                if "edit_screens" not in st.session_state:
                    st.session_state.edit_screens = pc_info.get("screens", {}).copy()
                
                st.markdown("### Screens")
                if st.button("+ Add Screen"):
                    screen_id = f"{pc_id}_screen_{uuid.uuid4().hex[:8]}"
                    next_monitor_num = get_next_monitor_number(st.session_state.edit_screens)
                    st.session_state.edit_screens[screen_id] = create_screen_structure(screen_id, next_monitor_num)
                
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
                        if new_name and st.session_state.edit_screens:
                            pc = PC(pc_id, new_name, new_ip, new_gpu)
                            db.update_pc(pc)
                            
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
                            
                            st.success("PC updated successfully.")
                            del st.session_state.edit_screens
                            time.sleep(1)
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
                        db.delete_pc(pc_id)
                        st.success("PC deleted.")
                        time.sleep(1)
                        st.rerun()
            
            with st.expander(f"Screen Details for {pc_info.get('name', '')}"):
                for screen_id, screen_info in pc_info.get("screens", {}).items():
                    st.markdown(f"**{screen_info['name']}**")
                    cols = st.columns(3)
                    cols[0].write(f"Layout: {screen_info['layout']['rows']}x{screen_info['layout']['columns']}")
                    switching_interval = config.get('mappings', {}).get('screen_to_cameras', {}).get(pc_id, {}).get(screen_id, {}).get('switching_interval', 10)
                    cols[1].write(f"Switching Interval: {switching_interval}s")
    else:
        st.info("No PCs available.")

pcs_page()