# pages/5_User_Management.py
import streamlit as st
import logging
import pandas as pd
import time
from database import Database, User
from utils.auth_utils import check_role_permission, get_current_user, logout_user
from utils.layout import create_header, create_footer, display_user_info, add_notification, handle_notifications, get_base_url
from datetime import datetime
import secrets
import string

# Configure logging
logger = logging.getLogger(__name__)

st.set_page_config(page_title="User Management", page_icon="👥", layout="wide")

# Display logo
st.logo(
    "assets/Horizontal-Logo.png", 
    size="large",
    icon_image="assets/Logomark.png"
)

# Check if user has admin privileges
if not check_role_permission('admin'):
    st.stop()  # Authentication check will handle displaying the error and stopping execution

# Display notifications at the top of the page
handle_notifications()

# Display user info in sidebar
display_user_info()

# Get current user for context
current_user = get_current_user()
is_super_admin = current_user and current_user.role == 'super_admin'

# Create header
create_header(
    title="👥 User Management",
    subtitle="Manage users and their permissions in the system"
)

# Initialize database
try:
    db = Database()
    
    # Layout
    tab1, tab2 = st.tabs(["User List", "Create New User"])
    
    # TAB 1: User List
    with tab1:
        st.subheader("Current Users")
        
        # Fetch all users
        try:
            users = db.get_all_users()
            
            if users:
                # Add refresh button
                refresh_col1, refresh_col2 = st.columns([5, 1])
                with refresh_col1:
                    st.write(f"Total users: **{len(users)}**")
                with refresh_col2:
                    if st.button("🔄 Refresh", key="refresh_users"):
                        st.experimental_rerun()
                
                # Convert to DataFrame for easier display
                users_data = []
                for user in users:
                    # Format last login time for better readability
                    if user.last_login:
                        try:
                            # Try to parse the timestamp and format it nicely
                            last_login = datetime.fromtimestamp(float(user.last_login))
                            last_login_str = last_login.strftime("%Y-%m-%d %H:%M:%S")
                        except:
                            last_login_str = user.last_login
                    else:
                        last_login_str = "Never"
                    
                    # Add visual indicators for status
                    status_icon = "✅" if user.is_active else "❌"
                    
                    # Add role colors and icons
                    if user.role == "super_admin":
                        role_display = "🔑 Super Admin"
                        role_color = "#FF5733"  # Red-orange
                    elif user.role == "admin":
                        role_display = "👨‍💼 Admin"
                        role_color = "#3366FF"  # Blue
                    else:
                        role_display = "👤 User"
                        role_color = "#33AA33"  # Green
                    
                    users_data.append({
                        "ID": user.id,
                        "Username": user.username,
                        "Email": user.email,
                        "Role": role_display,
                        "Active": f"{status_icon} {'Active' if user.is_active else 'Inactive'}",
                        "Last Login": last_login_str,
                        "_role_color": role_color,
                        "_actual_role": user.role,
                        "_actual_active": user.is_active
                    })
                
                users_df = pd.DataFrame(users_data)
                
                # Apply styling to dataframe
                styled_df = users_df.drop(columns=["_role_color", "_actual_role", "_actual_active"])
                
                # Display users table with column configuration
                st.dataframe(
                    styled_df,
                    use_container_width=True,
                    column_config={
                        "ID": st.column_config.TextColumn("ID", width="small"),
                        "Username": st.column_config.TextColumn("Username", width="medium"),
                        "Email": st.column_config.TextColumn("Email", width="medium"),
                        "Role": st.column_config.TextColumn("Role", width="small"),
                        "Active": st.column_config.TextColumn("Status", width="small"),
                        "Last Login": st.column_config.TextColumn("Last Login", width="medium"),
                    },
                    height=400
                )
                
                # User management actions section with a nicer UI
                st.markdown("---")
                st.subheader("User Management Actions")
                st.markdown("Select a user from the dropdown below to perform actions.")
                
                user_action_tabs = st.tabs(["Edit User", "Manage Access", "Delete User"])
                
                # TAB: Edit User
                with user_action_tabs[0]:
                    # Edit user
                    user_to_edit = st.selectbox(
                        "Select user to edit",
                        options=[(u.id, u.username) for u in users],
                        format_func=lambda x: x[1]
                    )
                    
                    if user_to_edit:
                        user_id = user_to_edit[0]
                        selected_user = next((u for u in users if u.id == user_id), None)
                        
                        if selected_user:
                            # Show user card with current details
                            st.write("### Current User Details")
                            if selected_user.role == "super_admin":
                                st.markdown("🔑 **Super Administrator**")
                            elif selected_user.role == "admin":
                                st.markdown("👨‍💼 **Administrator**")
                            else:
                                st.markdown("👤 **Regular User**")
                            
                            # Warning messages
                            # Don't allow editing own role (prevents lock-out)
                            if selected_user.id == current_user.id:
                                st.warning("⚠️ You cannot change your own role to prevent accidental lock-out.")
                            
                            # Don't allow non-super-admins to edit super-admins
                            if selected_user.role == 'super_admin' and not is_super_admin:
                                st.warning("⚠️ Only super administrators can modify other super administrators.")
                                edit_disabled = True
                            else:
                                edit_disabled = False
                            
                            # Edit form
                            with st.form(key="edit_user_form"):
                                st.subheader(f"Edit User: {selected_user.username}")
                                
                                # Username (displayed but not editable)
                                st.text_input("Username", value=selected_user.username, disabled=True, 
                                             help="Username cannot be changed once created")
                                
                                # Email
                                new_email = st.text_input("Email", value=selected_user.email)
                                
                                # Role selection with better UI
                                st.write("**User Role**")
                                role_options = ['user', 'admin']
                                if is_super_admin:
                                    role_options.append('super_admin')
                                
                                role_labels = {
                                    'user': '👤 Regular User',
                                    'admin': '👨‍💼 Administrator',
                                    'super_admin': '🔑 Super Administrator'
                                }
                                
                                new_role = st.selectbox(
                                    "Role",
                                    options=role_options,
                                    format_func=lambda x: role_labels.get(x, x),
                                    index=role_options.index(selected_user.role) if selected_user.role in role_options else 0,
                                    disabled=edit_disabled or selected_user.id == current_user.id
                                )
                                
                                # Status as a more visible toggle
                                st.write("**Account Status**")
                                new_active = st.toggle(
                                    "Active Account",
                                    value=selected_user.is_active,
                                    disabled=edit_disabled or selected_user.id == current_user.id,
                                    help="Toggle to activate or deactivate the account"
                                )
                                
                                # More user stats
                                if selected_user.last_login:
                                    try:
                                        last_login = datetime.fromtimestamp(float(selected_user.last_login))
                                        last_login_str = last_login.strftime("%Y-%m-%d %H:%M:%S")
                                    except:
                                        last_login_str = selected_user.last_login
                                    st.info(f"Last login: {last_login_str}")
                                else:
                                    st.info("This user has never logged in")
                                
                                submitted = st.form_submit_button("Update User", disabled=edit_disabled, use_container_width=True)
                                
                                if submitted:
                                    try:
                                        # Show processing indicator
                                        with st.spinner("Updating user..."):
                                            # Update user in database with more robust error handling
                                            success = db.update_user(
                                                user_id=selected_user.id,
                                                username=None,  # Username can't be changed
                                                email=new_email,
                                                password=None,  # Password not updated here
                                                role=new_role if selected_user.id != current_user.id else None,
                                                is_active=new_active if selected_user.id != current_user.id else None
                                            )
                                            
                                            if success:
                                                # Display success message
                                                st.success(f"User {selected_user.username} updated successfully!")
                                                add_notification(f"User {selected_user.username} updated successfully!", type="success")
                                                
                                                # Give user a moment to see the success message
                                                time.sleep(1)
                                                st.rerun()
                                            else:
                                                st.error(f"Failed to update user {selected_user.username}. Please try again.")
                                                add_notification(f"Failed to update user {selected_user.username}", type="error")
                                    except Exception as e:
                                        logger.error(f"Error updating user: {e}")
                                        st.error(f"Error updating user: {str(e)}")
                                        add_notification(f"Error updating user: {e}", type="error")
                
                # TAB: Manage Access
                with user_action_tabs[1]:
                    # Two columns: Invitations and Password Management
                    access_col1, access_col2 = st.columns(2)
                    
                    with access_col1:
                        st.subheader("🔗 Send User Invitation")
                        # Send invite (for users without last_login)
                        uninvited_users = [u for u in users if not u.last_login]
                        
                        if uninvited_users:
                            st.info("Select a user to generate an invitation link.")
                            
                            user_to_invite = st.selectbox(
                                "Select user to invite",
                                options=[(u.id, u.username) for u in uninvited_users],
                                format_func=lambda x: x[1]
                            )
                            
                            if user_to_invite:
                                if st.button("Generate Invitation Link", key="send_invite", use_container_width=True):
                                    try:
                                        with st.spinner("Generating invitation link..."):
                                            user_id = user_to_invite[0]
                                            invited_user = next((u for u in users if u.id == user_id), None)
                                            
                                            if invited_user:
                                                # Generate invitation token
                                                invite_token = db.generate_invitation_token(user_id)
                                                
                                                if invite_token:
                                                    # Get base URL for constructing the full invitation link
                                                    base_url = get_base_url()
                                                    
                                                    # Create invitation link with base URL if available
                                                    if base_url:
                                                        invite_link = f"{base_url}/accept_invite?token={invite_token}"
                                                    else:
                                                        invite_link = f"accept_invite?token={invite_token}"
                                                
                                                    st.success(f"Invitation ready for {invited_user.username}!")
                                                    add_notification(f"Invitation ready for {invited_user.username}!", type="success")
                                                    
                                                    # Display in a nicely formatted box
                                                    st.code(invite_link, language="text")
                                                    
                                                    # Better copy to clipboard function
                                                    st.markdown(
                                                        f"""
                                                        <div style="margin-bottom: 15px;">
                                                            <textarea id="invite-link-manage-{user_id}" style="position: absolute; left: -9999px;">{invite_link}</textarea>
                                                            <button 
                                                                onclick="
                                                                    const textarea = document.getElementById('invite-link-manage-{user_id}');
                                                                    textarea.select();
                                                                    document.execCommand('copy');
                                                                    alert('Invitation link copied to clipboard!');
                                                                "
                                                                style="background-color: #4CAF50; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer;">
                                                                Copy Invitation Link
                                                            </button>
                                                        </div>
                                                        """,
                                                        unsafe_allow_html=True
                                                    )
                                                    
                                                    # Fallback copy method
                                                    st.markdown("**If the button doesn't work:** Copy the code block above manually.")
                                                    
                                                    # Explain what to do with the link
                                                    st.info("Share this invitation link with the user. They will set their password when they click this link.")
                                                else:
                                                    st.error("Failed to generate invitation token.")
                                    except Exception as e:
                                        logger.error(f"Error sending invitation: {e}")
                                        st.error(f"Error generating invitation: {str(e)}")
                                        add_notification(f"Error sending invitation: {e}", type="error")
                        else:
                            st.info("All users have already logged in. No invitations needed.")
                    
                    with access_col2:
                        # Password management for super admin
                        if is_super_admin:
                            st.subheader("🔐 Password Management")
                            st.info("As a super admin, you can reset passwords for any user.")
                            
                            user_to_manage = st.selectbox(
                                "Select user to manage",
                                options=[(u.id, u.username) for u in users if u.id != current_user.id],
                                format_func=lambda x: x[1],
                                key="password_manage_select"
                            )
                            
                            if user_to_manage:
                                user_id = user_to_manage[0]
                                selected_user = next((u for u in users if u.id == user_id), None)
                                
                                if selected_user:
                                    # Password reset form with improved UI
                                    with st.form(key="password_reset_form"):
                                        st.write(f"##### Reset password for: {selected_user.username}")
                                        
                                        # Password fields in columns
                                        pw_col1, pw_col2 = st.columns(2)
                                        with pw_col1:
                                            new_password = st.text_input(
                                                "New Password",
                                                type="password",
                                                help="Enter the new password"
                                            )
                                        with pw_col2:
                                            confirm_password = st.text_input(
                                                "Confirm Password",
                                                type="password",
                                                help="Confirm the new password"
                                            )
                                        
                                        # Password strength indicator
                                        if new_password:
                                            strength = len(new_password)
                                            if strength < 8:
                                                st.warning("Password is too short (minimum 8 characters)")
                                                strength_text = "Weak"
                                                strength_color = "red"
                                            elif strength < 12:
                                                strength_text = "Moderate"
                                                strength_color = "orange"
                                            else:
                                                strength_text = "Strong"
                                                strength_color = "green"
                                            
                                            st.markdown(f"Password strength: <span style='color:{strength_color}'>{strength_text}</span>", unsafe_allow_html=True)
                                        
                                        submitted = st.form_submit_button("Reset Password", use_container_width=True)
                                        
                                        if submitted:
                                            # Form validation
                                            has_error = False
                                            
                                            if not new_password:
                                                st.error("Please enter a new password")
                                                has_error = True
                                            
                                            if new_password != confirm_password:
                                                st.error("Passwords do not match")
                                                has_error = True
                                                
                                            if len(new_password) < 8:
                                                st.error("Password must be at least 8 characters long")
                                                has_error = True
                                            
                                            if not has_error:
                                                try:
                                                    # Show processing indicator
                                                    with st.spinner("Resetting password..."):
                                                        # Update password
                                                        success = db.reset_password(user_id, new_password)
                                                        
                                                        if success:
                                                            st.success(f"Password reset successful for {selected_user.username}")
                                                            add_notification(f"Password reset successful for {selected_user.username}", type="success")
                                                            time.sleep(1)
                                                            st.rerun()
                                                        else:
                                                            st.error("Failed to reset password")
                                                            add_notification("Failed to reset password", type="error")
                                                except Exception as e:
                                                    logger.error(f"Error resetting password: {e}")
                                                    st.error(f"Error resetting password: {str(e)}")
                                                    add_notification(f"Error resetting password: {e}", type="error")
                        else:
                            st.info("Only super administrators can reset user passwords.")
                
                # TAB: Delete User
                with user_action_tabs[2]:
                    # Delete user option (only for super_admin)
                    if is_super_admin:
                        st.subheader("🗑️ Delete User")
                        st.warning("⚠️ **Warning**: Deleting a user is permanent and cannot be undone. All user data will be lost.")
                        
                        user_to_delete = st.selectbox(
                            "Select user to delete",
                            options=[(u.id, u.username) for u in users if u.id != current_user.id],  # Can't delete yourself
                            format_func=lambda x: x[1],
                            key="delete_user_select"
                        )
                        
                        if user_to_delete:
                            user_id = user_to_delete[0]
                            selected_user = next((u for u in users if u.id == user_id), None)
                            
                            if selected_user:
                                # Display user details before deletion
                                st.write("### User to Delete")
                                
                                # Show user card with details
                                details_md = "| Property | Value |\n| --- | --- |\n"
                                details_md += f"| **Username** | {selected_user.username} |\n"
                                details_md += f"| **Email** | {selected_user.email} |\n"
                                details_md += f"| **Role** | {selected_user.role} |\n"
                                details_md += f"| **Status** | {'Active' if selected_user.is_active else 'Inactive'} |\n"
                                st.markdown(details_md)
                                
                                # Extra warning for deleting admins or super admins
                                if selected_user.role in ['admin', 'super_admin']:
                                    st.error(f"⚠️ You are about to delete a user with {selected_user.role} privileges. This could affect system administration capabilities.")
                            
                            # Confirmation with the username typed in
                            with st.form(key="delete_user_form"):
                                st.write("#### Confirm Deletion")
                                st.write(f"To confirm deletion, type the username: **{user_to_delete[1]}**")
                                
                                delete_confirm = st.text_input(
                                    "Username Confirmation",
                                    key="delete_confirm",
                                    help="Type the username exactly as shown above"
                                )
                                
                                # Add a checkbox for extra confirmation
                                understand_confirm = st.checkbox(
                                    "I understand that this action cannot be undone",
                                    key="understand_confirm"
                                )
                                
                                delete_button = st.form_submit_button(
                                    "Delete User", 
                                    type="primary", 
                                    use_container_width=True
                                )
                                
                                if delete_button:
                                    if not understand_confirm:
                                        st.error("You must confirm that you understand this action cannot be undone.")
                                    elif delete_confirm != user_to_delete[1]:
                                        st.error("Username doesn't match. Deletion canceled.")
                                        add_notification("Username doesn't match. Deletion canceled.", type="error", duration=10)
                                    else:
                                        try:
                                            # Show processing indicator
                                            with st.spinner("Deleting user..."):
                                                # Delete user from database with improved error handling
                                                success = db.delete_user(user_id)
                                                
                                                if success:
                                                    st.success(f"User {user_to_delete[1]} deleted successfully!")
                                                    add_notification(f"User {user_to_delete[1]} deleted successfully!", type="success")
                                                    
                                                    # Give user a moment to see the success message
                                                    time.sleep(1)
                                                    st.rerun()
                                                else:
                                                    st.error(f"Failed to delete user {user_to_delete[1]}. Please try again later.")
                                                    add_notification(f"Failed to delete user {user_to_delete[1]}.", type="error")
                                        except Exception as e:
                                            logger.error(f"Error deleting user: {e}")
                                            st.error(f"Error deleting user: {str(e)}")
                                            add_notification(f"Error deleting user: {e}", type="error")
                    else:
                        st.info("Only super administrators can delete users.")
                        st.write("If you need to remove a user, please contact a super administrator.")
            else:
                st.info("No users found in the system.")
        
        except Exception as e:
            logger.error(f"Error loading users: {e}")
            st.error(f"Error loading users: {e}")
    
    # TAB 2: Create New User
    with tab2:
        st.subheader("Create a New User")
        
        # Create tabs for different creation methods
        create_method_tab2 = st.tabs(["Manual Creation"])
        
        
        
        with create_method_tab2:
            if not is_super_admin:
                st.error("Only super administrators can manually create users.")
            else:
                st.info("Manually create a user with predefined credentials.")
                with st.form(key="manual_create_user_form"):
                    new_username = st.text_input("Username", help="Username must be unique")
                    new_email = st.text_input("Email", help="User's email address")
                    
                    # Password fields with strength meter
                    col1, col2 = st.columns(2)
                    with col1:
                        new_password = st.text_input("Password", type="password", help="Set the user's password")
                    with col2:
                        new_password_confirm = st.text_input("Confirm Password", type="password", help="Confirm the password")
                    
                    # Password strength indicator
                    if new_password:
                        # Calculate strength based on multiple factors
                        length_score = min(len(new_password) / 12.0, 1.0)  # Length up to 12 chars
                        has_upper = any(c.isupper() for c in new_password)
                        has_lower = any(c.islower() for c in new_password)
                        has_digit = any(c.isdigit() for c in new_password)
                        has_special = any(not c.isalnum() for c in new_password)
                        
                        diversity_score = (has_upper + has_lower + has_digit + has_special) / 4.0
                        strength_score = (length_score * 0.7) + (diversity_score * 0.3)
                        
                        if strength_score < 0.5 or len(new_password) < 8:
                            st.warning("Password is too weak. Use at least 8 characters with a mix of uppercase, lowercase, numbers, and special characters.")
                            strength_text = "Weak"
                            strength_color = "red"
                        elif strength_score < 0.7:
                            strength_text = "Moderate"
                            strength_color = "orange"
                        else:
                            strength_text = "Strong"
                            strength_color = "green"
                        
                        st.markdown(f"Password strength: <span style='color:{strength_color}'>{strength_text}</span>", unsafe_allow_html=True)
                        
                        # Show strength tips
                        if strength_score < 0.7:
                            tips = []
                            if not has_upper:
                                tips.append("Add uppercase letters")
                            if not has_lower:
                                tips.append("Add lowercase letters")
                            if not has_digit:
                                tips.append("Add numbers")
                            if not has_special:
                                tips.append("Add special characters")
                            if len(new_password) < 12:
                                tips.append(f"Add {12-len(new_password)} more characters for optimal length")
                            
                            if tips:
                                st.markdown("**Tips to improve password strength:**")
                                for tip in tips:
                                    st.markdown(f"- {tip}")
                    
                    # Role selection for super admin with descriptive labels
                    role_labels = {
                        'user': '👤 Regular User - Can view content only',
                        'admin': '👨‍💼 Administrator - Can manage most settings',
                        'super_admin': '🔑 Super Administrator - Full system access'
                    }
                    
                    new_user_role = st.selectbox(
                        "Role", 
                        options=['user', 'admin', 'super_admin'],
                        format_func=lambda x: role_labels.get(x, x)
                    )
                    
                    # Active status
                    is_active = st.toggle(
                        "Account Active", 
                        value=True, 
                        help="Toggle to set whether the user account is active immediately"
                    )
                    
                    # Form submission
                    submitted = st.form_submit_button("Create User", use_container_width=True)
                    
                    if submitted:
                        # Form validation
                        has_error = False
                        
                        if not new_username:
                            st.error("Username is required")
                            has_error = True
                        
                        if not new_email:
                            st.error("Email is required")
                            has_error = True
                            
                        if not new_password:
                            st.error("Password is required")
                            has_error = True
                        
                        if new_password != new_password_confirm:
                            st.error("Passwords do not match")
                            has_error = True
                            
                        if len(new_password) < 8:
                            st.error("Password must be at least 8 characters long")
                            has_error = True
                        
                        if not has_error:
                            try:
                                # Show processing indicator
                                with st.spinner("Creating user..."):
                                    # Check if username exists
                                    existing_user = db.get_user_by_username(new_username)
                                    
                                    if existing_user:
                                        st.error(f"Username '{new_username}' already exists. Please choose a different username.")
                                        add_notification(f"Username '{new_username}' already exists", type="error", duration=10)
                                    else:
                                        try:
                                            # Create new user with password with robust error handling
                                            user_id = db.create_user(
                                                username=new_username,
                                                email=new_email,
                                                role=new_user_role,
                                                password=new_password,
                                                is_active=is_active
                                            )
                                            
                                            if user_id:
                                                # User created successfully
                                                st.success(f"User '{new_username}' created successfully!")
                                                add_notification(f"User '{new_username}' created successfully!", type="success", duration=15)
                                                
                                                # Show user summary with a nice formatted display
                                                st.write("### New User Details")
                                                user_details = {
                                                    "Username": new_username,
                                                    "Email": new_email,
                                                    "Role": f"{role_labels.get(new_user_role, new_user_role)}",
                                                    "Status": "Active" if is_active else "Inactive",
                                                    "ID": user_id,
                                                    "Password": "••••••••" + new_password[-2:] if len(new_password) > 2 else "••••••••"
                                                }
                                                
                                                # Create a formatted table display
                                                details_md = "| Property | Value |\n| --- | --- |\n"
                                                for key, value in user_details.items():
                                                    details_md += f"| **{key}** | {value} |\n"
                                                st.markdown(details_md)
                                                
                                                # Success icon and message
                                                st.markdown(
                                                    """
                                                    <div style="text-align: center; margin: 20px 0; color: #4CAF50">
                                                        <i class="fas fa-check-circle" style="font-size: 48px;"></i>
                                                        <p style="font-weight: bold; margin-top: 10px;">User created successfully!</p>
                                                    </div>
                                                    """, 
                                                    unsafe_allow_html=True
                                                )
                                                
                                                # Provide login instructions
                                                st.info("""
                                                The user can now log in with the credentials you provided.
                                                Be sure to communicate the username and password securely to the user.
                                                """)
                                            else:
                                                st.error("Failed to create user. Please try again.")
                                                add_notification("Failed to create user", type="error", duration=10)
                                        except Exception as e:
                                            logger.error(f"Error creating user: {e}")
                                            st.error(f"Error creating user: {str(e)}")
                                            add_notification(f"Database error: {str(e)}", type="error", duration=10)
                            except Exception as e:
                                logger.error(f"Error during user creation process: {e}")
                                st.error(f"System error: {str(e)}")
                                add_notification(f"System error: {str(e)}", type="error", duration=10)
                            
except Exception as e:
    logger.error(f"Database connection error: {e}")
    st.error(f"Error connecting to database: {e}")

# Footer
create_footer()

# Logout option in sidebar is handled by display_user_info()

# Check if we need to improve token generation functionality
try:
    # Add a helper function to check if the token generation method is missing or needs improvement
    db_functions = dir(db)
    if 'generate_invitation_token' not in db_functions:
        st.warning("The invitation token system needs to be updated. Please contact your administrator.")
except Exception as e:
    logger.error(f"Error checking database functions: {e}")
