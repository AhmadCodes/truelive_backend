import streamlit as st
import logging
from database import Database, User
import time

# Configure logging
logger = logging.getLogger(__name__)

def check_authentication():
    """
    Check if the user is authenticated and the session is valid.
    Redirects to login page if not authenticated.
    
    Returns:
        bool: True if authenticated, False otherwise
    """
    # Check if basic session state for auth exists
    if not st.session_state.get('logged_in') or not st.session_state.get('user_id'):
        logger.warning("User not logged in, redirecting to login")
        st.warning("Please log in to access this page")
        
        # Add a login button that redirects to the main page
        if st.button("Go to Login", use_container_width=True):
            st.switch_page("main.py")
        
        # Stop execution of the rest of the page
        st.stop()
        return False
    
    # Verify user still exists in database and is active
    try:
        db = Database()
        user = db.get_user_by_id(st.session_state['user_id'])
        
        if not user:
            logger.warning(f"User ID {st.session_state['user_id']} not found in database")
            st.error("Your account was not found. Please log in again.")
            
            # Force logout by clearing session state
            for key in ['logged_in', 'user_id', 'user_role']:
                if key in st.session_state:
                    del st.session_state[key]
            
            # Redirect to login page
            if st.button("Go to Login", use_container_width=True):
                st.switch_page("main.py")
            
            # Stop execution of the rest of the page
            st.stop()
            return False
        
        if not user.is_active:
            logger.warning(f"Inactive user {user.id} attempted to access page")
            st.error("Your account has been deactivated. Please contact your administrator.")
            
            # Force logout by clearing session state
            for key in ['logged_in', 'user_id', 'user_role']:
                if key in st.session_state:
                    del st.session_state[key]
            
            # Redirect to login page
            if st.button("Go to Login", use_container_width=True):
                st.switch_page("main.py")
            
            # Stop execution of the rest of the page
            st.stop()
            return False
        
        # Refresh the session's role information to ensure it's current
        if st.session_state.get('user_role') != user.role:
            logger.info(f"Updating user role for {user.id} from {st.session_state.get('user_role')} to {user.role}")
            st.session_state['user_role'] = user.role
        
        # All checks passed
        return True
    except Exception as e:
        logger.error(f"Error during authentication check: {e}")
        st.error("An error occurred verifying your session. Please try logging in again.")
        
        # Add a login button that redirects to the main page
        if st.button("Go to Login", use_container_width=True):
            st.switch_page("main.py")
        
        # Stop execution of the rest of the page
        st.stop()
        return False

def check_role_permission(required_role: str) -> bool:
    """
    Check if the current user has the required role permission.
    
    Args:
        required_role (str): The minimum required role ('admin' or 'super_admin')
        
    Returns:
        bool: True if user has permission, False otherwise
    """
    # First verify the user is authenticated
    if not check_authentication():
        return False
        
    # Get the user's role from session state
    user_role = st.session_state.get('user_role', '')
    
    # Check permission based on required role
    if required_role == 'admin':
        if user_role not in ['admin', 'super_admin']:
            logger.warning(f"User {st.session_state.get('user_id')} with role {user_role} attempted to access admin feature")
            st.error("You don't have permission to access this feature")
            return False
    elif required_role == 'super_admin':
        if user_role != 'super_admin':
            logger.warning(f"User {st.session_state.get('user_id')} with role {user_role} attempted to access super admin feature")
            st.error("You don't have permission to access this feature")
            return False
    
    # Permission granted
    return True

def get_current_user() -> User:
    """
    Get the current authenticated user from the database.
    Must be called after check_authentication() to ensure user exists.
    
    Returns:
        User: The current user object or None if not found
    """
    try:
        db = Database()
        return db.get_user_by_id(st.session_state.get('user_id'))
    except Exception as e:
        logger.error(f"Error retrieving current user: {e}")
        return None

def logout_user(clear_remember_me=False):
    """
    Log out the current user and redirect to login page.
    
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
        
        # Redirect to login page
        st.switch_page("main.py")
    except Exception as e:
        logger.error(f"Error during logout: {e}")
        # Force reset of critical session states as fallback
        st.session_state['logged_in'] = False
        st.session_state['user_id'] = None
        st.session_state['user_role'] = None
        st.rerun()

def set_logged_in_user(user):
    """
    Set the current logged in user in the session state
    
    Args:
        user (dict): The user dictionary containing user information
    """
    try:
        # Set session state variables for authentication
        st.session_state['logged_in'] = True
        st.session_state['user_id'] = user['user_id']
        st.session_state['user_role'] = user['role']
        
        logger.info(f"User {user['username']} (role: {user['role']}) logged in successfully")
    except Exception as e:
        logger.error(f"Error setting logged in user: {e}")
        # Ensure session is in a consistent state
        st.session_state['logged_in'] = False
        st.session_state['user_id'] = None
        st.session_state['user_role'] = None

def check_user_logged_in():
    """
    Check if a user is currently logged in
    
    Returns:
        bool: True if user is logged in, False otherwise
    """
    return st.session_state.get('logged_in', False) and st.session_state.get('user_id') is not None 