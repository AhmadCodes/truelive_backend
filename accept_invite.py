# Footer
st.markdown("<div class='centered-text'>© 2025 Shomer</div>", unsafe_allow_html=True) 

import streamlit as st
import time
import logging
from database import Database
from utils.auth_utils import logout_user
from utils.layout import add_notification

# Configure logging
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Accept Invitation",
    page_icon="🛡️",
    layout="centered"
)

# Custom CSS
st.markdown("""
<style>
    .centered-text {
        text-align: center;
    }
    .stButton button {
        width: 100%;
    }
    .main .block-container {
        padding-top: 2rem;
    }
</style>
""", unsafe_allow_html=True)

def accept_invite():
    """Handle user invitation acceptance"""
    
    # App title
    st.markdown("<h1 class='centered-text'>🛡️ Shomer Portal - Accept Invitation</h1>", unsafe_allow_html=True)
    
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
    
    # Get token from URL parameters
    token = st.query_params.get("token", "")
    
    # Initialize database connection
    db = Database()
    
    if not token:
        st.error("Invalid invitation link. No token provided.")
        st.info("Please contact your administrator for a valid invitation link.")
        return
    
    # Try to get user by token
    user = db.get_user_by_token(token)
    
    if not user:
        st.error("Invalid or expired invitation link.")
        st.info("Please contact your administrator for a new invitation link.")
        return
    
    # Display welcome message
    st.success(f"Welcome, {user.username}! Please set your password to complete your account setup.")
    
    # Password form
    with st.form("password_form"):
        password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm Password", type="password")
        submit = st.form_submit_button("Set Password & Login")
        
        if submit:
            # Validate password
            if not password:
                st.error("Password cannot be empty.")
                return
                
            if password != confirm_password:
                st.error("Passwords do not match.")
                return
                
            if len(password) < 8:
                st.error("Password must be at least 8 characters long.")
                return
            
            try:
                # Update user's password with more robust approach
                success = db.update_user(
                    user_id=user.id,
                    password=password,
                    is_active=True  # Ensure account is activated
                )
                
                if success:
                    # Mark invitation token as used
                    token_consumed = db.consume_invitation_token(token)
                    
                    if token_consumed:
                        # Show success message
                        st.success("Password set successfully! You can now log in.")
                        
                        # Redirect to login page after 3 seconds
                        st.markdown(
                            """
                            <meta http-equiv="refresh" content="3;url=/">
                            <p>Redirecting to login page in 3 seconds...</p>
                            """, 
                            unsafe_allow_html=True
                        )
                    else:
                        st.warning("Password was set but there was an issue marking the invitation as used. You can still log in with your new password.")
                        
                        # Redirect to login page after 5 seconds
                        st.markdown(
                            """
                            <meta http-equiv="refresh" content="5;url=/">
                            <p>Redirecting to login page in 5 seconds...</p>
                            """, 
                            unsafe_allow_html=True
                        )
                else:
                    st.error("There was a problem setting your password. Please try again or contact an administrator.")
                
            except Exception as e:
                logger.error(f"Error setting password: {e}")
                st.error(f"Error setting password: {str(e)}")

# Run the accept invite flow
accept_invite() 