"""
Webhook consumer registration + administration.

A **webhook consumer** is a downstream platform that should receive an
HMAC-signed JSON POST every time TrueLive produces a new alert. v1 supports
exactly one active consumer at a time; the schema supports N for forward
compatibility.

These endpoints are typically called by an admin during initial integration
setup, or by the downstream platform itself if it holds a service-account
token with the `webhook:manage` scope.

For the full delivery contract — payload schema, signing algorithm,
idempotency rules, retry behavior — see
`experiments/alerting_feature/webhook_contract.md`. A quick reference of the
runtime contract is included on each endpoint description below.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DBSession, admin_or_scope
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
    summary="List registered webhook consumers",
    description=(
        "Returns all registered consumers, newest first. The `secret` field is "
        "**never** included in any response — it's only stored to sign outbound "
        "POSTs. To rotate a lost secret, `PATCH` with a new value (see below)."
    ),
)
def list_consumers(db: DBSession, _auth = Depends(admin_or_scope("webhook:manage"))):
    return db.query(WebhookConsumer).order_by(WebhookConsumer.created_at.desc()).all()


@router.post(
    "/webhook-consumers",
    response_model=WebhookConsumerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook consumer",
    description=(
        "Registers a new downstream consumer. After this call, every parsed "
        "alert is POSTed to `url` while `is_active=true`.\n\n"
        "## Body fields\n\n"
        "- **`name`** — unique label (e.g. `acme-monitoring-prod`).\n"
        "- **`url`** — HTTPS endpoint that will accept `POST application/json`. "
        "The consumer **must** ack 2xx within 5 seconds.\n"
        "- **`secret`** — shared key (≥16 chars) used to HMAC-SHA256 sign every "
        "outbound delivery. TrueLive stores it server-side; the consumer uses "
        "the same value to verify the `X-TrueLive-Signature` header. Choose a "
        "cryptographically random 32+ byte string.\n\n"
        "## Delivery contract (summary)\n\n"
        "Each POST carries these headers:\n"
        "- `Content-Type: application/json`\n"
        "- `X-TrueLive-Signature: sha256=<hex>` — `HMAC-SHA256(body, secret)`\n"
        "- `X-TrueLive-Timestamp: <unix-seconds>` — reject if > 5 min from now\n"
        "- `X-TrueLive-Alert-Id: <uuid>` — **dedupe on this** (idempotency)\n"
        "- `X-TrueLive-Delivery-Id: <uuid>` — unique per attempt\n\n"
        "Retry chain on non-2xx or timeout: 1m → 5m → 30m → 2h → 12h, then "
        "give up after 6 attempts (~15h window). After give-up the alert is "
        "still retrievable via `GET /alerts/{id}`; you can also force a fresh "
        "attempt via `POST /alerts/{id}/redeliver`.\n\n"
        "## Errors\n\n"
        "- **409 Conflict** — A consumer with this name already exists.\n\n"
        "See `webhook_contract.md` for the full integration guide."
    ),
    responses={
        409: {"description": "A consumer with this name already exists."},
    },
)
def create_consumer(
    body: WebhookConsumerCreate,
    db: DBSession,
    _auth = Depends(admin_or_scope("webhook:manage")),
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
    summary="Update url / secret / is_active",
    description=(
        "Partial update — only fields included in the body are changed.\n\n"
        "- **`url`** — change the delivery target. Takes effect on the next "
        "outbound POST; in-flight retries already queued use the old URL.\n"
        "- **`secret`** — rotate the HMAC shared key. Future deliveries are "
        "signed with the new secret; in-flight retries already queued still "
        "use the previous one (so coordinate the rotation with the consumer "
        "to avoid a brief signature-mismatch window).\n"
        "- **`is_active`** — flip to `false` to suspend deliveries without "
        "deleting the row. Useful during downstream maintenance windows."
    ),
    responses={404: {"description": "Consumer not found."}},
)
def update_consumer(
    consumer_id: str,
    body: WebhookConsumerUpdate,
    db: DBSession,
    _auth = Depends(admin_or_scope("webhook:manage")),
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
    summary="Delete a webhook consumer (hard delete)",
    description=(
        "Permanently deletes the consumer row. Future alerts will not be "
        "delivered to it; existing delivery rows (in `/alerts/{id}/deliveries`) "
        "remain for audit purposes.\n\n"
        "For a temporary pause, prefer `PATCH` with `is_active=false`."
    ),
    responses={404: {"description": "Consumer not found."}},
)
def delete_consumer(
    consumer_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("webhook:manage")),
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
    description=(
        "Picks the most recent real alert in the DB and re-fires it to this "
        "consumer through the **live delivery pipeline** — same signing, same "
        "headers, same retry chain. Useful to verify the consumer's signature "
        "verification and idempotency code paths without waiting for a real "
        "alert from the SMTP pipeline.\n\n"
        "Returns the Celery task ID of the enqueued delivery. The POST happens "
        "asynchronously; check `GET /alerts/{id}/deliveries` (where `{id}` is "
        "the alert chosen) to see the result.\n\n"
        "## Errors\n\n"
        "- **400 Bad Request** — the consumer is inactive, or there are no "
        "alerts in the DB yet (you need at least one real alert from the live "
        "pipeline to use as the test payload)."
    ),
    responses={
        400: {"description": "Consumer inactive, or no alerts in DB to test with."},
        404: {"description": "Consumer not found."},
    },
)
def test_consumer(
    consumer_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("webhook:manage")),
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
