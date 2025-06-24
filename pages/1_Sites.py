# pages/1_Sites.py
import streamlit as st
from streamlit_modal import Modal
from utils.config_loader import load_camera_config, save_camera_config
from database import Database, SiteCategoryMapping
import uuid
import time
from utils.background_task import initialize_background_task, get_background_status

# Initialize the background task system
initialize_background_task()

# Display logo
st.logo(
    "assets/Horizontal-Logo.png", 
    size="large",
    icon_image="assets/Logomark.png"
)

def check_user_permission(required_role=None):
    """
    Check if the current user has the required role.
    If required_role is None, just check if the user is logged in.
    """
    if 'user_id' not in st.session_state or not st.session_state['user_id']:
        st.warning("You must be logged in to access this page")
        st.stop()
    
    if required_role is None:
        return True
    
    user_role = st.session_state.get('user_role', '')
    
    if required_role == 'admin':
        if user_role not in ['admin', 'super_admin']:
            st.error("You don't have permission to access this feature")
            return False
    elif required_role == 'super_admin':
        if user_role != 'super_admin':
            st.error("You don't have permission to access this feature")
            return False
    
    return True

def sites_page():
    st.set_page_config(page_title="Site Management", page_icon="🎥", layout="wide")
    
    # Check if user is logged in
    if 'user_id' not in st.session_state or not st.session_state['user_id']:
        st.warning("Please log in to access this page")
        st.stop()

    user_role = st.session_state.get('user_role', '')
    is_read_only = user_role == 'user'

    st.title("Site Management")

    if is_read_only:
        st.info("You have read-only access to site information. Contact an administrator to make changes.")

    db = Database()
    config = load_camera_config()

    # --- CATEGORY LOGIC ---
    # Ensure default category exists (white, 0xFFFFFFFF)
    default_color = 0xFFFFFFFF
    default_category_name = "Default"
    categories = db.get_all_site_categories()
    default_category = next((c for c in categories if c.color == default_color), None)
    if not default_category:
        import uuid as _uuid
        # Check if we have any categories to get the type from
        if categories:
            category_type = type(categories[0])
        else:
            # If no categories exist, import the class directly
            from database import SiteCategory
            category_type = SiteCategory
            
        default_category = db.add_site_category(
            category_type(
                id=str(_uuid.uuid4()),
                name=default_category_name,
                color=default_color
            )
        )
        categories = db.get_all_site_categories()
        default_category = next((c for c in categories if c.color == default_color), None)

    # Ensure every site has a category mapping
    all_sites = db.get_all_sites() if hasattr(db, 'get_all_sites') else []
    for site in all_sites:
        cats = db.get_site_categories_for_site(site.id)
        if not cats:
            db.add_site_category_mapping(type(db.get_site_categories_for_site(site.id))[0](site_id=site.id, category_id=default_category.id))

    # --- TABS LAYOUT ---
    tab_config, tab_categories = st.tabs(["Config", "Categories"])

    # --- CONFIG TAB ---
    with tab_config:
        # Place the "Add New Site" button on the right side (only for admins and super admins)
        if not is_read_only:
            btn_cols = st.columns([9, 1])
            with btn_cols[1]:
                add_site_clicked = st.button("Add New Site", key="add_site_btn", help="Add a new site")
        else:
            add_site_clicked = False

        # ----- Add Site Modal -----
        add_site_modal = Modal(key="add_site_modal", title="Add New Site")
        if add_site_clicked:
            add_site_modal.open()
        if add_site_modal.is_open():
            with add_site_modal.container():
                st.subheader("Add New Site")
                site_name = st.text_input("Site Name", key="new_site_name")
                nvr_username = st.text_input("NVR Username", key="new_site_nvr_username")
                nvr_password = st.text_input("NVR Password", type="password", key="new_site_nvr_password")
                if st.button("Submit", key="submit_new_site"):
                    if site_name and nvr_username and nvr_password:
                        site_id = "SITE_" + str(uuid.uuid4())
                        config["sites"][site_id] = {
                            "name": site_name,
                            "nvr_username": nvr_username,
                            "nvr_password": nvr_password,
                            "cameras": {}
                        }
                        save_camera_config(config)
                        st.success(f"Added site: {site_name}")
                        time.sleep(0.5)
                        add_site_modal.close()
                        st.rerun()
                    else:
                        st.error("Please fill in all fields.")

        # ----- Display Sites Table with Category Dropdown -----
        if config["sites"]:
            st.markdown("### All Sites")
            header_cols = st.columns([3, 2, 3, 3])
            header_cols[0].markdown("**Site Name**")
            header_cols[1].markdown("**No. of Cameras**")
            header_cols[2].markdown("**Category**")
            header_cols[3].markdown("**Actions**")
            for sid, info in config["sites"].items():
                row_cols = st.columns([3, 2, 3, 3])
                with row_cols[0]:
                    view_clicked = st.button(info["name"], key=f"view_{sid}", help="View Site Details")
                row_cols[1].write(len(info["cameras"]))
                # Category dropdown
                with row_cols[2]:
                    site_cats = db.get_site_categories_for_site(sid)
                    current_cat = site_cats[0] if site_cats else default_category
                    cat_options = [f"{c.name} [{hex(c.color)}]" for c in categories]
                    cat_colors = {f"{c.name} [{hex(c.color)}]": c for c in categories}
                    
                    # Create columns for side-by-side layout
                    dd_col, color_col = st.columns([4, 1])
                    with dd_col:
                        selected = st.selectbox(
                            "Category", 
                            cat_options,
                            index=cat_options.index(f"{current_cat.name} [{hex(current_cat.color)}]") if current_cat else 0,
                            key=f"cat_select_{sid}",
                            label_visibility="collapsed"
                        )
                    with color_col:
                        color_hex = f"#{current_cat.color & 0xFFFFFF:06X}" if current_cat else "#FFFFFF"
                        # Custom CSS for better color preview
                        st.markdown(f"""
                        <div style="display: flex; align-items: center; height: 38px; margin-top: 5px;">                        
                            <div style="
                                width: 28px;
                                height: 28px;
                                background-color: {color_hex};
                                border-radius: 4px;
                                border: 1px solid rgba(0,0,0,0.1);
                                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                                display: inline-block;
                                vertical-align: middle;
                            "></div>
                        </div>
                        """, unsafe_allow_html=True)
                    # Handle category change
                    if not is_read_only:
                        if st.session_state.get(f"cat_select_{sid}") != f"{current_cat.name} [{hex(current_cat.color)}]":
                            new_cat = cat_colors[st.session_state[f"cat_select_{sid}"]]
                            db.delete_all_category_mappings_for_site(sid)
                            db.add_site_category_mapping(SiteCategoryMapping(site_id=sid, category_id=new_cat.id))
                            st.success("Category updated!")
                            st.rerun()
                # Actions
                with row_cols[3]:
                    if not is_read_only:
                        icon_edit_col, icon_delete_col = st.columns(2)
                        with icon_edit_col:
                            edit_clicked = st.button("✏️", key=f"edit_{sid}", help="Edit Site")
                        with icon_delete_col:
                            delete_clicked = st.button("🗑️", key=f"delete_{sid}", help="Delete Site")
                    else:
                        edit_clicked = False
                        delete_clicked = False
                if view_clicked:
                    st.session_state["view_site_id"] = sid
                    # view_site_modal.open()  # Add modal logic as needed
                if edit_clicked:
                    st.session_state["edit_site_id"] = sid
                    # edit_site_modal.open()  # Add modal logic as needed
                if delete_clicked:
                    # Delete logic here (reuse your existing logic)
                    st.success("Site deleted (implement logic)")
                    st.rerun()

    # --- CATEGORIES TAB ---
    with tab_categories:
        st.markdown("### Manage Categories")
        cat_add_col, cat_spacer, cat_add_btn_col = st.columns([6, 3, 1])
        with cat_add_btn_col:
            add_cat_clicked = st.button("Add Category", key="add_cat_btn")
        # Add Category Modal
        add_cat_modal = Modal(key="add_cat_modal", title="Add Category")
        if add_cat_clicked:
            add_cat_modal.open()
        if add_cat_modal.is_open():
            with add_cat_modal.container():
                st.subheader("Add Category")
                cat_name = st.text_input("Category Name", key="new_cat_name")
                cat_color = st.color_picker("Category Color", value="#FFFFFF", key="new_cat_color")
                if st.button("Submit", key="submit_new_cat"):
                    if cat_name:
                        import uuid as _uuid
                        color_int = int(cat_color.replace("#", "0xFF"), 16) if cat_color.startswith("#") else default_color
                        db.add_site_category(type(db.get_all_site_categories()[0])(id=str(_uuid.uuid4()), name=cat_name, color=color_int))
                        st.success("Category added!")
                        add_cat_modal.close()
                        st.rerun()
                    else:
                        st.error("Please enter a category name.")
        # Show Categories Table
        cat_table_cols = st.columns([4, 2, 2, 2])
        cat_table_cols[0].markdown("**Category Name**")
        cat_table_cols[1].markdown("**Color**")
        cat_table_cols[2].markdown("**Edit**")
        cat_table_cols[3].markdown("**Delete**")
        for cat in categories:
            row = st.columns([4, 2, 2, 2])
            row[0].write(cat.name)
            color_hex = f"#{cat.color & 0xFFFFFF:06X}"
            row[1].markdown(f"<div style='width:30px;height:20px;background:{color_hex};border:1px solid #ccc;'></div>", unsafe_allow_html=True)
            with row[2]:
                if st.button("Edit", key=f"edit_cat_{cat.id}"):
                    st.session_state["edit_cat_id"] = cat.id
                    st.session_state["edit_cat_name"] = cat.name
                    st.session_state["edit_cat_color"] = color_hex
                    st.session_state["edit_cat_modal_open"] = True
            with row[3]:
                if st.button("Delete", key=f"delete_cat_{cat.id}"):
                    db.delete_site_category(cat.id)
                    st.success("Category deleted!")
                    st.rerun()
        # Edit Category Modal
        if st.session_state.get("edit_cat_modal_open"):
            edit_cat_modal = Modal(key="edit_cat_modal", title="Edit Category")
            edit_cat_modal.open()
            if edit_cat_modal.is_open():
                with edit_cat_modal.container():
                    st.subheader("Edit Category")
                    edit_cat_name = st.text_input("Category Name", value=st.session_state.get("edit_cat_name", ""), key="edit_cat_name_input")
                    edit_cat_color = st.color_picker("Category Color", value=st.session_state.get("edit_cat_color", "#FFFFFF"), key="edit_cat_color_input")
                    if st.button("Save Changes", key="save_cat_changes"):
                        color_int = int(edit_cat_color.replace("#", "0xFF"), 16) if edit_cat_color.startswith("#") else default_color
                        db.update_site_category(st.session_state["edit_cat_id"], name=edit_cat_name, color=color_int)
                        st.success("Category updated!")
                        st.session_state["edit_cat_modal_open"] = False
                        st.rerun()
                    if st.button("Cancel", key="cancel_cat_edit"):
                        st.session_state["edit_cat_modal_open"] = False
                        st.rerun()

sites_page()