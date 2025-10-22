"""
Audit logging service for tracking user actions and system events.
"""

from sqlalchemy.orm import Session
from typing import Optional, Any
from uuid import UUID
from fastapi import Request
import json

from app.models.user import AuditLog, User


def create_audit_log(
    db: Session,
    action: str,
    resource_type: str,
    user_id: Optional[UUID] = None,
    resource_id: Optional[str] = None,
    changes: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
) -> AuditLog:
    """
    Create an audit log entry.

    Args:
        db: Database session
        action: Action identifier (e.g., 'user.created', 'site.updated', 'login.success')
        resource_type: Type of resource (e.g., 'user', 'site', 'camera', 'auth')
        user_id: User who performed the action (optional for system actions)
        resource_id: ID of the affected resource (optional)
        changes: Dictionary of changes made (e.g., {"old": {...}, "new": {...}})
        ip_address: IP address of the user
        user_agent: User agent string from the request

    Returns:
        Created AuditLog instance
    """
    # Convert changes dict to JSON string if provided
    changes_json = json.dumps(changes) if changes else None

    audit_log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes_json,
        ip_address=ip_address,
        user_agent=user_agent
    )

    db.add(audit_log)
    db.commit()
    db.refresh(audit_log)

    return audit_log


def get_client_ip(request: Request) -> Optional[str]:
    """
    Extract client IP address from request.

    Checks X-Forwarded-For header first (for proxied requests),
    then falls back to direct client IP.

    Args:
        request: FastAPI request object

    Returns:
        Client IP address or None
    """
    # Check for X-Forwarded-For header (proxy/load balancer)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, take the first one
        return forwarded_for.split(",")[0].strip()

    # Fall back to direct client IP
    if request.client:
        return request.client.host

    return None


def get_user_agent(request: Request) -> Optional[str]:
    """
    Extract user agent string from request.

    Args:
        request: FastAPI request object

    Returns:
        User agent string or None
    """
    return request.headers.get("User-Agent")


def log_user_action(
    db: Session,
    request: Request,
    action: str,
    resource_type: str,
    user_id: Optional[UUID] = None,
    resource_id: Optional[str] = None,
    changes: Optional[dict[str, Any]] = None
) -> AuditLog:
    """
    Convenience function to log user action with automatic IP and user agent extraction.

    Args:
        db: Database session
        request: FastAPI request object
        action: Action identifier
        resource_type: Type of resource
        user_id: User who performed the action
        resource_id: ID of the affected resource
        changes: Dictionary of changes made

    Returns:
        Created AuditLog instance
    """
    return create_audit_log(
        db=db,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        resource_id=resource_id,
        changes=changes,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request)
    )


# Common audit log actions
class AuditAction:
    """Constants for common audit log actions."""

    # Authentication
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILED = "auth.login.failed"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"
    PASSWORD_CHANGE = "auth.password.change"
    EMAIL_UPDATE = "auth.email.update"

    # User management
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DELETED = "user.deleted"
    USER_ACTIVATED = "user.activated"
    USER_DEACTIVATED = "user.deactivated"
    USER_PASSWORD_RESET = "user.password.reset"

    # Invitation management
    INVITATION_SENT = "invitation.sent"
    INVITATION_USED = "invitation.used"
    INVITATION_REVOKED = "invitation.revoked"

    # Site management
    SITE_CREATED = "site.created"
    SITE_UPDATED = "site.updated"
    SITE_DELETED = "site.deleted"

    # Camera management
    CAMERA_CREATED = "camera.created"
    CAMERA_UPDATED = "camera.updated"
    CAMERA_DELETED = "camera.deleted"

    # PC/Screen management
    PC_CREATED = "pc.created"
    PC_UPDATED = "pc.updated"
    PC_DELETED = "pc.deleted"
    SCREEN_CREATED = "screen.created"
    SCREEN_UPDATED = "screen.updated"
    SCREEN_DELETED = "screen.deleted"
    CONFIG_DEPLOYED = "config.deployed"


class ResourceType:
    """Constants for resource types."""

    AUTH = "auth"
    USER = "user"
    INVITATION = "invitation"
    SITE = "site"
    CAMERA = "camera"
    PC = "pc"
    SCREEN = "screen"
    VIEW = "view"
    CONFIG = "config"
