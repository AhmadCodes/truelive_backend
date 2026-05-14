"""
API endpoints for per-camera alert addresses.

An **alert address** is the inbound email address that the upstream alert
source (e.g. Calipsa) uses to deliver alerts for a specific camera. The
format is `cam-<token>@alerts.usvg.ai`. When mail arrives:

  upstream sender → Postfix MX → LMTP → truelive-smtp-ingest → MinIO + raw_messages
                                              ↓
                                       Celery parse → alerts + alert_media rows
                                              ↓
                                       Celery deliver → downstream webhook

These endpoints let you provision, list, rotate, quarantine, and revoke those
addresses. The auto-provision hook on camera create already gives every new
camera one active address — you typically only use these endpoints for
rotation or runaway-camera mitigation.

Path layout follows the existing project pattern (see `screens.py`, `pcs.py`):

- `/cameras/{camera_id}/alert-addresses` — camera-scoped list / create
- `/alert-addresses/{id}` — direct address mutations

All endpoints require an admin JWT.
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
    summary="List all alert addresses for a camera",
    description=(
        "Returns every alert address ever provisioned for this camera, newest "
        "first. Includes revoked and quarantined rows so you can see the "
        "rotation history.\n\n"
        "**Spotting the current address:** look for the row with `is_active=true` "
        "AND `is_quarantined=false`. There should be at most one such row per "
        "camera at any time (enforced by the provisioning path).\n\n"
        "**Path parameter `camera_id`** — the ID of the camera as it appears "
        "in `/cameras/{id}`. The endpoint 404s if the camera doesn't exist."
    ),
    responses={404: {"description": "Camera not found."}},
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
    summary="Provision an alert address (idempotent)",
    description=(
        "Returns the camera's existing active address if one already exists; "
        "otherwise generates a new one with a fresh opaque token in the form "
        "`cam-<16-char-base64url>`.\n\n"
        "**Idempotent by design** — repeated calls do not create duplicates. "
        "Camera creation already triggers an auto-provision, so you typically "
        "only POST here to recover from a missing row (e.g. after a manual "
        "DB cleanup) or to confirm the camera has an address before pasting it "
        "into the upstream system.\n\n"
        "**To force a brand-new token without throwing away history**, use "
        "`POST /alert-addresses/{id}/rotate` against the current active row.\n\n"
        "No request body is required — the server generates the local part."
    ),
    responses={
        201: {"description": "Either a new address was created or the existing active one was returned."},
        404: {"description": "Camera not found."},
    },
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
    description=(
        "Marks the address inactive (`is_active=false`) and sets `revoked_at`. "
        "Future mail to this address is rejected at LMTP RCPT TO with "
        "`550 5.1.1 No such recipient`.\n\n"
        "**This is permanent for that specific address row.** The token will "
        "never be re-validated, even if the camera gets a new active address "
        "later. To replace it with a fresh one in a single step, prefer "
        "`POST /alert-addresses/{id}/rotate`.\n\n"
        "**For a reversible block**, use `POST /alert-addresses/{id}/quarantine` "
        "instead (see below)."
    ),
    responses={
        204: {"description": "Address revoked (or was already revoked — idempotent)."},
        404: {"description": "Alert address not found."},
    },
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
    summary="Revoke + provision atomically (one-step rotation)",
    description=(
        "Revokes the given address and immediately provisions a new one for "
        "the same camera. Use this when:\n\n"
        "- You suspect the address leaked.\n"
        "- A policy rotation interval (e.g. yearly) is up.\n"
        "- A previous quarantine was lifted but you want a clean handle.\n\n"
        "The response carries both rows: `revoked_address` is the old one "
        "(now `is_active=false`, `revoked_at` set) and `new_address` is the "
        "freshly-issued replacement. **Paste the new address into the upstream "
        "system** — mail to the old token is dead immediately.\n\n"
        "Calling rotate against an already-revoked row is still valid: it "
        "skips the (no-op) revoke and just provisions a new address."
    ),
    responses={404: {"description": "Alert address not found."}},
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
    summary="Hard-block an address (reversible)",
    description=(
        "Sets `is_quarantined=true`. The LMTP server rejects all subsequent "
        "mail to this address with `550 5.7.1 Recipient quarantined`. The "
        "address row remains and `is_active` stays `true` — this is **not** a "
        "revoke.\n\n"
        "Use this when a single camera floods the system (runaway PIR, lightning "
        "storm motion bursts) and you want to stop ingestion without losing "
        "the address mapping. Lift the block via "
        "`POST /alert-addresses/{id}/unquarantine`.\n\n"
        "Distinct from `DELETE` (irreversible revoke) and the automatic "
        "per-address rate limit (returns 451 to defer rather than 550 to reject)."
    ),
    responses={404: {"description": "Alert address not found."}},
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
    description=(
        "Sets `is_quarantined=false`. The LMTP server resumes accepting mail "
        "to this address (subject to the per-address rate limit). The address "
        "and its tokens are unchanged.\n\n"
        "Idempotent — calling on an address that isn't quarantined returns "
        "the unchanged row."
    ),
    responses={404: {"description": "Alert address not found."}},
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
