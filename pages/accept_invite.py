# pages/accept_invite.py
import streamlit as st
import time
import secrets
import logging
from database import Database, User

# Configure logging
logger = logging.getLogger(__name__)

def accept_invite_page():
    """Handle user invitation acceptance with robust error handling"""
    st.set_page_config(page_title="Accept Invitation", page_icon="📨", layout="wide")
    
    st.title("Accept Invitation")
    
    # Check for token in URL with error handling
    try:
        token = st.query_params.get("token", None)
    except Exception as e:
        logger.error(f"Error accessing query parameters: {e}")
        token = None
    
    # Validate token exists
    if not token:
        st.error("No invitation token provided")
        st.markdown("Please use the link provided in your invitation email to access this page.")
        
        # Provide a way to go to main login page
        st.markdown("---")
        st.markdown("Already have an account?")
        
        if st.button("Go to Login Page", use_container_width=True):
            # Redirect to main page
            st.switch_page("main.py")
        return
    
    try:
        # Initialize database with error handling
        try:
            db = Database()
        except Exception as e:
            logger.error(f"Database connection error: {e}")
            st.error("System error: Unable to connect to the database. Please try again later.")
            return
        
        # Verify token with error handling
        try:
            user = db.get_user_by_invite_token(token)
        except Exception as e:
            logger.error(f"Error retrieving user by token: {e}")
            st.error("System error during token verification. Please try again or contact your administrator.")
            return
        
        if not user:
            st.error("Invalid invitation token")
            st.markdown("The invitation token is invalid or has been used already.")
            
            # Provide a way to go to main login page
            st.markdown("---")
            st.markdown("Already have an account?")
            
            if st.button("Go to Login Page", use_container_width=True):
                # Redirect to main page
                st.switch_page("main.py")
            return
        
        # Check if token is expired
        current_time = int(time.time())
        if user.token_expiry and user.token_expiry < current_time:
            token_expiry_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(user.token_expiry))
            st.error(f"Invitation has expired on {token_expiry_date}")
            st.markdown("This invitation has expired. Please contact your administrator for a new invitation.")
            
            # Provide a way to go to main login page
            st.markdown("---")
            st.markdown("Already have an account?")
            
            if st.button("Go to Login Page", use_container_width=True):
                # Redirect to main page
                st.switch_page("main.py")
            return
        
        # Show account information
        st.markdown(f"### Welcome, {user.username}!")
        st.markdown(f"Email: {user.email}")
        st.markdown("Please set your password to complete your registration.")
        
        # Password form with validation
        with st.form("password_form"):
            password = st.text_input("New Password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")
            
            # Password requirements explanation
            st.info("Password should be at least 8 characters long and include a mix of letters, numbers, and special characters.")
            
            submitted = st.form_submit_button("Set Password and Login", use_container_width=True)
            
            if submitted:
                # Validate password
                if not password:
                    st.error("Password is required")
                    return
                
                if len(password) < 8:
                    st.error("Password must be at least 8 characters long")
                    return
                
                if password != confirm_password:
                    st.error("Passwords do not match")
                    return
                
                try:
                    # Generate secure password hash
                    salt = secrets.token_hex(16)
                    password_hash = db._hash_password(password, salt)
                    user.password_hash = f"{salt}:{password_hash}"
                    
                    # Clear invitation token
                    user.invite_token = None
                    user.token_expiry = None
                    
                    # Update user with error handling
                    try:
                        db.update_user(user)
                    except Exception as e:
                        logger.error(f"Error updating user: {e}")
                        st.error("Failed to update account. Please try again or contact your administrator.")
                        return
                    
                    # Set session state for login
                    st.session_state['logged_in'] = True
                    st.session_state['user_id'] = user.id
                    st.session_state['user_role'] = user.role
                    
                    # Update last login time
                    try:
                        db.update_last_login(user.id)
                    except Exception as e:
                        logger.warning(f"Failed to update last login time: {e}")
                        # Non-critical error, continue
                    
                    # Success message with progress indicator
                    success_container = st.empty()
                    success_container.success("Password set successfully! Redirecting to dashboard...")
                    
                    # Progress bar for visual feedback
                    progress = st.progress(0)
                    for i in range(100):
                        time.sleep(0.01)
                        progress.progress(i + 1)
                    
                    # Clear query parameters
                    st.query_params.clear()
                    
                    # Redirect to main page
                    st.switch_page("main.py")
                except Exception as e:
                    logger.error(f"Error during password reset: {e}")
                    st.error("An unexpected error occurred. Please try again or contact support.")
    except Exception as e:
        logger.error(f"Unexpected error in accept_invite page: {e}")
        st.error("An unexpected error occurred. Please try again later or contact support.")

# Run the page
if __name__ == "__main__":
    accept_invite_page() 