import os
from dataclasses import dataclass
import streamlit as st
import time
from utils.background_task import initialize_background_task, get_background_status
from database import Database, User
import secrets
import hashlib
import string
from streamlit_modal import Modal
import logging
from utils.auth_utils import logout_user, set_logged_in_user, check_user_logged_in
from utils.layout import add_notification
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize Streamlit app configuration 
st.set_page_config(
    page_title="Shomer Portal",
    page_icon="assets/Logomark.png",
    layout="centered",
    initial_sidebar_state="auto"
)

# Display logo
st.logo(
    "assets/Horizontal-Logo.png", 
    size="large",
    icon_image="assets/Logomark.png"
)

# Custom CSS
st.markdown("""
<style>
    .login-container {
        max-width: 400px;
        margin: 0 auto;
        padding: 20px;
    }
    .stButton button {
        width: 100%;
    }
    .main .block-container {
        padding-top: 2rem;
    }
    .centered-text {
        text-align: center;
    }
</style>
""", unsafe_allow_html=True)

# Initialize the background task system
initialize_background_task()

# Initialize session state variables
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
if 'user_id' not in st.session_state:
    st.session_state['user_id'] = None
if 'user_role' not in st.session_state:
    st.session_state['user_role'] = None
if 'login_error' not in st.session_state:
    st.session_state['login_error'] = None
if 'invitation_token' not in st.session_state:
    st.session_state['invitation_token'] = None
if 'reset_password_mode' not in st.session_state:
    st.session_state['reset_password_mode'] = False
# Add remember me related states
if 'remember_me_enabled' not in st.session_state:
    st.session_state['remember_me_enabled'] = False
if 'remembered_user_id' not in st.session_state:
    st.session_state['remembered_user_id'] = None

# Check if user should be remembered from previous session
if not st.session_state['logged_in'] and st.session_state['remember_me_enabled'] and st.session_state['remembered_user_id']:
    try:
        # Try to automatically log in the remembered user
        db = Database()
        remembered_user = db.get_user_by_id(st.session_state['remembered_user_id'])
        
        if remembered_user and remembered_user.is_active:
            logger.info(f"Auto-login for remembered user: {remembered_user.username}")
            
            # Set session state for authenticated user
            st.session_state['logged_in'] = True
            st.session_state['user_id'] = remembered_user.id
            st.session_state['user_role'] = remembered_user.role
            
            # Update last login time
            db.update_last_login(remembered_user.id)
    except Exception as e:
        logger.error(f"Auto-login failed: {e}")
        # Clear remember me state on error
        st.session_state['remember_me_enabled'] = False
        st.session_state['remembered_user_id'] = None

@dataclass
class Config:
    DB_PATH = os.getenv('DB_PATH', 'config.db')
    STREAM_APP_WS_URL = os.getenv('STREAM_APP_WS_URL', 'ws://localhost:8765')

def check_path_for_token():
    """Check URL path for invitation token"""
    try:
        # Get the path from the URL
        path = st.query_params
        if 'token' in path:
            st.session_state['invitation_token'] = path['token']
            return True
    except:
        pass
    return False

def set_invitation_token(token):
    """Set the invitation token in session state"""
    st.session_state['invitation_token'] = token
    st.session_state['reset_password_mode'] = True
    
def reset_password_form():
    """Handle password reset from invitation token"""
    
    # Display logo in sidebar
    
    
    # Get token from URL query parameters
    token = st.query_params.get("token", "")
    
    try:
        # Check if token exists
        if not token:
            st.error("Invalid invitation link. No token provided.")
            if st.button("Return to Login", use_container_width=True):
                st.switch_page("main.py")
            return
        
        # Initialize database
        try:
            db = Database()
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            st.error("Could not connect to the database. Please try again later.")
            if st.button("Return to Login", use_container_width=True):
                st.switch_page("main.py")
            return
        
        # Get user by token
        try:
            user = db.get_user_by_token(token)
        except Exception as e:
            logger.error(f"Error retrieving user by token: {e}")
            st.error("Error verifying your invitation. The link may be invalid or expired.")
            if st.button("Return to Login", use_container_width=True):
                st.switch_page("main.py")
            return
        
        # Check if token is valid
        if not user:
            st.error("Invalid or expired invitation link.")
            if st.button("Return to Login", use_container_width=True):
                st.switch_page("main.py")
            return
        
        # Token is valid, show reset form
        st.success(f"Welcome, {user.username}! Please set your password to continue.")
        
        with st.form("reset_password_form"):
            new_password = st.text_input("New Password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")
            
            submitted = st.form_submit_button("Set Password", use_container_width=True)
            
            if submitted:
                try:
                    # Validate passwords
                    if not new_password:
                        st.error("Please enter a password")
                    elif new_password != confirm_password:
                        st.error("Passwords do not match")
                    elif len(new_password) < 8:
                        st.error("Password must be at least 8 characters long")
                    else:
                        # Update password and consume token
                        success = db.reset_password(user.id, new_password)
                        
                        if success:
                            # Mark invitation token as used
                            db.consume_invitation_token(token)
                            
                            # Update last login time
                            db.update_last_login(user.id)
                            
                            # Show success message with progress
                            st.success("Password set successfully! Redirecting to login...")
                            
                            # Set up for auto login
                            st.session_state['logged_in'] = True
                            st.session_state['user_id'] = user.id
                            st.session_state['user_role'] = user.role
                            
                            # Progress bar for visual feedback
                            progress_bar = st.progress(0)
                            for i in range(100):
                                # Update progress bar
                                progress_bar.progress(i + 1)
                                time.sleep(0.01)
                            
                            # Redirect to Dashboard
                            st.switch_page("main.py")
                        else:
                            st.error("Failed to update password. Please try again.")
                except Exception as e:
                    logger.error(f"Password reset error: {e}")
                    st.error(f"An error occurred: {e}")
    except Exception as e:
        logger.error(f"Password reset form error: {e}")
        st.error("An unexpected error occurred. Please try again later.")
        if st.button("Return to Login", use_container_width=True):
            st.switch_page("main.py")

def login(username, password):
    """
    Authenticate user with provided username and password
    
    Args:
        username (str): The username to authenticate
        password (str): The password to authenticate
        
    Returns:
        tuple: (success, message, user_id, user_role)
    """
    try:
        # Initialize database
        db = Database()
        
        # Check if username and password are provided
        if not username or not password:
            return False, "Username and password are required", None, None
            
        # Get user by username
        user = db.get_user_by_username(username)
        
        if not user:
            logger.warning(f"Login attempt with non-existent username: {username}")
            # Use a generic error message to prevent username enumeration
            return False, "Invalid username or password", None, None
            
        # Check if user is active
        if not user.get('is_active', False):
            logger.warning(f"Login attempt for inactive user: {username}")
            return False, "Your account has been deactivated. Please contact an administrator.", None, None
            
        # Verify password
        if db.verify_password(password, user.get('password', '')):
            # Update last login time
            db.update_user_last_login(user['user_id'])
            
            logger.info(f"Successful login: {username} (role: {user['role']})")
            return True, "Login successful", user['user_id'], user['role']
        else:
            logger.warning(f"Failed login attempt for user: {username}")
            return False, "Invalid username or password", None, None
        
    except Exception as e:
        logger.error(f"Login error: {e}")
        return False, f"An error occurred during login. Please try again.", None, None

def login_form():
    """Display login form and handle authentication"""
    # Display logo in sidebar
    
        
    # Check for notifications
    if "notifications" in st.session_state and st.session_state.notifications:
        for notification in st.session_state.notifications:
            if notification["type"] == "success":
                st.success(notification["message"])
            elif notification["type"] == "error":
                st.error(notification["message"])
            elif notification["type"] == "warning":
                st.warning(notification["message"])
            else:
                st.info(notification["message"])
        # Clear notifications after displaying them
        st.session_state.notifications = []
    
   
    
    # Check for logout request
    if st.query_params.get("logout"):
        logout_user()
        add_notification("You have been successfully logged out", type="info")
        # Clear the parameter to avoid showing the message again on refresh
        st.query_params.clear()
        st.rerun()
        return

    # Check for login error
    if "login_error" in st.session_state and st.session_state["login_error"]:
        st.error(st.session_state["login_error"])
        # Clear the error after displaying it
        del st.session_state["login_error"]
    
    # Login form in a container
    with st.container():
        # Login form
        with st.form("login_form"):
            st.subheader("Sign In")
            username = st.text_input("Username", key="login_username")
            password = st.text_input("Password", type="password", key="login_password")
            remember_me = st.checkbox("Remember Me", key="remember_me_checkbox")
            
            submitted = st.form_submit_button("Login", use_container_width=True)
            
            if submitted:
                success, message, user_id, user_role = login(username, password)
                
                if success:
                    # Set session state
                    st.session_state['logged_in'] = True
                    st.session_state['user_id'] = user_id
                    st.session_state['user_role'] = user_role
                    
                    # Set remember me state if checked
                    if remember_me:
                        st.session_state['remember_me_enabled'] = True
                        st.session_state['remembered_user_id'] = user_id
                    else:
                        # Ensure remember me is disabled if not checked
                        st.session_state['remember_me_enabled'] = False
                        st.session_state['remembered_user_id'] = None
                    
                    # Show success message with spinner
                    with st.spinner("Logging in..."):
                        time.sleep(1)
                    add_notification("Login successful!", type="success")
                    time.sleep(0.5)
                    
                    # Redirect to Dashboard
                    st.switch_page("main.py")
                else:
                    st.error(message)
        
        # Reset password link
        reset_link = st.button("Forgot Password?", use_container_width=True)
        if reset_link:
            st.info("Please contact your administrator to reset your password.")
    
    # First time setup section
    st.divider()
    
    with st.expander("First Time Setup", expanded=False):
        st.subheader("Create Administrator Account")
        
        # Check if super admin exists
        try:
            db = Database()
            super_admin_exists = db.check_super_admin_exists()
            
            if not super_admin_exists:
                st.warning("No super administrator account exists. Create the first admin to get started.")
                
                # Form for creating the first admin
                with st.form("create_admin_form"):
                    admin_username = st.text_input("Username", key="admin_username")
                    admin_email = st.text_input("Email", key="admin_email")
                    admin_password = st.text_input("Password", type="password", key="admin_password")
                    admin_password_confirm = st.text_input("Confirm Password", type="password", key="admin_password_confirm")
                    
                    submitted = st.form_submit_button("Create Admin Account", type="primary", use_container_width=True)
                    
                    if submitted:
                        if not admin_username or not admin_email or not admin_password:
                            st.error("All fields are required")
                        elif admin_password != admin_password_confirm:
                            st.error("Passwords do not match")
                        elif len(admin_password) < 8:
                            st.error("Password must be at least 8 characters long")
                        else:
                            try:
                                # Create super admin
                                user_id = db.create_user(
                                    username=admin_username,
                                    email=admin_email,
                                    role="super_admin",
                                    password=admin_password
                                )
                                
                                if user_id:
                                    add_notification("Super administrator account created successfully!", type="success")
                                    time.sleep(1)
                                    st.rerun()
                                else:
                                    st.error("Failed to create administrator account")
                            except Exception as e:
                                logger.error(f"Error creating super admin: {e}")
                                st.error(f"Error creating account: {e}")
            else:
                st.info("Administrator account already exists. Please log in.")
        except Exception as e:
            logger.error(f"Error checking for super admin: {e}")
            st.error(f"Database error: {e}")

    # Footer
    st.markdown("<div class='centered-text'>© 2025 Shomer</div>", unsafe_allow_html=True)

def logout(clear_remember_me=False):
    """Log out the current user with robust error handling
    
    Args:
        clear_remember_me (bool): Whether to clear the remember me state
    """
    try:
        # Clear all authentication-related session state
        auth_keys = ['logged_in', 'user_id', 'user_role', 'login_error']
        for key in auth_keys:
            if key in st.session_state:
                del st.session_state[key]
        
        # Ensure logged_in is explicitly set to False
        st.session_state['logged_in'] = False
        
        # Clear remember me state if requested
        if clear_remember_me:
            st.session_state['remember_me_enabled'] = False
            st.session_state['remembered_user_id'] = None
        
        # Clear any URL parameters
        try:
            st.query_params.clear()
        except:
            pass
        
        st.rerun()
    except Exception as e:
        logger.error(f"Error during logout: {e}")
        # Force reset of critical session states
        st.session_state['logged_in'] = False
        st.session_state['user_id'] = None
        st.session_state['user_role'] = None
        st.rerun()

def check_auth():
    """Validate user authentication status"""
    # Check if user is logged in
    if not st.session_state.get('logged_in') or not st.session_state.get('user_id'):
        # Check if we should try to auto-login using remember_me
        if st.session_state.get('remember_me_enabled') and st.session_state.get('remembered_user_id'):
            try:
                db = Database()
                user = db.get_user_by_id(st.session_state['remembered_user_id'])
                
                if user and user.is_active:
                    # Auto-login the remembered user
                    st.session_state['logged_in'] = True
                    st.session_state['user_id'] = user.id
                    st.session_state['user_role'] = user.role
                    
                    # Update last login time
                    db.update_last_login(user.id)
                    logger.info(f"Auto-login successful for remembered user: {user.username}")
                    return True
                else:
                    # User not found or inactive, clear remember me
                    st.session_state['remember_me_enabled'] = False
                    st.session_state['remembered_user_id'] = None
                    return False
            except Exception as e:
                logger.error(f"Error during auto-login: {e}")
                # Clear remember me state on error
                st.session_state['remember_me_enabled'] = False
                st.session_state['remembered_user_id'] = None
                return False
        else:
            return False
    
    # Verify user still exists in database
    try:
        db = Database()
        user = db.get_user_by_id(st.session_state['user_id'])
        
        if not user:
            logger.warning(f"User not found: {st.session_state['user_id']}")
            # User no longer exists, force logout
            logout()
            return False
        
        if not user.is_active:
            logger.warning(f"Inactive user attempted access: {user.id}")
            # User is inactive, force logout
            st.session_state['login_error'] = "Your account has been deactivated. Please contact an administrator."
            logout()
            return False
        
        # All checks passed
        return True
    except Exception as e:
        logger.error(f"Error during authentication check: {e}")
        # On error, fail closed (safest option)
        logout()
        return False

def main_app():
    """Display the main application dashboard with authentication verification"""
    # Display logo in sidebar
    
        
    # Verify authentication is still valid
    if not check_auth():
        st.error("Authentication failed. Please log in again.")
        login_form()
        return
    
    st.title("Shomer Portal")
    
    # Display vertical logo
    st.markdown(
        """
        <div style="position: absolute; top: 20px; right: 20px; z-index: 999;">
            <img src="assets/Vertical-Logo.png" width="100">
        </div>
        """,
        unsafe_allow_html=True
    )
    
    # Get database instance with error handling
    try:
        db = Database()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        st.error("System error: Could not connect to the database. Please try again later.")
        return
    
    # Get current user with error handling
    try:
        user = db.get_user_by_id(st.session_state['user_id'])
    except Exception as e:
        logger.error(f"Error retrieving user profile: {e}")
        st.error("Failed to load user profile. Please refresh the page.")
        return
    
    # Double-check user exists and is active
    if not user or not user.is_active:
        st.error("Your account has been deactivated or deleted")
        logout()
        return
    
    # Add a logout button in the sidebar
    with st.sidebar:
        st.write(f"Logged in as: **{user.username}** ({user.role})")
        
        # If user is using "Remember Me", show option to forget on logout
        if st.session_state.get('remember_me_enabled', False):
            clear_remember = st.checkbox("Forget me on logout", value=False, key="forget_me_checkbox",
                                        help="Check this to completely log out and disable auto-login")
        else:
            clear_remember = False
            
        if st.button("Logout"):
            logout(clear_remember_me=clear_remember)
    
    # Get status of background tasks for display
    try:
        bg_status = get_background_status()
        
        # Display status in the sidebar (remove in production if desired)
        st.sidebar.text(f"Last Sites Fetch Time: {bg_status['last_run_time']} (EST)")
        st.sidebar.text(f"Fetch active: {'Yes' if bg_status['is_running'] else 'No'}")
    except Exception as e:
        logger.error(f"Failed to get background status: {e}")
        # Non-critical error, provide fallback
        st.sidebar.text("Background task status: Not available")
    
    st.write("""
    This application manages camera configurations and viewing layouts through SQLite database.
    Use the sidebar to navigate between different sections:
    - Sites: Manage your site locations and NVR credentials
    - Cameras: Configure cameras and their RTSP streams
    - PCs: Manage viewing stations and their capabilities
    - Screen Layout: Configure viewing layouts and communicate with streaming application
    """)
    
    # Display role-specific information
    if user.role == "user":
        st.info("""
        You are logged in with a regular user account. You can:
        - View site and camera information
        - Create and manage screen layouts
        
        Note: You cannot add or modify sites, cameras, or PCs.
        """)
    elif user.role == "admin":
        st.success("""
        You are logged in with an admin account. You have full access to the system except:
        - Cannot delete other admin users
        - Cannot delete super admin users
        """)
    elif user.role == "super_admin":
        st.success("You are logged in with a super admin account. You have full access to all features.")

# Main application flow
if st.session_state['reset_password_mode'] or check_path_for_token():
    reset_password_form()
elif st.session_state['logged_in']:
    main_app()
else:
    login_form()