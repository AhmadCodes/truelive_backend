"""
Dependency injection for FastAPI routes.
Handles authentication, authorization, and common dependencies.
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from jose import JWTError
from typing import Annotated
import uuid

from app.database import get_db
from app.core.security import decode_token
from app.models.user import User


# HTTP Bearer security scheme
security = HTTPBearer()


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[Session, Depends(get_db)]
) -> User:
    """
    Get current authenticated user from JWT token.

    Args:
        credentials: HTTP bearer credentials
        db: Database session

    Returns:
        Current user object

    Raises:
        HTTPException: If token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token = credentials.credentials
        payload = decode_token(token)

        user_id_str: str = payload.get("sub")
        token_type: str = payload.get("type")

        if user_id_str is None or token_type != "access":
            raise credentials_exception

        user_id = uuid.UUID(user_id_str)

    except (JWTError, ValueError):
        raise credentials_exception

    user = db.query(User).filter(User.user_id == user_id).first()

    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )

    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """
    Ensure current user is active.

    Args:
        current_user: Current authenticated user

    Returns:
        Active user object

    Raises:
        HTTPException: If user is not active
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user


async def require_role(
    required_role: str,
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> User:
    """
    Check if current user has required role.

    Args:
        required_role: Required role (user, admin, super_admin)
        current_user: Current authenticated user

    Returns:
        User if authorized

    Raises:
        HTTPException: If user doesn't have required role
    """
    role_hierarchy = {
        "user": 0,
        "admin": 1,
        "super_admin": 2
    }

    user_role_level = role_hierarchy.get(current_user.role, -1)
    required_role_level = role_hierarchy.get(required_role, 999)

    if user_role_level < required_role_level:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. {required_role} role required."
        )

    return current_user


def get_admin_user(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> User:
    """
    Ensure current user is admin or super_admin.

    Args:
        current_user: Current authenticated user

    Returns:
        Admin user

    Raises:
        HTTPException: If user is not admin
    """
    if current_user.role not in ["admin", "super_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required"
        )
    return current_user


def get_super_admin_user(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> User:
    """
    Ensure current user is super_admin.

    Args:
        current_user: Current authenticated user

    Returns:
        Super admin user

    Raises:
        HTTPException: If user is not super admin
    """
    if current_user.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin privileges required"
        )
    return current_user


# Type aliases for dependency injection
CurrentUser = Annotated[User, Depends(get_current_user)]
ActiveUser = Annotated[User, Depends(get_current_active_user)]
AdminUser = Annotated[User, Depends(get_admin_user)]
SuperAdminUser = Annotated[User, Depends(get_super_admin_user)]
DBSession = Annotated[Session, Depends(get_db)]
