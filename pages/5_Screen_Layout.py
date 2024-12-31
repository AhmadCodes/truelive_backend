import streamlit as st
from streamlit_modal import Modal
import json
from utils.config_loader import load_site_config, load_camera_config, save_site_config
from utils.config_generator import generate_config

def send_screen_mapping(mapping_json):
    """Dummy function to send screen mapping configuration"""
    # This function will be implemented by you later
    config = generate_config(mappings)

    # Save config.json
    with open("config.json", "w") as file:
        json.dump(config, file, indent=4)
    pass

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

def create_empty_view():
    """Create an empty 3x3 grid view"""
    empty_view = {}
    for row in range(1, 4):
        for col in range(1, 4):
            empty_view[f"slot_{row}_{col}"] = None
    return empty_view

def screen_layout_page():
    st.set_page_config(page_title="Screen Layout Configuration", layout="wide")
    st.title("Screen Layout Configuration")

    # Load configurations
    site_config = load_site_config()
    camera_config = load_camera_config()

    # Custom CSS for grid layout
    st.markdown("""
        <style>
        .grid-slot {
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 10px;
            margin: 5px;
            background-color: #f8f9fa;
        }
        .grid-slot:hover {
            background-color: #f0f1f2;
        }
        </style>
    """, unsafe_allow_html=True)

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

    # Create sidebar for navigation
    with st.sidebar:
        st.header("Navigation")
        
        # PC Selection
        pc_options = [(pc_id, pc_info['name']) for pc_id, pc_info in site_config['pcs'].items()]
        if pc_options:
            selected_pc_index = st.selectbox(
                "Select PC",
                range(len(pc_options)),
                format_func=lambda x: f"🖥️ {pc_options[x][1]}",
                key="pc_selector"
            )
            current_pc_id, current_pc_name = pc_options[selected_pc_index]
            st.session_state.selected_pc = current_pc_id

            # Screen Selection
            if st.session_state.selected_pc:
                pc_info = site_config['pcs'][current_pc_id]
                # screen_ids = [f"{current_pc_id}_screen_{i}" for i in range(1, pc_info['n_screens'] + 1)]
                mapping = site_config['mappings']['screen_to_cameras'].get(current_pc_id, {})
                screen_ids = list(mapping.keys())
                selected_screen_index = st.selectbox(
                    "Select Screen",
                    range(len(screen_ids)),
                    format_func=lambda x: f"📺 Screen {x + 1}",
                    key="screen_selector"
                )
                st.session_state.selected_screen = screen_ids[selected_screen_index]
                
                # Add rename modal
                rename_modal = Modal(key="rename_modal", title="Rename View")
                if st.session_state.editing_view_name and rename_modal.is_open():
                    with rename_modal.container():
                        view_name = st.session_state.editing_view_name
                        new_name = st.text_input("New Name", value=view_name)
                        views = site_config['mappings']['screen_to_cameras'][current_pc_id][st.session_state.selected_screen]
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button("Save", use_container_width=True):
                                if new_name != view_name and new_name not in views:
                                    views[new_name] = views.pop(view_name)
                                    if st.session_state.selected_view == view_name:
                                        st.session_state.selected_view = new_name
                                    save_site_config(site_config)
                                st.session_state.editing_view_name = None
                                rename_modal.close()
                                st.rerun()
                        with col2:
                            if st.button("Cancel", use_container_width=True):
                                st.session_state.editing_view_name = None
                                rename_modal.close()

                # View Management
                if st.session_state.selected_screen:
                    st.markdown("### Views")
                    if 'mappings' not in site_config:
                        site_config['mappings'] = {'screen_to_cameras': {}}
                    if 'screen_to_cameras' not in site_config['mappings']:
                        site_config['mappings']['screen_to_cameras'] = {}
                    if current_pc_id not in site_config['mappings']['screen_to_cameras']:
                        site_config['mappings']['screen_to_cameras'][current_pc_id] = {}
                    screen_mappings = site_config['mappings']['screen_to_cameras'].get(current_pc_id, {})
                    
                    if st.session_state.selected_screen not in screen_mappings:
                        screen_mappings[st.session_state.selected_screen] = {}
                    
                    views = screen_mappings.get(st.session_state.selected_screen, {})

                    # Add new view button
                    if st.button("➕ Add New View", type="primary"):
                        next_view_num = len(views) + 1
                        new_view_name = f"view_{next_view_num}"
                        views[new_view_name] = create_empty_view()
                        save_site_config(site_config)
                        st.rerun()

                    # List all views with edit/delete buttons
                    st.markdown("#### Available Views:")
                    for view_name in views.keys():
                        cols = st.columns([3, 1, 1])
                        
                        with cols[0]:
                            if st.session_state.editing_view_name == view_name:
                                new_name = st.text_input("Name", value=view_name, key=f"rename_{view_name}")
                                if new_name != view_name and new_name not in views:
                                    views[new_name] = views.pop(view_name)
                                    if st.session_state.selected_view == view_name:
                                        st.session_state.selected_view = new_name
                                    save_site_config(site_config)
                                    st.session_state.editing_view_name = None
                                    st.rerun()
                            else:
                                if st.button(f"👁️ {view_name}", key=f"view_btn_{view_name}", 
                                        use_container_width=True,
                                        type="secondary" if st.session_state.selected_view != view_name else "primary"):
                                    st.session_state.selected_view = view_name
                                    st.session_state.current_view_config = dict(views[view_name])
                        
                            with cols[1]:
                                if st.button("✏️", key=f"edit_{view_name}", use_container_width=True):
                                    st.session_state.editing_view_name = view_name
                                    rename_modal.open()
                        
                        with cols[2]:
                            if st.button("🗑️", key=f"delete_{view_name}", use_container_width=True):
                                del views[view_name]
                                save_site_config(site_config)
                                st.session_state.selected_view = None
                                st.rerun()

    # Camera Selection Modal
    camera_modal = Modal(key="camera_select_modal", title="Select Camera")
    if st.session_state.edit_slot and camera_modal.is_open():
        with camera_modal.container():
            # Site selection
            site_options = [(site_id, site_info['name']) 
                           for site_id, site_info in camera_config['sites'].items()]
            
            selected_site_index = st.selectbox(
                "Select Site",
                range(len(site_options)),
                format_func=lambda x: site_options[x][1]
            )
            selected_site_id = site_options[selected_site_index][0]
            selected_site_name = site_options[selected_site_index][1]
            
            # Camera selection for selected site
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
                        st.session_state.current_view_config[st.session_state.edit_slot] = {
                            'site_id': selected_site_id,
                            'site_name': selected_site_name,
                            'camera_id': selected_camera['camera_id'],
                            'camera_name': selected_camera['name'],
                            'rtsp_url': selected_camera['rtsp_url']
                        }
                        st.session_state.edit_slot = None
                        st.session_state.show_save_button = True
                        camera_modal.close()
                with col2:
                    if st.button("Clear Slot", use_container_width=True):
                        st.session_state.current_view_config[st.session_state.edit_slot] = None
                        st.session_state.edit_slot = None
                        st.session_state.show_save_button = True
                        camera_modal.close()

    # Main content area
    if (st.session_state.selected_pc and st.session_state.selected_screen and 
            st.session_state.selected_view and st.session_state.current_view_config):
        
        st.header(f"Layout Configuration")
        st.subheader(f"{site_config['pcs'][st.session_state.selected_pc]['name']} - Screen {selected_screen_index + 1} - {st.session_state.selected_view}")
        
        # Create 3x3 grid layout with borders
        for row in range(1, 4):
            cols = st.columns(3)
            for col in range(1, 4):
                with cols[col-1]:
                    slot_name = f"slot_{row}_{col}"
                    current_slot = st.session_state.current_view_config.get(slot_name)
                    
                    # Create a container with border for each slot
                    with st.container():
                        st.markdown(f'---------', unsafe_allow_html=True)
                        
                        # Display current camera info or empty slot
                        if current_slot:
                            st.markdown(f"""
                                ### Slot {row}-{col}
                                
                                **Site:** {current_slot['site_name']} | 
                                **Camera:** {current_slot['camera_name']}
                                
                                **RTSP:** `{current_slot['rtsp_url']}`
                            """)
                        else:
                            st.markdown(f"**Slot {row}-{col}**\n\nEmpty")
                        
                        if st.button("Select Camera", key=f"select_{slot_name}"):
                            st.session_state.edit_slot = slot_name
                            camera_modal.open()
                        st.markdown('</div>', unsafe_allow_html=True)

        # Save button
        if st.session_state.show_save_button:
            if st.button("Save View Configuration", type="primary"):
                screen_mappings = site_config['mappings']['screen_to_cameras'][st.session_state.selected_pc][st.session_state.selected_screen]
                screen_mappings[st.session_state.selected_view] = st.session_state.current_view_config
                save_site_config(site_config)
                send_screen_mapping(site_config['mappings']['screen_to_cameras'])
                st.session_state.show_save_button = False
                st.success("View configuration saved successfully!")
                st.rerun()

if __name__ == "__main__":
    screen_layout_page()