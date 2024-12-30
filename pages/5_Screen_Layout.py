import streamlit as st
from utils.config_loader import load_site_config, load_camera_config, save_site_config
from utils.websocket_client import StreamAppClient
import pandas as pd
import asyncio

# if 'logged_in' not in st.session_state or not st.session_state['logged_in']:
#     st.error("You need to log in first.")
#     st.stop()

def screen_layout_page():
    
    st.set_page_config(
        page_title="Live View Camera Configuration System",
        page_icon="🎥",
        layout="wide"
    )
    st.title("Screen Layout Configuration")
    
    site_config = load_site_config()
    camera_config = load_camera_config()
    
    # Add new screen
    with st.form("new_screen"):
        st.subheader("Add New Screen")
        screen_name = st.text_input("Screen Name")
        layout = st.selectbox("Layout", ["2x2", "3x3", "4x4"])
        
        if st.form_submit_button("Add Screen"):
            screen_id = f"screen_{len(site_config['screens']) + 1}"
            site_config['screens'][screen_id] = {
                "name": screen_name,
                "layout": layout
            }
            save_site_config(site_config)
            st.success(f"Added screen: {screen_name}")
    
    # Map sites to screens
    with st.form("site_screen_mapping"):
        st.subheader("Map Site to Screen")
        site = st.selectbox("Site", 
                          options=list(camera_config['sites'].keys()),
                          format_func=lambda x: camera_config['sites'][x]['name'])
        screen = st.selectbox("Screen",
                            options=list(site_config['screens'].keys()),
                            format_func=lambda x: site_config['screens'][x]['name'])
        
        if st.form_submit_button("Create Mapping"):
            if site not in site_config['mappings']['site_to_screen']:
                site_config['mappings']['site_to_screen'][site] = []
            if screen not in site_config['mappings']['site_to_screen'][site]:
                site_config['mappings']['site_to_screen'][site].append(screen)
                save_site_config(site_config)
                
                # Send mapping to streaming application
                client = StreamAppClient(Config.STREAM_APP_WS_URL)
                asyncio.run(client.send_site_screen_mapping({
                    "site_id": site,
                    "screen_id": screen,
                    "cameras": list(camera_config['sites'][site]['cameras'].keys())
                }))
                
                st.success(f"Mapped {camera_config['sites'][site]['name']} to screen {site_config['screens'][screen]['name']}")

screen_layout_page()