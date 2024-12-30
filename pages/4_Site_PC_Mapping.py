import streamlit as st
import time
from streamlit_modal import Modal
from utils.config_loader import load_site_config, save_site_config, load_camera_config

def site_pc_mapping_page():
    st.set_page_config(
        page_title="Live View Camera Configuration System",
        page_icon="🎥",
        layout="wide"
    )
    st.title("PC-to-Site Mapping")

    site_config = load_site_config()
    camera_config = load_camera_config()

    pcs_data = site_config.setdefault("pcs", {})
    site_to_pc_map = site_config.setdefault("mappings", {}).setdefault("site_to_pc", {})

    if not pcs_data:
        st.info("No PCs found.")
        return

    # Session state for the modal
    if "pc_modal_open" not in st.session_state:
        st.session_state["pc_modal_open"] = False
    if "pc_modal_id" not in st.session_state:
        st.session_state["pc_modal_id"] = None

    # Modal definition
    site_add_modal = Modal(key="site_add_modal", title="Map Site to PC")

    # If we have set values, open the modal
    if (st.session_state["pc_modal_open"] 
        and st.session_state["pc_modal_id"]
        and not site_add_modal.is_open()
        ):
        site_add_modal.open()

    st.markdown("Below is a list of all PCs. Each PC can be expanded to view details and manage associated sites.")

    for pc_id, pc_info in pcs_data.items():
        with st.expander(f"{pc_info.get('name', pc_id)}"):
            st.write(f"IP Address: {pc_info.get('ip_address', '')}")
            st.write(f"GPU Type: {pc_info.get('gpu_type', '')}")
            st.write(f"Number of Screens: {pc_info.get('screens', '')}")

            # "Add Site" button at the top of the mapped sites section
            if st.button("Add Site", key=f"open_modal_{pc_id}"):
                st.session_state["pc_modal_open"] = True
                st.session_state["pc_modal_id"] = pc_id
                site_add_modal.open()
                st.rerun()

            # Show already-mapped sites for this PC
            st.markdown("**Mapped Sites:**")
            mapped_sites_to_this_pc = [
                s_id for s_id, mapped_pcs in site_to_pc_map.items() if pc_id in mapped_pcs
            ]
            if mapped_sites_to_this_pc:
                for s_id in mapped_sites_to_this_pc:
                    site_name = camera_config["sites"][s_id]["name"] if s_id in camera_config["sites"] else s_id
                    cols = st.columns([6, 2])
                    cols[0].write(site_name)
                    with cols[1]:
                        if st.button("Remove", key=f"remove_{s_id}_{pc_id}"):
                            if pc_id in site_to_pc_map[s_id]:
                                site_to_pc_map[s_id].remove(pc_id)
                                save_site_config(site_config)
                                st.success(f"Removed site '{site_name}' from PC '{pc_info['name']}'.")
                                time.sleep(0.5)
                                st.rerun()
            else:
                st.write("*No sites mapped to this PC yet.*")

    # Modal content
    if site_add_modal.is_open() and st.session_state["pc_modal_id"]:
        with site_add_modal.container():
            pc_id = st.session_state["pc_modal_id"]
            pc_info = pcs_data.get(pc_id, {})
            st.subheader(f"Map Site to PC: {pc_info.get('name', pc_id)}")

            sites_dict = camera_config.get("sites", {})
            if sites_dict:
                site_options = list(sites_dict.keys())
                selected_site = st.selectbox(
                    "Select a site to map:",
                    options=site_options,
                    format_func=lambda s: sites_dict[s]["name"] if s in sites_dict else s,
                    key="modal_site_select"
                )
                if st.button("Add Site to PC", key="modal_add_site_btn"):
                    # Ensure the list for the site is initialized
                    site_to_pc_map.setdefault(selected_site, [])
                    if pc_id in site_to_pc_map[selected_site]:
                        st.warning("This site is already mapped to this PC.")
                    else:
                        site_to_pc_map[selected_site].append(pc_id)
                        save_site_config(site_config)
                        st.success(f"Site '{sites_dict[selected_site]['name']}' mapped to PC '{pc_info.get('name', pc_id)}'.")
                        time.sleep(0.5)
                        st.rerun()
            else:
                st.warning("No sites available in camera_config.")

            if st.button("Close", key="close_modal_btn"):
                st.session_state["pc_modal_open"] = False
                st.session_state["pc_modal_id"] = None
                site_add_modal.close()
                st.rerun()

def main():
    site_pc_mapping_page()

if __name__ == "__main__":
    main()