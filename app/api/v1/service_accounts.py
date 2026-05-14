"""
Admin endpoints for service-account auth.

A **service account** is a non-human principal — typically a downstream
platform that needs to call TrueLive APIs. It holds one or more scoped bearer
tokens (`tlsa_<...>`) used as `Authorization: Bearer ...`. Endpoints under
this tag are admin-only because they mint and manage credentials.

Typical onboarding flow for a new integration:

1. Admin creates the service account here, picking the right `scopes`.
2. Admin issues a token via `POST /service-accounts/{id}/tokens`. The raw
   token is returned ONCE — copy it into a secret manager immediately.
3. Hand off the token to the downstream platform out of band (Bitwarden,
   1Password, etc.). Their integration then sends it as
   `Authorization: Bearer tlsa_<...>` on every request.
4. Rotate: when issuing a replacement token, soft-revoke the old one with
   `DELETE /service-accounts/{id}/tokens/{token_id}` after the downstream
   platform confirms the new token is in use.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AdminUser, DBSession
from app.models.service_account import ServiceAccount, ServiceAccountToken
from app.schemas.service_account import (
    ServiceAccountCreate, ServiceAccountUpdate, ServiceAccountResponse,
    ServiceAccountTokenCreate, ServiceAccountTokenResponse,
    ServiceAccountTokenWithSecret,
)
from app.utils.secrets_gen import generate_service_account_token


router = APIRouter()


# Reusable description blocks. Pulled out so the scope reference appears once
# and is reused across the GET/POST/PATCH summaries.

_SCOPE_TABLE = """
## Available scopes

| Scope               | Grants                                                          |
|---------------------|-----------------------------------------------------------------|
| `alerts:read`       | `GET /alerts`, `GET /alerts/{id}`, `GET /alerts/{id}/deliveries`, `GET /alerts/{id}/media/{media_id}` |
| `alerts:raw:read`   | `GET /alerts/{id}/raw` — the raw RFC822 message source. Separate from `alerts:read` so callers can have the parsed view without the unredacted original. |
| `webhook:manage`    | Full CRUD on `/alerting/webhook-consumers`, restricted to consumers the caller owns. |
| `addresses:read`    | `GET /cameras/{id}/alert-addresses` — see which inbound email maps to which camera. |

Scopes are a closed set — adding a new one requires a code change (intentional;
scopes are a security boundary). An empty `scopes` array is valid and creates
a service account with zero permissions (useful for provisioning ahead of time
or temporarily disabling access without deleting the account).
"""


@router.get(
    "",
    response_model=list[ServiceAccountResponse],
    summary="List service accounts",
    description=(
        "Returns all service accounts, newest first. Tokens are not included — "
        "use `GET /service-accounts/{id}/tokens` to see them.\n\n"
        + _SCOPE_TABLE
    ),
)
def list_accounts(db: DBSession, _admin: AdminUser):
    return db.query(ServiceAccount).order_by(ServiceAccount.created_at.desc()).all()


@router.post(
    "",
    response_model=ServiceAccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service account",
    description=(
        "Creates a new service account. The returned row holds **no credentials** — "
        "you still need to issue a token via `POST /service-accounts/{id}/tokens`.\n\n"
        "Pick a unique `name` (e.g. `acme-monitoring-prod`) — recommend one "
        "service account per integrating system. Use the `description` to record "
        "the owner, purpose, and rotation policy.\n"
        + _SCOPE_TABLE +
        "\n## Errors\n\n"
        "- **409 Conflict** — A service account with this name already exists. "
        "Names are unique."
    ),
    responses={
        409: {"description": "A service account with this name already exists."},
    },
)
def create_account(
    body: ServiceAccountCreate,
    db: DBSession,
    admin: AdminUser,
):
    if db.query(ServiceAccount).filter(ServiceAccount.name == body.name).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Service account '{body.name}' already exists",
        )
    row = ServiceAccount(
        id=str(uuid.uuid4()),
        name=body.name,
        description=body.description,
        scopes=list(body.scopes or []),
        is_active=True,
        created_by=str(admin.user_id) if admin else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/{account_id}",
    response_model=ServiceAccountResponse,
    summary="Get one service account",
    description="Returns a single service account by UUID. Token list is on a separate endpoint.",
    responses={404: {"description": "Service account not found."}},
)
def get_account(account_id: str, db: DBSession, _admin: AdminUser):
    row = db.query(ServiceAccount).filter(ServiceAccount.id == account_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    return row


@router.patch(
    "/{account_id}",
    response_model=ServiceAccountResponse,
    summary="Update an account (description / scopes / is_active)",
    description=(
        "Partial update — only fields you include in the body get changed.\n\n"
        "**Setting `is_active=false`** is the global kill switch — every token "
        "for this account stops working immediately, regardless of individual "
        "revocation status. Set back to `true` to re-enable.\n\n"
        "**Updating `scopes`** replaces the whole list (not a merge). Pass `[]` "
        "to strip all permissions while keeping the account row.\n"
        + _SCOPE_TABLE
    ),
    responses={404: {"description": "Service account not found."}},
)
def update_account(
    account_id: str,
    body: ServiceAccountUpdate,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(ServiceAccount).filter(ServiceAccount.id == account_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    if body.description is not None:
        row.description = body.description
    if body.scopes is not None:
        row.scopes = list(body.scopes)
    if body.is_active is not None:
        row.is_active = body.is_active
    db.commit()
    db.refresh(row)
    return row


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a service account (hard delete; cascades to tokens)",
    description=(
        "Permanently deletes the account and all its tokens. Use this only when "
        "the integration is fully decommissioned. For a temporary disable, "
        "prefer `PATCH` with `is_active=false`."
    ),
    responses={404: {"description": "Service account not found."}},
)
def delete_account(account_id: str, db: DBSession, _admin: AdminUser):
    row = db.query(ServiceAccount).filter(ServiceAccount.id == account_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    db.delete(row)
    db.commit()
    return None


# ---------- tokens ---------- #

@router.get(
    "/{account_id}/tokens",
    response_model=list[ServiceAccountTokenResponse],
    summary="List tokens for an account (metadata only — no secrets)",
    description=(
        "Returns every token (active and revoked) for the given account. The "
        "raw secret is **never** returned here — that only happens once at "
        "token creation. Use this endpoint to track rotation: each row has "
        "`created_at`, `last_used_at`, `expires_at`, and `revoked_at`."
    ),
    responses={404: {"description": "Service account not found."}},
)
def list_tokens(account_id: str, db: DBSession, _admin: AdminUser):
    if db.query(ServiceAccount).filter(ServiceAccount.id == account_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    return (
        db.query(ServiceAccountToken)
        .filter(ServiceAccountToken.service_account_id == account_id)
        .order_by(ServiceAccountToken.created_at.desc())
        .all()
    )


@router.post(
    "/{account_id}/tokens",
    response_model=ServiceAccountTokenWithSecret,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a new bearer token (the secret is returned ONCE)",
    description=(
        "Mints a fresh `tlsa_<...>` token and returns the raw secret **in this "
        "response body only**. Subsequent reads (GET) will never include it — "
        "the database only stores the bcrypt hash.\n\n"
        "## What to do with the secret\n\n"
        "1. Copy it into a secret manager (1Password, Bitwarden, Vault) before "
        "leaving this page.\n"
        "2. Configure the downstream platform to send it as "
        "`Authorization: Bearer tlsa_<...>` on every request.\n"
        "3. If you lose it, **don't try to recover** — issue a new token and "
        "revoke this one.\n\n"
        "## Rotation pattern\n\n"
        "- Issue a new token first (this endpoint), confirm it works.\n"
        "- Swap it in on the downstream side.\n"
        "- Once you see the new token's `last_used_at` advancing in `GET /tokens`, "
        "revoke the old one with `DELETE /tokens/{id}`.\n\n"
        "Token `name` is just a label for humans (e.g. `prod-2026-05`). "
        "`expires_at` is optional — set it for an automatic time-based rotation, "
        "or leave null and manage rotation manually."
    ),
    responses={404: {"description": "Service account not found."}},
)
def issue_token(
    account_id: str,
    body: ServiceAccountTokenCreate,
    db: DBSession,
    _admin: AdminUser,
):
    if db.query(ServiceAccount).filter(ServiceAccount.id == account_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    raw, hashed = generate_service_account_token()
    row = ServiceAccountToken(
        id=str(uuid.uuid4()),
        service_account_id=account_id,
        token_hash=hashed,
        name=body.name,
        expires_at=body.expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ServiceAccountTokenWithSecret(
        id=row.id, name=row.name, expires_at=row.expires_at,
        last_used_at=row.last_used_at, revoked_at=row.revoked_at,
        created_at=row.created_at,
        secret=raw,
    )


@router.delete(
    "/{account_id}/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a token (soft delete — sets revoked_at)",
    description=(
        "Marks the token as revoked. The next auth attempt with that token is "
        "rejected with 401. The row stays in the table for audit purposes.\n\n"
        "Already-revoked tokens are silently accepted (idempotent)."
    ),
    responses={404: {"description": "Token not found under this account."}},
)
def revoke_token(
    account_id: str,
    token_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = (
        db.query(ServiceAccountToken)
        .filter(
            ServiceAccountToken.id == token_id,
            ServiceAccountToken.service_account_id == account_id,
        )
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return None
