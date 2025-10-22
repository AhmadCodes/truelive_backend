"""
User management API endpoints.
Only super admins can create, update, and delete users.
"""

from fastapi import APIRouter, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
import uuid

from app.api.deps import SuperAdminUser, AdminUser, DBSession
from app.models.user import User
from app.schemas.auth import (
    UserCreate,
    UserUpdate,
    UserResponse,
    UserDetailResponse,
    PasswordReset
)
from app.core.security import get_password_hash, validate_password_strength
from app.services.audit_service import log_user_action, AuditAction, ResourceType


router = APIRouter()


@router.post("", response_model=UserDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_data: UserCreate,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Create a new user.

    Only super admins can create users.
    Allowed roles: user, admin, super_admin

    Args:
        user_data: User creation data
        current_user: Current authenticated super admin
        db: Database session

    Returns:
        Created user details

    Raises:
        HTTPException: If username or email already exists
    """
    # Check if username already exists
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )

    # Check if email already exists
    existing_email = db.query(User).filter(User.email == user_data.email).first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Validate password strength
    is_valid, error_msg = validate_password_strength(user_data.password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )

    # Create new user
    new_user = User(
        user_id=uuid.uuid4(),
        username=user_data.username,
        full_name=user_data.full_name,
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        role=user_data.role,
        is_active=user_data.is_active,
        created_by=current_user.user_id
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Log user creation
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.USER_CREATED,
        resource_type=ResourceType.USER,
        user_id=current_user.user_id,
        resource_id=str(new_user.user_id),
        changes={
            "username": user_data.username,
            "full_name": user_data.full_name,
            "email": user_data.email,
            "role": user_data.role,
            "is_active": user_data.is_active
        }
    )

    return new_user


@router.get("", response_model=List[UserDetailResponse])
async def list_users(
    current_user: AdminUser,
    db: DBSession,
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(50, ge=1, le=100, description="Number of records to return"),
    role: Optional[str] = Query(None, description="Filter by role (user, admin, super_admin)"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    search: Optional[str] = Query(None, description="Search by username or email")
):
    """
    List all users with optional filtering.

    Admins and super admins can view all users.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session
        skip: Number of records to skip (pagination)
        limit: Number of records to return (pagination)
        role: Filter by user role
        is_active: Filter by active status
        search: Search by username or email

    Returns:
        List of users
    """
    query = db.query(User)

    # Apply filters
    if role:
        if role not in ["user", "admin", "super_admin"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid role. Must be one of: user, admin, super_admin"
            )
        query = query.filter(User.role == role)

    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (User.username.ilike(search_filter)) |
            (User.full_name.ilike(search_filter)) |
            (User.email.ilike(search_filter))
        )

    # Order by created_at descending
    query = query.order_by(User.created_at.desc())

    # Apply pagination
    users = query.offset(skip).limit(limit).all()

    return users


@router.get("/count")
async def count_users(
    current_user: AdminUser,
    db: DBSession,
    role: Optional[str] = Query(None, description="Filter by role"),
    is_active: Optional[bool] = Query(None, description="Filter by active status")
):
    """
    Get total count of users with optional filters.

    Args:
        current_user: Current authenticated admin or super admin
        db: Database session
        role: Filter by user role
        is_active: Filter by active status

    Returns:
        Total count of users matching filters
    """
    query = db.query(func.count(User.user_id))

    if role:
        query = query.filter(User.role == role)

    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    total = query.scalar()

    return {"total": total}


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user(
    user_id: uuid.UUID,
    current_user: AdminUser,
    db: DBSession
):
    """
    Get single user by ID.

    Admins and super admins can view any user.

    Args:
        user_id: User UUID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        User details

    Raises:
        HTTPException: If user not found
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return user


@router.patch("/{user_id}", response_model=UserDetailResponse)
async def update_user(
    user_id: uuid.UUID,
    user_data: UserUpdate,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Update user details (partial update supported).

    Only super admins can update users.

    Args:
        user_id: User UUID
        user_data: User update data
        current_user: Current authenticated super admin
        db: Database session

    Returns:
        Updated user details

    Raises:
        HTTPException: If user not found or email already exists
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Prevent super admin from demoting themselves
    if user.user_id == current_user.user_id and user_data.role and user_data.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own role"
        )

    # Prevent super admin from deactivating themselves
    if user.user_id == current_user.user_id and user_data.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account"
        )

    # Track changes for audit log
    changes = {"old": {}, "new": {}}

    # Update full_name if provided
    if user_data.full_name:
        changes["old"]["full_name"] = user.full_name
        changes["new"]["full_name"] = user_data.full_name
        user.full_name = user_data.full_name

    # Update email if provided
    if user_data.email:
        # Check if email already exists for another user
        existing_email = db.query(User).filter(
            User.email == user_data.email,
            User.user_id != user_id
        ).first()

        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        changes["old"]["email"] = user.email
        changes["new"]["email"] = user_data.email
        user.email = user_data.email

    # Update role if provided
    if user_data.role:
        changes["old"]["role"] = user.role
        changes["new"]["role"] = user_data.role
        user.role = user_data.role

    # Update is_active if provided
    if user_data.is_active is not None:
        changes["old"]["is_active"] = user.is_active
        changes["new"]["is_active"] = user_data.is_active
        user.is_active = user_data.is_active

    db.commit()
    db.refresh(user)

    # Log user update if there were changes
    if changes["old"]:
        log_user_action(
            db=db,
            request=request,
            action=AuditAction.USER_UPDATED,
            resource_type=ResourceType.USER,
            user_id=current_user.user_id,
            resource_id=str(user.user_id),
            changes=changes
        )

    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Delete a user.

    Only super admins can delete users.
    Super admins cannot delete themselves.

    Args:
        user_id: User UUID
        current_user: Current authenticated super admin
        db: Database session

    Raises:
        HTTPException: If user not found or trying to delete self
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Prevent super admin from deleting themselves
    if user.user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )

    # Log user deletion before deleting
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.USER_DELETED,
        resource_type=ResourceType.USER,
        user_id=current_user.user_id,
        resource_id=str(user.user_id),
        changes={
            "username": user.username,
            "email": user.email,
            "role": user.role
        }
    )

    db.delete(user)
    db.commit()

    return None


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: uuid.UUID,
    password_data: PasswordReset,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Reset a user's password.

    Only super admins can reset user passwords.

    Args:
        user_id: User UUID
        password_data: New password data
        current_user: Current authenticated super admin
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If user not found or password is weak
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Validate password strength
    is_valid, error_msg = validate_password_strength(password_data.new_password)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )

    # Update password
    user.password_hash = get_password_hash(password_data.new_password)
    db.commit()

    # Log password reset
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.USER_PASSWORD_RESET,
        resource_type=ResourceType.USER,
        user_id=current_user.user_id,
        resource_id=str(user.user_id),
        changes={"username": user.username}
    )

    return {"message": f"Password reset successfully for user {user.username}"}


@router.patch("/{user_id}/activate")
async def activate_user(
    user_id: uuid.UUID,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Activate a user account.

    Only super admins can activate users.

    Args:
        user_id: User UUID
        current_user: Current authenticated super admin
        db: Database session

    Returns:
        Updated user details

    Raises:
        HTTPException: If user not found
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.is_active = True
    db.commit()
    db.refresh(user)

    # Log user activation
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.USER_ACTIVATED,
        resource_type=ResourceType.USER,
        user_id=current_user.user_id,
        resource_id=str(user.user_id),
        changes={"username": user.username}
    )

    return {"message": f"User {user.username} activated successfully", "user": user}


@router.patch("/{user_id}/deactivate")
async def deactivate_user(
    user_id: uuid.UUID,
    current_user: SuperAdminUser,
    db: DBSession,
    request: Request
):
    """
    Deactivate a user account.

    Only super admins can deactivate users.
    Super admins cannot deactivate themselves.

    Args:
        user_id: User UUID
        current_user: Current authenticated super admin
        db: Database session

    Returns:
        Updated user details

    Raises:
        HTTPException: If user not found or trying to deactivate self
    """
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Prevent super admin from deactivating themselves
    if user.user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account"
        )

    user.is_active = False
    db.commit()
    db.refresh(user)

    # Log user deactivation
    log_user_action(
        db=db,
        request=request,
        action=AuditAction.USER_DEACTIVATED,
        resource_type=ResourceType.USER,
        user_id=current_user.user_id,
        resource_id=str(user.user_id),
        changes={"username": user.username}
    )

    return {"message": f"User {user.username} deactivated successfully", "user": user}
