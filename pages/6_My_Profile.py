import streamlit as st
import logging
import time
import hashlib
from database import Database
from utils.auth_utils import check_authentication, get_current_user, logout_user
from utils.layout import create_header, create_footer, display_user_info, add_notification

# Configure logging
logger = logging.getLogger(__name__)

st.set_page_config(page_title="My Profile", page_icon="👤", layout="centered")

# Check authentication
check_authentication()

# Display user info in sidebar
display_user_info()

# Get current user
current_user = get_current_user()
if not current_user:
    st.error("Could not retrieve your user information. Please try logging in again.")
    if st.button("Go to Login", use_container_width=True):
        logout_user()
    st.stop()

# Create header
create_header(
    title="👤 My Profile",
    subtitle="View and update your account settings"
)

# User information section
st.subheader("Account Information")

# Display user information
# Format last login time handling both timestamp and datetime string formats
last_login_str = "Never"
if current_user.last_login:
    try:
        # Try parsing as datetime string first
        last_login_str = time.strftime('%Y-%m-%d %H:%M:%S', 
            time.strptime(current_user.last_login.split('.')[0], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        try:
            # Try parsing as timestamp
            last_login_str = time.strftime('%Y-%m-%d %H:%M:%S', 
                time.localtime(float(current_user.last_login)))
        except:
            last_login_str = str(current_user.last_login)

created_at_str = "Unknown"
if current_user.created_at:
    try:
        # Try parsing as datetime string first
        created_at_str = time.strftime('%Y-%m-%d %H:%M:%S',
            time.strptime(current_user.created_at.split('.')[0], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        try:
            # Try parsing as timestamp
            created_at_str = time.strftime('%Y-%m-%d %H:%M:%S',
                time.localtime(float(current_user.created_at)))
        except:
            created_at_str = str(current_user.created_at)

st.info(f"""
**Username:** {current_user.username}  
**Email:** {current_user.email}  
**Role:** {current_user.role.capitalize()}  
**Last Login:** {last_login_str}  
**Account Created:** {created_at_str}
""")

# Layout with tabs
tab1, tab2 = st.tabs(["Update Profile", "Change Password"])

# Tab 1: Update profile
with tab1:
    st.subheader("Update Your Information")
    
    # Profile update form
    with st.form("update_profile_form"):
        new_email = st.text_input("Email", value=current_user.email)
        
        submitted = st.form_submit_button("Update Profile", use_container_width=True)
        
        if submitted:
            try:
                if not new_email:
                    add_notification("Email is required", type="error")
                elif new_email != current_user.email:
                    # Update email
                    db = Database()
                    
                    # Check if email is already in use
                    existing_user = db.get_user_by_email(new_email)
                    if existing_user and existing_user.id != current_user.id:
                        add_notification("This email is already in use by another account", type="error")
                    else:
                        # Update user
                        db.update_user(
                            user_id=current_user.id,
                            email=new_email
                        )
                        
                        add_notification("Profile updated successfully!", type="success")
                        time.sleep(1)
                        st.rerun()
                else:
                    add_notification("No changes were made", type="info")
            except Exception as e:
                logger.error(f"Error updating profile: {e}")
                add_notification(f"An error occurred: {e}", type="error")

# Tab 2: Change password
with tab2:
    st.subheader("Change Your Password")
    
    # Password change form
    with st.form("change_password_form"):
        current_password = st.text_input("Current Password", type="password")
        new_password = st.text_input("New Password", type="password", 
                                    help="Password must be at least 8 characters long")
        confirm_password = st.text_input("Confirm New Password", type="password")
        
        submitted = st.form_submit_button("Change Password", use_container_width=True)
        
        if submitted:
            try:
                if not current_password or not new_password or not confirm_password:
                    add_notification("All fields are required", type="error")
                elif new_password != confirm_password:
                    add_notification("New passwords do not match", type="error")
                elif len(new_password) < 8:
                    add_notification("Password must be at least 8 characters long", type="error")
                else:
                    # Verify current password
                    db = Database()
                    salt, stored_hash = current_user.password_hash.split(':')
                    password_hash = hashlib.sha256((current_password + salt).encode()).hexdigest()
                    
                    if password_hash != stored_hash:
                        logger.warning(f"Failed password change attempt for user: {current_user.username}")
                        add_notification("Current password is incorrect", type="error")
                    else:
                        # Update password
                        success = db.reset_password(current_user.id, new_password)
                        
                        if success:
                            add_notification("Password changed successfully!", type="success")
                            time.sleep(1.5)
                            st.rerun()
                        else:
                            add_notification("Failed to update password. Please try again.", type="error")
            except Exception as e:
                logger.error(f"Error changing password: {e}")
                add_notification(f"An error occurred: {e}", type="error")

# Footer
create_footer()

# Logout option
with st.sidebar:
    if st.button("Logout", use_container_width=True):
        logout_user() 