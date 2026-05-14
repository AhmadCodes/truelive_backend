"""
Admin endpoints for service-account auth.

POST /service-accounts            — create a service account
GET  /service-accounts            — list
GET  /service-accounts/{id}        — get one
PATCH /service-accounts/{id}       — update description/scopes/is_active
DELETE /service-accounts/{id}     — delete (cascades to tokens)

POST /service-accounts/{id}/tokens         — issue a new token (raw shown once)
GET  /service-accounts/{id}/tokens         — list tokens (no secrets)
DELETE /service-accounts/{id}/tokens/{tid} — revoke a token

All require admin role.
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


@router.get("", response_model=list[ServiceAccountResponse], summary="List service accounts")
def list_accounts(db: DBSession, _admin: AdminUser):
    return db.query(ServiceAccount).order_by(ServiceAccount.created_at.desc()).all()


@router.post(
    "", response_model=ServiceAccountResponse,
    status_code=status.HTTP_201_CREATED, summary="Create a service account",
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
    "/{account_id}", response_model=ServiceAccountResponse,
    summary="Get one service account",
)
def get_account(account_id: str, db: DBSession, _admin: AdminUser):
    row = db.query(ServiceAccount).filter(ServiceAccount.id == account_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service account not found")
    return row


@router.patch(
    "/{account_id}", response_model=ServiceAccountResponse,
    summary="Update description/scopes/is_active",
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
    summary="Delete a service account (cascades to tokens)",
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
    summary="List tokens for an account (no secrets — those are shown once on creation)",
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
    summary="Issue a new bearer token (the secret is returned ONCE — store it securely)",
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
