import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config
from utils.config_generator import generate_config
import time
from utils.websocket_client import send_config_sync
from database import Database, View, ScreenMapping, Screen, PC

@st.cache_resource
def get_db_instance():
    return Database()

def send_screen_mapping(config):
    """Send screen mapping configuration"""
    generated_config = generate_config(config)
    send_config_sync(generated_config)
    return True

def get_site_cameras(camera_config, site_id):
    """Get cameras for a specific site"""
    site_info = camera_config['sites'].get(site_id, {})
    cameras = []
    for cam_id, cam_info in site_info.get('cameras', {}).items():
        cameras.append({
            'camera_id': cam_id,
            'name': cam_info['name'],
            'rtsp_url': cam_info.get('rtsp_url', '')
        })
    return cameras

def create_empty_view(rows=3, columns=3):
    """Create an empty grid view"""
    empty_view = {}
    for row in range(1, rows + 1):
        for col in range(1, columns + 1):
            empty_view[f"slot_{row}_{col}"] = None
    return empty_view

def screen_layout_page():
    st.set_page_config(page_title="Screen Layout Configuration", layout="wide")
    st.title("Screen Layout Configuration")

    db = get_db_instance()
    camera_config = load_camera_config()

    # Initialize session states
    if 'selected_pc' not in st.session_state:
        st.session_state.selected_pc = None
    if 'selected_screen' not in st.session_state:
        st.session_state.selected_screen = None
    if 'selected_view' not in st.session_state:
        st.session_state.selected_view = None
    if 'edit_slot' not in st.session_state:
        st.session_state.edit_slot = None
    if 'selected_site' not in st.session_state:
        st.session_state.selected_site = None
    if 'current_view_config' not in st.session_state:
        st.session_state.current_view_config = None
    if 'show_save_button' not in st.session_state:
        st.session_state.show_save_button = False
    if 'editing_view_name' not in st.session_state:
        st.session_state.editing_view_name = None

    with st.sidebar:
        st.header("Navigation")
        if st.button("Configure Live View", type="primary"):
            # Generate config from database
            if st.session_state.selected_view:
                view = db.get_view_by_id(f"{st.session_state.selected_screen}_{st.session_state.selected_view}")
                if view:
                    config = db.get_view_config(view.id)
                    mapping_ret = send_screen_mapping(config)
                    if mapping_ret:
                        success = st.success("Live view configuration applied successfully!")
                        time.sleep(1)
                        success.empty()
                    else:
                        st.error("Failed to apply live view configuration!")

        # PC Selection
        pcs = db.get_pcs()
        if pcs:
            pc_options = [(pc.id, pc.name) for pc in pcs]
            selected_pc_index = st.selectbox(
                "Select PC",
                range(len(pc_options)),
                format_func=lambda x: f"🖥️ {pc_options[x][1]}",
                key="pc_selector"
            )
            current_pc_id, current_pc_name = pc_options[selected_pc_index]
            st.session_state.selected_pc = current_pc_id

            if st.session_state.selected_pc:
                screens = db.get_screens_by_pc(current_pc_id)
                screen_options = [(screen.id, screen.name) for screen in screens]
                
                selected_screen_index = st.selectbox(
                    "Select Screen",
                    range(len(screen_options)),
                    format_func=lambda x: f"📺 {screen_options[x][1]}",
                    key="screen_selector"
                )
                st.session_state.selected_screen = screen_options[selected_screen_index][0]

                rename_modal = Modal(key="rename_modal", title="Rename View")
                if st.session_state.editing_view_name and rename_modal.is_open():
                    with rename_modal.container():
                        old_name = st.session_state.editing_view_name
                        new_name = st.text_input("New Name", value=old_name)
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button("Save", use_container_width=True):
                                if new_name != old_name:
                                    db.update_view_name(old_name, new_name,
                                                     st.session_state.selected_screen)
                                    if st.session_state.selected_view == old_name:
                                        st.session_state.selected_view = new_name
                                    st.rerun()
                        with col2:
                            if st.button("Cancel", use_container_width=True):
                                st.session_state.editing_view_name = None
                                rename_modal.close()

                if st.session_state.selected_screen:
                    st.markdown("### Views")
                    screen = db.get_screen_by_id(st.session_state.selected_screen)
                    views = db.get_views_by_screen(st.session_state.selected_screen)

                    if st.button("➕ Add New View", type="primary"):
                        next_view_num = len(views) + 1
                        new_view_name = f"view_{next_view_num}"
                        new_view = View(
                            id=f"{st.session_state.selected_screen}_{new_view_name}",
                            screen_id=st.session_state.selected_screen,
                            name=new_view_name,
                            layout_rows=screen.rows,
                            layout_columns=screen.columns
                        )
                        db.add_view(new_view)
                        st.session_state.selected_view = new_view_name
                        st.rerun()

                    st.markdown("#### Available Views:")
                    for view in views:
                        with st.container():
                            cols = st.columns([3, 1, 1])
                            with cols[0]:
                                if st.button(f"👁️ {view.name}", 
                                           key=f"view_btn_{view.name}",
                                           use_container_width=True,
                                           type="secondary" if st.session_state.selected_view != view.name else "primary"):
                                    st.session_state.selected_view = view.name
                                    st.session_state.current_view_config = db.get_view_config(view.id)
                                    st.rerun()
                            
                            with cols[1]:
                                if st.button("✏️", key=f"edit_{view.name}", use_container_width=True):
                                    st.session_state.editing_view_name = view.name
                                    rename_modal.open()
                            
                            with cols[2]:
                                if st.button("🗑️", key=f"delete_{view.name}", use_container_width=True):
                                    db.delete_view(view.id)
                                    st.session_state.selected_view = None
                                    st.rerun()

    # Camera Selection Modal
    camera_modal = Modal(key="camera_select_modal", title="Select Camera")
    if st.session_state.edit_slot and camera_modal.is_open():
        with camera_modal.container():
            site_options = [(site_id, site_info['name']) 
                           for site_id, site_info in camera_config['sites'].items()]
            
            selected_site_index = st.selectbox(
                "Select Site",
                range(len(site_options)),
                format_func=lambda x: site_options[x][1]
            )
            total_sites = len(site_options)
            if total_sites == 0:
                st.error("No sites found in the site configuration!")
                time.sleep(2)
                camera_modal.close()
                
            selected_site_id = site_options[selected_site_index][0]
            selected_site_name = site_options[selected_site_index][1]
            
            cameras = get_site_cameras(camera_config, selected_site_id)
            if cameras:
                selected_camera_index = st.selectbox(
                    "Select Camera",
                    range(len(cameras)),
                    format_func=lambda x: cameras[x]['name']
                )
                selected_camera = cameras[selected_camera_index]
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Confirm", use_container_width=True):
                        view_id = f"{st.session_state.selected_screen}_{st.session_state.selected_view}"
                        row, col = map(int, st.session_state.edit_slot.split('_')[1:])
                        
                        mapping = ScreenMapping(
                            screen_id=st.session_state.selected_screen,
                            view_name=st.session_state.selected_view,
                            slot_row=row,
                            slot_col=col,
                            site_id=selected_site_id,
                            camera_id=selected_camera['camera_id']
                        )
                        db.add_screen_mapping(mapping)
                        
                        st.session_state.current_view_config = db.get_view_config(view_id)
                        st.session_state.edit_slot = None
                        st.session_state.show_save_button = True
                        camera_modal.close()
                        
                with col2:
                    if st.button("Clear Slot", use_container_width=True):
                        view_id = f"{st.session_state.selected_screen}_{st.session_state.selected_view}"
                        row, col = map(int, st.session_state.edit_slot.split('_')[1:])
                        db.delete_screen_mapping(st.session_state.selected_screen,
                                              st.session_state.selected_view,
                                              row, col)
                        st.session_state.current_view_config = db.get_view_config(view_id)
                        st.session_state.edit_slot = None
                        st.session_state.show_save_button = True
                        camera_modal.close()

    # Main content area
    if (st.session_state.selected_pc and st.session_state.selected_screen and 
            st.session_state.selected_view and st.session_state.current_view_config is not None):
        
        pc = db.get_pc_by_id(st.session_state.selected_pc)
        screen = db.get_screen_by_id(st.session_state.selected_screen)
        
        st.header("Layout Configuration")
        st.subheader(f"{pc.name} - {screen.name} - {st.session_state.selected_view}")
        
        # Create grid layout
        for row in range(1, screen.rows + 1):
            cols = st.columns(screen.columns)
            for col in range(1, screen.columns + 1):
                with cols[col-1]:
                    slot_name = f"slot_{row}_{col}"
                    current_slot = st.session_state.current_view_config.get(slot_name)
                    
                    with st.container():
                        st.markdown('----------------')
                        
                        if current_slot:
                            site = db.get_site_by_id(current_slot['site_id'])
                            camera = db.get_camera_by_id(current_slot['camera_id'])
                            
                            st.markdown(f"""
                                ### Slot {row}-{col}
                                
                                **Site:** {site.name if site else 'N/A'}  
                                **Camera:** {camera.name if camera else 'N/A'}
                                
                                **RTSP:** `{camera.rtsp_url if camera else 'N/A'}`
                            """)
                        else:
                            st.markdown(f"### Slot {row}-{col}\n\nEmpty")
                        
                        st.button("Select Camera", key=f"select_{slot_name}", 
                                on_click=lambda s=slot_name: [
                                    setattr(st.session_state, 'edit_slot', s),
                                    camera_modal.open()
                                ])

        # Action buttons
        col1, col2 = st.columns(2)
        with col1:
            if st.session_state.show_save_button:
                if st.button("Save View Configuration", type="primary"):
                    st.session_state.show_save_button = False
                    st.success("View configuration saved successfully!")
                    st.rerun()

if __name__ == "__main__":
    screen_layout_page()