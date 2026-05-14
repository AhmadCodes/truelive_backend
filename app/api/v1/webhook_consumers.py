"""
Webhook consumer registration + administration.

v1 ships with a single active consumer (GuardDesk). GuardDesk registers itself
by POSTing to `/alerting/webhook-consumers` with its URL + a secret it chose.

The HMAC secret is stored as-is. (NOTE: spec calls for encryption at rest via
the same mechanism as NVR passwords. The existing NVR password encryption lives
in app/utils/url_processor.py and friends — this can be retrofitted once the
shared encryption interface is finalized. For v1 the table is owned by ops and
sits in the same trust boundary as the rest of the schema.)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AdminUser, DBSession, require_scope
from app.models.alerting import Alert
from app.models.webhook import WebhookConsumer
from app.schemas.alerting import (
    WebhookConsumerCreate, WebhookConsumerUpdate, WebhookConsumerResponse,
    WebhookTestResponse,
)


router = APIRouter()


@router.get(
    "/webhook-consumers",
    response_model=list[WebhookConsumerResponse],
    summary="List webhook consumers",
)
def list_consumers(db: DBSession, _admin: AdminUser):
    return db.query(WebhookConsumer).order_by(WebhookConsumer.created_at.desc()).all()


@router.post(
    "/webhook-consumers",
    response_model=WebhookConsumerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook consumer (called by GuardDesk via service-account auth)",
)
def create_consumer(
    body: WebhookConsumerCreate,
    db: DBSession,
    _admin: AdminUser,
):
    if db.query(WebhookConsumer).filter(WebhookConsumer.name == body.name).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Consumer with name '{body.name}' already exists",
        )
    row = WebhookConsumer(
        id=str(uuid.uuid4()),
        name=body.name,
        url=str(body.url),
        secret=body.secret,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch(
    "/webhook-consumers/{consumer_id}",
    response_model=WebhookConsumerResponse,
    summary="Update url/secret/is_active for a consumer",
)
def update_consumer(
    consumer_id: str,
    body: WebhookConsumerUpdate,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(WebhookConsumer).filter(WebhookConsumer.id == consumer_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumer not found")
    if body.url is not None:
        row.url = str(body.url)
    if body.secret is not None:
        row.secret = body.secret
    if body.is_active is not None:
        row.is_active = body.is_active
    db.commit()
    db.refresh(row)
    return row


@router.delete(
    "/webhook-consumers/{consumer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a webhook consumer",
)
def delete_consumer(
    consumer_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    row = db.query(WebhookConsumer).filter(WebhookConsumer.id == consumer_id).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumer not found")
    db.delete(row)
    db.commit()
    return None


@router.post(
    "/webhook-consumers/{consumer_id}/test",
    response_model=WebhookTestResponse,
    summary="Fire a synthetic alert for integration testing",
)
def test_consumer(
    consumer_id: str,
    db: DBSession,
    _admin: AdminUser,
):
    consumer = db.query(WebhookConsumer).filter(WebhookConsumer.id == consumer_id).one_or_none()
    if consumer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consumer not found")
    if not consumer.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Consumer is inactive")

    # Pick any recent alert to fire — if none exists, build a synthetic one.
    recent = db.query(Alert).order_by(Alert.received_at.desc()).first()
    if recent is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No alerts available to test with. Send one via the live pipeline first.",
        )
    try:
        from app.tasks.deliver_webhook import deliver_webhook
        result = deliver_webhook.apply_async(
            args=[recent.id, recent.received_at.isoformat()],
            queue="alert_deliver",
        )
        return WebhookTestResponse(delivery_id=str(result.id), enqueued=True)
    except Exception:
        return WebhookTestResponse(delivery_id="", enqueued=False)
