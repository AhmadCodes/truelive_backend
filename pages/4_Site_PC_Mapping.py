import streamlit as st
from utils.config_loader import load_site_config, load_camera_config, save_site_config
import pandas as pd


# if 'logged_in' not in st.session_state or not st.session_state['logged_in']:
#     st.error("You need to log in first.")
#     st.stop()

def site_pc_mapping_page():
    st.title("Site to PC Mapping")
    
    site_config = load_site_config()
    camera_config = load_camera_config()
    
    # Create mapping
    with st.form("site_pc_mapping"):
        st.subheader("Map Site to PC")
        site = st.selectbox("Site", 
                          options=list(camera_config['sites'].keys()),
                          format_func=lambda x: camera_config['sites'][x]['name'])
        pc = st.selectbox("PC", 
                         options=list(site_config['pcs'].keys()),
                         format_func=lambda x: site_config['pcs'][x]['hostname'])
        
        if st.form_submit_button("Create Mapping"):
            if site not in site_config['mappings']['site_to_pc']:
                site_config['mappings']['site_to_pc'][site] = []
            if pc not in site_config['mappings']['site_to_pc'][site]:
                site_config['mappings']['site_to_pc'][site].append(pc)
                save_site_config(site_config)
                st.success(f"Mapped {camera_config['sites'][site]['name']} to {site_config['pcs'][pc]['hostname']}")

    # Show existing mappings
    mappings = []
    for site_id, pc_list in site_config['mappings']['site_to_pc'].items():
        site_name = camera_config['sites'][site_id]['name']
        for pc_id in pc_list:
            pc_info = site_config['pcs'][pc_id]
            mappings.append({
                'Site': site_name,
                'PC Hostname': pc_info['hostname'],
                'PC IP': pc_info['ip_address']
            })
    
    if mappings:
        st.dataframe(pd.DataFrame(mappings))
        
site_pc_mapping_page()