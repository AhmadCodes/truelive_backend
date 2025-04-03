import streamlit as st
import logging
import time
from utils.auth_utils import get_current_user, logout_user

# Configure logging
logger = logging.getLogger(__name__)

def create_header(title = "", subtitle = "", current_page=None):
    """Create a consistent header for all pages
    
    Args:
        title (str): The main title to display in the header
        subtitle (str): The subtitle text to display below the title
    """
    with st.container():
        col1, col2 = st.columns([6, 1])
        with col1:
            st.title(title)
            if subtitle:
                st.markdown(subtitle)
        with col2:
            if "user" in st.session_state and st.session_state.user:
                # User is logged in, show logout button
                if st.button("Logout", key="logout"):
                    logout_user()
                    # Use safe redirect
                    st.switch_page("main.py")
    
    # Horizontal rule
    st.markdown("---")

def handle_notifications():
    """
    Handle system notifications in a consistent way
    
    This function displays all pending notifications and manages their lifecycle
    """
    try:
        # Initialize notification state if not exists
        if "notifications" not in st.session_state:
            st.session_state.notifications = []
        
        # Skip if no notifications
        if not st.session_state.notifications:
            return
        
        # Display notifications in a dedicated container
        with st.container():
            current_time = time.time()
            displayed_count = 0
            
            for idx, notification in enumerate(st.session_state.notifications):
                try:
                    # Extract notification data with safe defaults
                    notification_type = notification.get("type", "info")
                    message = notification.get("message", "")
                    expires = notification.get("expires", None)
                    
                    # Skip expired notifications
                    if expires and current_time > expires:
                        continue
                    
                    # Select appropriate icon
                    if notification_type == "success":
                        icon = "✅"
                    elif notification_type == "error":
                        icon = "⚠️" 
                    elif notification_type == "warning":
                        icon = "⚠️"
                    else:
                        icon = "ℹ️"
                    
                    # Display notification based on type
                    if notification_type == "success":
                        st.success(message, icon=icon)
                    elif notification_type == "error":
                        st.error(message, icon=icon)
                    elif notification_type == "warning":
                        st.warning(message, icon=icon)
                    else:  # info
                        st.info(message, icon=icon)
                    
                    displayed_count += 1
                except Exception as e:
                    logger.error(f"Error displaying notification: {e}")
                    # Skip this notification if it causes errors
                    continue
            
            # Log notification activity for debugging
            if displayed_count > 0:
                logger.info(f"Displayed {displayed_count} notifications")
        
        # Clean up expired notifications to prevent accumulation
        try:
            current_time = time.time()
            original_count = len(st.session_state.notifications)
            
            st.session_state.notifications = [
                n for n in st.session_state.notifications 
                if not (n.get("expires") and current_time > n.get("expires"))
            ]
            
            removed_count = original_count - len(st.session_state.notifications)
            if removed_count > 0:
                logger.info(f"Removed {removed_count} expired notifications")
        except Exception as e:
            logger.error(f"Error cleaning up notifications: {e}")
            # If cleanup fails, reset notifications to prevent issues
            st.session_state.notifications = []
    except Exception as e:
        logger.error(f"Notification system error: {e}")
        # Reset notifications on critical error
        st.session_state.notifications = []

def add_notification(message, type="info", auto_dismiss=True, duration=5):
    """
    Add a notification to be displayed at the top of the page
    
    Args:
        message (str): The notification message
        type (str): Type of notification - "info", "success", "warning", "error"
        auto_dismiss (bool): Whether to automatically dismiss after duration
        duration (int): How long to display notification in seconds
    """
    try:
        # Initialize notifications if not exists
        if "notifications" not in st.session_state:
            st.session_state.notifications = []
        
        # Validate message
        if not message:
            logger.warning("Attempted to add empty notification")
            return
            
        # Validate type
        valid_types = ["info", "success", "warning", "error"]
        if type not in valid_types:
            logger.warning(f"Invalid notification type '{type}', defaulting to 'info'")
            type = "info"
        
        # Create notification object
        notification = {
            "message": str(message),  # Ensure message is a string
            "type": type,
            "created": time.time()
        }
        
        # Add expiration time if auto dismiss is enabled
        if auto_dismiss and duration > 0:
            notification["expires"] = time.time() + max(1, duration)  # Ensure minimum duration
        
        # Add to notifications list
        st.session_state.notifications.append(notification)
        
        # Log for debugging
        logger.info(f"Added {type} notification: {message} (expires: {notification.get('expires')})")
        
        # Prevent excessive notifications (keep maximum 10)
        if len(st.session_state.notifications) > 10:
            logger.warning("Too many notifications, removing oldest")
            st.session_state.notifications = st.session_state.notifications[-10:]
    except Exception as e:
        # Log error but don't crash
        logger.error(f"Error adding notification: {e}")

def create_footer(include_version=True, copyright_text=None):
    """
    Creates a consistent footer
    
    Args:
        include_version (bool): Whether to include version info
        copyright_text (str, optional): Custom copyright text
    """
    # Divider
    st.divider()
    
    # Footer container
    with st.container():
        # Default copyright if none provided
        if not copyright_text:
            copyright_text = "© 2025 Shomer"
        
        st.caption(copyright_text)
        
        if include_version:
            st.caption("Version 1.0.0")

def display_user_info():
    """
    Display current user information in sidebar
    """
    current_user = get_current_user()
    if current_user:
        with st.sidebar:
            st.write("---")
            st.caption("**User Information**")
            st.caption(f"Username: **{current_user.username}**")
            st.caption(f"Role: **{current_user.role}**")
            
            # Navigation to profile
            if st.button("My Profile", key="sidebar_profile", use_container_width=True):
                st.switch_page("pages/6_My_Profile.py")
            
            # Show forget me option if remember_me is enabled
            clear_remember = False
            if st.session_state.get('remember_me_enabled', False):
                clear_remember = st.checkbox("Forget me on logout", value=False, 
                                           key="sidebar_forget_me_checkbox",
                                           help="Check this to completely log out and disable auto-login")
            
            # Logout button
            if st.button("Logout", key="sidebar_logout", use_container_width=True):
                logout_user(clear_remember_me=clear_remember)

def get_base_url():
    """
    Attempt to get the base URL of the current application
    
    This function tries different methods to obtain the base URL
    for constructing invitation links
    
    Returns:
        str: The base URL or empty string if not found
    """
    base_url = ""
    try:
        # Method 1: Try to get the base URL from server options
        if hasattr(st, 'get_option'):
            server_url = st.get_option("server.baseUrl")
            if server_url:
                base_url = server_url
                logger.info(f"Got base URL from server.baseUrl: {base_url}")
                return base_url
                
        # Method 2: Try to get from the runtime
        import urllib.parse
        try:
            runtime = st.runtime.get_instance()
            if runtime and hasattr(runtime, '_session_mgr'):
                sessions = runtime._session_mgr.list_active_sessions()
                if sessions:
                    req = runtime._session_mgr.get_active_session_info(sessions[0]).request
                    if req:
                        protocol = req.protocol or "http"
                        host = req.host or "localhost"
                        joinme = (protocol, host, "", "", "", "")
                        base_url = urllib.parse.urlunparse(joinme)
                        logger.info(f"Got base URL from runtime: {base_url}")
                        return base_url
        except Exception as e:
            logger.warning(f"Error getting base URL from runtime: {e}")
            
        # Method 3: Use a default based on environment
        import os
        base_url = os.environ.get("STREAMLIT_SERVER_BASE_URL", "")
        if base_url:
            logger.info(f"Got base URL from environment: {base_url}")
            
    except Exception as e:
        logger.error(f"Error getting base URL: {e}")
        
    return base_url 