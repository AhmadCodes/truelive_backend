"""
API endpoints for per-camera alert addresses.

Path pattern follows the existing project style (see screens.py, pcs.py):
- /cameras/{camera_id}/alert-addresses for camera-scoped list/create
- /alert-addresses/{id} for direct address operations

Auth: admin user for mutations; service-account with `addresses:read` scope OR
admin for reads.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AdminUser, DBSession, require_scope
from app.core.config import settings
from app.models.alerting import AlertAddress
from app.models.camera import Camera
from app.schemas.alerting import AlertAddressResponse, AlertAddressRotateResponse
from app.utils.secrets_gen import generate_alert_local_part


router = APIRouter()


def _active_address(db, camera_id: str) -> AlertAddress | None:
    return (
        db.query(AlertAddress)
        .filter(
            AlertAddress.camera_id == camera_id,
            AlertAddress.is_active == True,  # noqa: E712
        )
        .order_by(AlertAddress.created_at.desc())
        .first()
    )


def _provision_address(db, camera_id: str) -> AlertAddress:
    """Insert a fresh active address for a camera. Tries up to 5 times on collision."""
    for _ in range(5):
        local = generate_alert_local_part()
        addr = AlertAddress(
            id=str(uuid.uuid4()),
            camera_id=camera_id,
            local_part=local,
            domain=settings.ALERT_DOMAIN,
        )
        db.add(addr)
        try:
            db.commit()
            db.refresh(addr)
            return addr
        except Exception:
            db.rollback()
            continue
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to provision alert address after 5 attempts",
    )


@router.get(
    "/cameras/{camera_id}/alert-addresses",
    response_model=list[AlertAddressResponse],
    summary="List a camera's alert addresses",
)
def list_camera_addresses(
    camera_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    rows = (
        db.query(AlertAddress)
        .filter(AlertAddress.camera_id == camera_id)
        .order_by(AlertAddress.created_at.desc())
        .all()
    )
    return rows


@router.post(
    "/cameras/{camera_id}/alert-addresses",
    response_model=AlertAddressResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a new alert address (idempotent — returns existing active if present)",
)
def create_camera_address(
    camera_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")

    existing = _active_address(db, camera_id)
    if existing is not None:
        return existing

    return _provision_address(db, camera_id)


@router.delete(
    "/alert-addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an alert address (soft delete)",
)
def revoke_address(
    address_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(AlertAddress).filter(AlertAddress.id == address_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert address not found")
    if row.is_active:
        row.is_active = False
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return None


@router.post(
    "/alert-addresses/{address_id}/rotate",
    response_model=AlertAddressRotateResponse,
    summary="Revoke and provision a fresh address in one operation",
)
def rotate_address(
    address_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(AlertAddress).filter(AlertAddress.id == address_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert address not found")
    if row.is_active:
        row.is_active = False
        row.revoked_at = datetime.now(timezone.utc)
        db.commit()
    new_addr = _provision_address(db, row.camera_id)
    return AlertAddressRotateResponse(revoked_address=row, new_address=new_addr)


@router.post(
    "/alert-addresses/{address_id}/quarantine",
    response_model=AlertAddressResponse,
    summary="Hard-block an address (deliveries rejected at LMTP RCPT TO with 550 5.7.1)",
)
def quarantine_address(
    address_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(AlertAddress).filter(AlertAddress.id == address_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert address not found")
    if not row.is_quarantined:
        row.is_quarantined = True
        db.commit()
        db.refresh(row)
    return row


@router.post(
    "/alert-addresses/{address_id}/unquarantine",
    response_model=AlertAddressResponse,
    summary="Clear the quarantine flag",
)
def unquarantine_address(
    address_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(AlertAddress).filter(AlertAddress.id == address_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert address not found")
    if row.is_quarantined:
        row.is_quarantined = False
        db.commit()
        db.refresh(row)
    return row
