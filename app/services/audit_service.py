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
    user_agent: Optional[str] = None,
    *,
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    actor_label: Optional[str] = None,
    commit: bool = True,
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
        actor_type: Actor kind ('user' | 'service_account' | 'system'). When a
            human user performs the action, populate BOTH user_id and actor_id.
        actor_id: User UUID (as text) or service-account id of the actor.
        actor_label: Display name of the actor, frozen at action time (audit rows
            are historical and never rewritten on rename/delete).
        commit: When True (default) commit + refresh immediately. Pass False to
            let the entry join the caller's transaction (used so an entity write
            and its audit row commit atomically).

    Returns:
        Created AuditLog instance
    """
    # Convert changes dict to JSON string if provided. default=str keeps
    # datetimes / UUIDs / Decimals in delete snapshots JSON-serialisable.
    changes_json = json.dumps(changes, default=str) if changes else None

    audit_log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes_json,
        ip_address=ip_address,
        user_agent=user_agent,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
    )

    db.add(audit_log)
    if commit:
        db.commit()
        db.refresh(audit_log)

    return audit_log


def _actor_kwargs(actor) -> dict[str, Any]:
    """Expand an ActorTriple (type, id, label) into create_audit_log kwargs.

    For a human user, populate BOTH user_id (back-compat) and actor_id.
    """
    actor_type, actor_id, actor_label = actor
    kwargs: dict[str, Any] = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "actor_label": actor_label,
    }
    if actor_type == "user" and actor_id:
        try:
            kwargs["user_id"] = UUID(str(actor_id))
        except (ValueError, TypeError):
            pass
    return kwargs


def record_create(db: Session, *, resource_type: str, resource_id: str, actor) -> AuditLog:
    """Audit a create. Joins the caller's transaction (commit=False)."""
    return create_audit_log(
        db, action=f"{resource_type}.created", resource_type=resource_type,
        resource_id=resource_id, commit=False, **_actor_kwargs(actor),
    )


def record_update(
    db: Session, *, resource_type: str, resource_id: str, actor,
    before: dict[str, Any], after: dict[str, Any], extra: Optional[dict[str, Any]] = None,
) -> Optional[AuditLog]:
    """Audit an update with a masked field-level {old,new} diff. Joins the caller's
    transaction. Returns None (writes nothing) when nothing actually changed and no
    `extra` payload is supplied.

    WARNING: `extra` is merged into `changes` VERBATIM (not masked). Only pass
    non-secret data (ids, counts, flags) — never raw secret field values.
    """
    from app.services.actor import diff_fields

    changes = diff_fields(resource_type, before, after)
    if extra:
        changes = {**changes, **extra}
    if not changes:
        return None
    return create_audit_log(
        db, action=f"{resource_type}.updated", resource_type=resource_type,
        resource_id=resource_id, changes=changes, commit=False, **_actor_kwargs(actor),
    )


def record_delete(
    db: Session, *, resource_type: str, resource_id: str, actor,
    snapshot: dict[str, Any], extra: Optional[dict[str, Any]] = None,
) -> AuditLog:
    """Audit a delete with a masked full-row snapshot. Joins the caller's
    transaction. Capture `snapshot` BEFORE db.delete() so cascades don't blank it.

    WARNING: `extra` is merged into `changes` VERBATIM (not masked). Only pass
    non-secret data — never raw secret field values."""
    from app.services.actor import mask_payload

    payload: dict[str, Any] = {"snapshot": mask_payload(resource_type, snapshot)}
    if extra:
        payload.update(extra)
    return create_audit_log(
        db, action=f"{resource_type}.deleted", resource_type=resource_type,
        resource_id=resource_id, changes=payload, commit=False, **_actor_kwargs(actor),
    )


def record_event(
    db: Session, *, action: str, resource_type: str, resource_id: str, actor,
    changes: Optional[dict[str, Any]] = None, commit: bool = False,
) -> AuditLog:
    """Audit an arbitrary event (e.g. a bulk aggregate like 'pc.screens.configured')."""
    return create_audit_log(
        db, action=action, resource_type=resource_type, resource_id=resource_id,
        changes=changes, commit=commit, **_actor_kwargs(actor),
    )


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
    changes: Optional[dict[str, Any]] = None,
    actor_label: Optional[str] = None,
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
        actor_label: Optional display name of the acting user (frozen at write time).

    Returns:
        Created AuditLog instance
    """
    # Existing human/auth callers pass user_id; mirror it into the actor triple so
    # these rows carry actor_type='user' alongside the legacy user_id column.
    actor_type = "user" if user_id is not None else "system"
    actor_id = str(user_id) if user_id is not None else None
    return create_audit_log(
        db=db,
        action=action,
        resource_type=resource_type,
        user_id=user_id,
        resource_id=resource_id,
        changes=changes,
        ip_address=get_client_ip(request),
        user_agent=get_user_agent(request),
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
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

    # Device management
    DEVICE_CREATED = "device.created"
    DEVICE_UPDATED = "device.updated"
    DEVICE_DELETED = "device.deleted"

    # Team management
    TEAM_CREATED = "team.created"
    TEAM_UPDATED = "team.updated"
    TEAM_DELETED = "team.deleted"

    # PC/Screen management
    PC_CREATED = "pc.created"
    PC_UPDATED = "pc.updated"
    PC_DELETED = "pc.deleted"
    SCREEN_CREATED = "screen.created"
    SCREEN_UPDATED = "screen.updated"
    SCREEN_DELETED = "screen.deleted"
    VIEW_CREATED = "view.created"
    VIEW_UPDATED = "view.updated"
    VIEW_DELETED = "view.deleted"
    SCREEN_MAPPING_CREATED = "screen_mapping.created"
    SCREEN_MAPPING_UPDATED = "screen_mapping.updated"
    SCREEN_MAPPING_DELETED = "screen_mapping.deleted"

    # Screen layout management
    LAYOUT_CREATED = "layout.created"
    LAYOUT_UPDATED = "layout.updated"
    LAYOUT_DELETED = "layout.deleted"

    # Bulk / aggregate operations
    PC_SCREENS_CONFIGURED = "pc.screens.configured"
    PC_CONFIG_IMPORTED = "pc.config.imported"
    LAYOUT_COPIED = "layout.copied"

    CONFIG_DEPLOYED = "config.deployed"


class ResourceType:
    """Constants for resource types."""

    AUTH = "auth"
    USER = "user"
    INVITATION = "invitation"
    SITE = "site"
    DEVICE = "device"
    CAMERA = "camera"
    PC = "pc"
    TEAM = "team"
    LAYOUT = "layout"
    SCREEN = "screen"
    VIEW = "view"
    SCREEN_MAPPING = "screen_mapping"
    CONFIG = "config"
