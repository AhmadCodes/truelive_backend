"""
Celery task: deliver one alert to all active webhook_consumers (v1 = one).

Runs on the `alert_deliver` queue with prefetch=1 so slow consumer responses
don't starve other deliveries.

Retry chain: 1m, 5m, 30m, 2h, 12h. Six attempts total (~15h). On attempt 6
failure we mark `status=giving_up` and `raw_messages.status=forward_failed`.

The downstream consumer MUST dedupe on `alert_id` (spec §10 idempotency contract)
— if our POST timed out but actually succeeded on their side, retries will re-
deliver and they need to ack 2xx without re-processing.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import requests
from celery import shared_task
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import SessionLocal
from app.models.alerting import Alert, AlertMedia, RawMessage
from app.models.webhook import WebhookConsumer, WebhookDelivery
from app.services.minio_client import storage
from app.utils.hmac_sign import sign, now_timestamp_header

logger = logging.getLogger(__name__)


def _build_payload(db: Session, alert: Alert) -> dict:
    media_rows = db.query(AlertMedia).filter(AlertMedia.alert_id == alert.id).all()
    media = []
    for m in media_rows:
        try:
            url, expires = storage.presign_get(m.storage_uri)
        except Exception:
            logger.exception("presign failed; media URL omitted", extra={"media_id": m.id})
            url, expires = None, None
        media.append({
            "media_id": m.id,
            "kind": m.kind,
            "content_type": m.content_type,
            "size_bytes": m.size_bytes,
            "sha256": m.sha256,
            "url": url,
            "url_expires_at": expires.isoformat() if expires else None,
            "original_filename": m.original_filename,
        })

    return {
        "schema_version": "1.0",
        "alert_id": alert.id,
        "camera_id": alert.camera_id,
        "received_at": alert.received_at.astimezone(timezone.utc).isoformat(),
        "detected_at": alert.detected_at.astimezone(timezone.utc).isoformat() if alert.detected_at else None,
        "event_type": alert.event_type,
        "event_subtype": alert.event_subtype,
        "confidence": alert.confidence,
        "subject": alert.subject,
        "body_text": alert.body_text,
        "media": media,
        "parser": {
            "id": alert.parser_id,
            "version": alert.parser_version,
            "confidence": alert.parser_confidence,
        },
        "raw_message_id": alert.raw_message_id,
        "extra": alert.extra or {},
    }


def _retry_delay(attempt: int) -> int | None:
    """Return seconds to wait before attempt `attempt+1`, or None if exhausted."""
    schedule = settings.WEBHOOK_RETRY_SCHEDULE_SECONDS
    if attempt >= len(schedule):
        return None
    return schedule[attempt]


def _post_once(
    url: str, body: bytes, signature: str, alert_id: str, delivery_id: str,
) -> tuple[int | None, str | None, str | None]:
    """Return (http_status, response_excerpt, error_str)."""
    headers = {
        "Content-Type": "application/json",
        "X-TrueLive-Signature": signature,
        "X-TrueLive-Alert-Id": alert_id,
        "X-TrueLive-Delivery-Id": delivery_id,
        "X-TrueLive-Timestamp": now_timestamp_header(),
        "User-Agent": "truelive-webhook/1.0",
    }
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=settings.WEBHOOK_TIMEOUT_SECONDS)
        excerpt = resp.text[:1024] if resp.content else None
        return resp.status_code, excerpt, None
    except requests.RequestException as exc:
        return None, None, str(exc)


@shared_task(
    bind=True, name="app.tasks.deliver_webhook",
    max_retries=10,  # Effective retry chain is managed by countdown, not retries.
)
def deliver_webhook(self, alert_id: str, received_at_iso: str):
    """
    Look up alert, build payload, POST to each active consumer. Each consumer is
    its own delivery row; retries are per-(alert, consumer) pair.
    """
    received_at = datetime.fromisoformat(received_at_iso)
    with SessionLocal() as db:
        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id, Alert.received_at == received_at)
            .one_or_none()
        )
        if alert is None:
            logger.warning("alert not found", extra={"alert_id": alert_id})
            return

        consumers = (
            db.query(WebhookConsumer)
            .filter(WebhookConsumer.is_active == True)  # noqa: E712
            .all()
        )
        if not consumers:
            logger.info("no active webhook consumers; alert held", extra={"alert_id": alert_id})
            return

        payload = _build_payload(db, alert)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        for consumer in consumers:
            _deliver_to_consumer(
                db, alert=alert, consumer=consumer, body=body, attempt=1,
            )


def _deliver_to_consumer(
    db: Session, *, alert: Alert, consumer: WebhookConsumer, body: bytes, attempt: int,
) -> None:
    delivery_id = str(uuid.uuid4())
    attempted_at = datetime.now(timezone.utc)
    signature = sign(body, consumer.secret)

    # Pre-insert the pending row so we have a stable id for the headers.
    delivery = WebhookDelivery(
        id=delivery_id,
        alert_id=alert.id,
        consumer_id=consumer.id,
        attempt=attempt,
        status="pending",
        attempted_at=attempted_at,
    )
    db.add(delivery)
    db.commit()

    http_status, excerpt, error = _post_once(
        consumer.url, body, signature, alert.id, delivery_id,
    )

    if http_status is not None and 200 <= http_status < 300:
        delivery.status = "success"
        delivery.http_status = http_status
        delivery.response_excerpt = excerpt
        db.commit()
        _mark_raw_forwarded(db, alert.raw_message_id)
        logger.info(
            "webhook delivered",
            extra={"alert_id": alert.id, "consumer_id": consumer.id, "attempt": attempt},
        )
        return

    # Failure path: schedule retry or give up.
    delivery.http_status = http_status
    delivery.response_excerpt = excerpt
    delivery.error = error
    delay = _retry_delay(attempt)
    if delay is None:
        delivery.status = "giving_up"
        db.commit()
        _mark_raw_forward_failed(db, alert.raw_message_id)
        logger.error(
            "webhook giving up",
            extra={"alert_id": alert.id, "consumer_id": consumer.id, "attempts": attempt},
        )
        return

    delivery.status = "failed"
    delivery.next_retry_at = datetime.now(timezone.utc).replace(microsecond=0)  # Best-effort
    db.commit()

    # Re-enqueue via Celery's apply_async with a countdown.
    deliver_webhook_retry.apply_async(
        args=[alert.id, alert.received_at.isoformat(), consumer.id, attempt + 1],
        countdown=delay,
        queue="alert_deliver",
    )
    logger.warning(
        "webhook failed; scheduled retry",
        extra={
            "alert_id": alert.id, "consumer_id": consumer.id,
            "attempt": attempt, "next_in_s": delay, "http_status": http_status,
            "error": error,
        },
    )


@shared_task(bind=True, name="app.tasks.deliver_webhook_retry", max_retries=10)
def deliver_webhook_retry(
    self, alert_id: str, received_at_iso: str, consumer_id: str, attempt: int,
):
    """Per-consumer retry entrypoint. Keeps the retry chain isolated per consumer."""
    received_at = datetime.fromisoformat(received_at_iso)
    with SessionLocal() as db:
        alert = (
            db.query(Alert)
            .filter(Alert.id == alert_id, Alert.received_at == received_at)
            .one_or_none()
        )
        consumer = db.query(WebhookConsumer).filter(WebhookConsumer.id == consumer_id).one_or_none()
        if alert is None or consumer is None or not consumer.is_active:
            return
        payload = _build_payload(db, alert)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        _deliver_to_consumer(
            db, alert=alert, consumer=consumer, body=body, attempt=attempt,
        )


def _mark_raw_forwarded(db: Session, raw_message_id: str) -> None:
    # The raw_messages.status reflects best-effort across all consumers.
    raw = db.query(RawMessage).filter(RawMessage.id == raw_message_id).one_or_none()
    if raw is None:
        return
    if raw.status not in ("forwarded", "forward_failed"):
        raw.status = "forwarded"
        db.commit()


def _mark_raw_forward_failed(db: Session, raw_message_id: str) -> None:
    raw = db.query(RawMessage).filter(RawMessage.id == raw_message_id).one_or_none()
    if raw is None or raw.status == "forwarded":
        return
    raw.status = "forward_failed"
    db.commit()
