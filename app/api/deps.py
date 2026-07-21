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


# ============================================================================ #
# Service-account auth (non-human principals, e.g. a downstream platform)
#
# Tokens have a `tlsa_` prefix so they're easy to grep in logs and obviously
# distinct from JWTs. Stored hashed (bcrypt); verify with passlib.
# Scopes are checked via require_scope() — see scoping in spec §12.2.
# ============================================================================ #


async def get_current_service_account(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Resolve a service account from a `Bearer tlsa_<secret>` token. Returns the
    ServiceAccount row. Raises 401 on any failure.

    Imported lazily so this module stays importable in environments where the
    alerting models haven't been migrated yet.
    """
    from datetime import datetime, timezone
    from passlib.hash import bcrypt  # type: ignore
    from app.models.service_account import ServiceAccount, ServiceAccountToken

    raw = credentials.credentials or ""
    if not raw.startswith("tlsa_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Service account token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    now = datetime.now(timezone.utc)
    # Bcrypt verify against every active token. With <100 active tokens this is
    # ~50ms total — acceptable for our scale. A lookup-table optimization (HMAC
    # of the raw token as a fast index) is a follow-up if this becomes a hotspot.
    candidates = (
        db.query(ServiceAccountToken)
        .join(ServiceAccount, ServiceAccountToken.service_account_id == ServiceAccount.id)
        .filter(
            ServiceAccountToken.revoked_at.is_(None),
            ServiceAccount.is_active == True,  # noqa: E712
        )
        .all()
    )
    matched: ServiceAccountToken | None = None
    for tok in candidates:
        if tok.expires_at and tok.expires_at < now:
            continue
        try:
            if bcrypt.verify(raw, tok.token_hash):
                matched = tok
                break
        except Exception:
            continue

    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service-account token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    matched.last_used_at = now
    db.commit()

    return matched.service_account


def require_scope(*required_scopes: str):
    """
    Build a dependency that asserts the service account has at least one of the
    given scopes. Use as `Depends(require_scope('alerts:read'))`.
    """

    async def _check(
        sa = Depends(get_current_service_account),
    ):
        scopes = set(sa.scopes or [])
        if not any(s in scopes for s in required_scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope ({' or '.join(required_scopes)})",
            )
        return sa

    return _check


# ============================================================================ #
# Hybrid auth dependencies — accept EITHER a human JWT user OR a service
# account with one of the listed scopes. The bearer token's prefix determines
# which path runs: `tlsa_*` → service account, anything else → JWT.
#
# Used by sites, devices + cameras endpoints (and any future resource that should be
# reachable by both humans and machine integrations).
# ============================================================================ #


def user_or_scope(*scopes: str):
    """
    Allow any active JWT user OR a service account with one of `scopes`.

    Used for read-only endpoints that all authenticated humans can hit, plus
    service accounts with the appropriate read (or manage, since manage implies
    read) scope.
    """

    async def _dep(
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
        db: Annotated[Session, Depends(get_db)],
    ):
        raw = credentials.credentials or ""
        if raw.startswith("tlsa_"):
            sa = await get_current_service_account(credentials, db)
            sa_scopes = set(sa.scopes or [])
            if not (sa_scopes & set(scopes)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing required scope ({' or '.join(scopes)})",
                )
            return sa
        return await get_current_user(credentials, db)

    return _dep


def admin_or_scope(*scopes: str):
    """
    Allow admin/super-admin JWT user OR a service account with one of `scopes`.

    Used for write endpoints that admins (or service accounts granted the
    manage scope) can hit.
    """

    async def _dep(
        credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
        db: Annotated[Session, Depends(get_db)],
    ):
        raw = credentials.credentials or ""
        if raw.startswith("tlsa_"):
            sa = await get_current_service_account(credentials, db)
            sa_scopes = set(sa.scopes or [])
            if not (sa_scopes & set(scopes)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing required scope ({' or '.join(scopes)})",
                )
            return sa
        user = await get_current_user(credentials, db)
        if user.role not in ("admin", "super_admin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin privileges required",
            )
        return user

    return _dep


# Type aliases for dependency injection
CurrentUser = Annotated[User, Depends(get_current_user)]
ActiveUser = Annotated[User, Depends(get_current_active_user)]
AdminUser = Annotated[User, Depends(get_admin_user)]
SuperAdminUser = Annotated[User, Depends(get_super_admin_user)]
DBSession = Annotated[Session, Depends(get_db)]
ServiceAccountAuth = Annotated["ServiceAccount", Depends(get_current_service_account)]  # noqa: F821
