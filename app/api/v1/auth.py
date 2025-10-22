"""
Authentication API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import Annotated

from app.database import get_db
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    decode_token,
    validate_password_strength
)
from app.core.config import settings
from app.models.user import User, InvitationToken
from app.schemas.auth import (
    Login,
    Token,
    TokenRefresh,
    UserResponse,
    UserCreate,
    PasswordChange,
    EmailUpdate
)
from app.schemas.invitation import RegisterWithInvitationRequest
from app.api.deps import CurrentUser, DBSession
from app.services.audit_service import log_user_action, AuditAction, ResourceType


router = APIRouter()


@router.post("/login", response_model=Token)
async def login(
    login_data: Login,
    db: DBSession,
    request: Request
):
    """
    User login endpoint.

    Returns JWT access and refresh tokens.
    """
    user = db.query(User).filter(User.username == login_data.username).first()

    if not user or not verify_password(login_data.password, user.password_hash):
        # Log failed login attempt
        log_user_action(
            db=db,
            request=request,
            action=AuditAction.LOGIN_FAILED,
            resource_type=ResourceType.AUTH,
            user_id=None,
            changes={"username": login_data.username}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )

    # Update last login timestamp
    user.last_login = datetime.now(timezone.utc)
    db.commit()

    # Log successful login
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.LOGIN_SUCCESS,
        resource_type=ResourceType.AUTH,
        user_id=user.user_id,
        changes={"username": user.username}
    )

    # Create tokens
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    if login_data.remember_me:
        # Extend token expiry for remember me
        access_token_expires = timedelta(days=7)

    access_token = create_access_token(
        subject=str(user.user_id),
        expires_delta=access_token_expires
    )

    refresh_token = create_refresh_token(
        subject=str(user.user_id)
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=int(access_token_expires.total_seconds())
    )


@router.post("/register-with-invitation", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_with_invitation(
    registration_data: RegisterWithInvitationRequest,
    db: DBSession
):
    """
    Register a new user using an invitation token.

    Validates the invitation token and creates a new user account.
    """
    # Find the invitation token
    invitation = db.query(InvitationToken).filter(
        InvitationToken.token == registration_data.token
    ).first()

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This invitation link is no longer valid. It may have expired, been revoked, or replaced with a newer invitation. Please contact your administrator for a new invitation."
        )

    # Check if token is already used
    if invitation.is_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invitation has already been used"
        )

    # Check if token is expired
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invitation has expired"
        )

    # Check if username already exists
    existing_user = db.query(User).filter(User.username == registration_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{registration_data.username}' already exists"
        )

    # Check if email already registered (shouldn't happen but double-check)
    existing_email = db.query(User).filter(User.email == invitation.email).first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{invitation.email}' is already registered"
        )

    # Validate password strength
    is_valid, error_msg = validate_password_strength(registration_data.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )

    # Create new user
    new_user = User(
        username=registration_data.username,
        full_name=registration_data.full_name,
        email=invitation.email,
        password_hash=get_password_hash(registration_data.password),
        role=invitation.role,
        is_active=True,
        created_by=invitation.invited_by_id
    )

    db.add(new_user)
    db.flush()  # Get the user_id before committing

    # Mark invitation as used
    invitation.is_used = True
    invitation.used_at = datetime.now(timezone.utc)
    invitation.user_id = new_user.user_id
    # Note: IP address tracking would require request object

    db.commit()
    db.refresh(new_user)

    return new_user


@router.post("/refresh", response_model=Token)
async def refresh_token(
    refresh_data: TokenRefresh,
    db: DBSession
):
    """
    Refresh access token using refresh token.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(refresh_data.refresh_token)
        user_id: str = payload.get("sub")
        token_type: str = payload.get("type")

        if user_id is None or token_type != "refresh":
            raise credentials_exception

    except Exception:
        raise credentials_exception

    user = db.query(User).filter(User.user_id == user_id).first()

    if user is None or not user.is_active:
        raise credentials_exception

    # Create new access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        subject=str(user.user_id),
        expires_delta=access_token_expires
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_data.refresh_token,  # Return same refresh token
        token_type="bearer",
        expires_in=int(access_token_expires.total_seconds())
    )


@router.post("/logout")
async def logout(
    current_user: CurrentUser
):
    """
    Logout endpoint.

    In a stateless JWT system, actual logout happens on client side by
    discarding the tokens. This endpoint can be used for audit logging.
    """
    return {"message": "Successfully logged out"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: CurrentUser
):
    """
    Get current user information.
    """
    return current_user


@router.patch("/me/email", response_model=UserResponse)
async def update_own_email(
    email_update: EmailUpdate,
    current_user: CurrentUser,
    db: DBSession,
    request: Request
):
    """
    Update current user's email.
    """
    # Check if email already exists
    existing_user = db.query(User).filter(
        User.email == email_update.email,
        User.user_id != current_user.user_id
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Track old email for audit log
    old_email = current_user.email

    current_user.email = email_update.email
    db.commit()
    db.refresh(current_user)

    # Log email update
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.EMAIL_UPDATE,
        resource_type=ResourceType.AUTH,
        user_id=current_user.user_id,
        changes={"old_email": old_email, "new_email": email_update.email}
    )

    return current_user


@router.post("/me/password")
async def change_own_password(
    password_change: PasswordChange,
    current_user: CurrentUser,
    db: DBSession,
    request: Request
):
    """
    Change current user's password.
    """
    # Verify current password
    if not verify_password(password_change.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect"
        )

    # Validate new password strength
    is_valid, error_msg = validate_password_strength(password_change.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )

    # Update password
    current_user.password_hash = get_password_hash(password_change.new_password)
    db.commit()

    # Log password change
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.PASSWORD_CHANGE,
        resource_type=ResourceType.AUTH,
        user_id=current_user.user_id,
        changes={"username": current_user.username}
    )

    return {"message": "Password changed successfully"}
