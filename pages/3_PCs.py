import streamlit as st
from utils.config_loader import load_site_config, save_site_config
import pandas as pd


# if 'logged_in' not in st.session_state or not st.session_state['logged_in']:
#     st.error("You need to log in first.")
#     st.stop()

def pcs_page():
    st.title("PC Management")
    
    config = load_site_config()
    
    # Add new PC
    with st.form("new_pc"):
        st.subheader("Add New PC")
        hostname = st.text_input("Hostname")
        ip_address = st.text_input("IP Address")
        gpu_type = st.selectbox("GPU Type", ["NVIDIA", "Intel"])
        
        if st.form_submit_button("Add PC"):
            pc_id = f"pc_{len(config['pcs']) + 1}"
            config['pcs'][pc_id] = {
                "hostname": hostname,
                "ip_address": ip_address,
                "gpu_type": gpu_type
            }
            save_site_config(config)
            st.success(f"Added PC: {hostname}")
    
    # List existing PCs
    if config['pcs']:
        pc_data = pd.DataFrame([{
            'PC ID': pc_id,
            'Hostname': pc_info['hostname'],
            'IP Address': pc_info['ip_address'],
            'GPU Type': pc_info['gpu_type']
        } for pc_id, pc_info in config['pcs'].items()])
        st.dataframe(pc_data)
        
pcs_page()