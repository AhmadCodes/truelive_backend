"""
Celery task: parse an inbound raw email into a normalized alert + media rows,
then enqueue webhook delivery.

This task runs on the `alert_parse` queue. Failures are logged and the raw_message
status is moved to `parse_failed` — but we still emit an alerts row with
`parser_confidence="unparsed"` and forward it, so the downstream consumer can
fall back to human review (spec §8 "even unparsed alerts get forwarded").
"""

from __future__ import annotations

import email
import hashlib
import logging
import mimetypes
import uuid
from datetime import datetime, timezone
from typing import Iterable

from celery import shared_task

from app.database import SessionLocal
from app.models.alerting import Alert, AlertMedia, RawMessage
from app.services.alert_parsers import dispatch
from app.services.alert_parsers.helpers import extract_attachments
from app.services.minio_client import storage, MinioClientError

logger = logging.getLogger(__name__)


def _extension_for(filename: str, content_type: str) -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    ext = mimetypes.guess_extension(content_type or "") or ""
    return ext.lstrip(".") or "bin"


def _classify_media(content_type: str, filename: str) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "snapshot"
    if ct.startswith("video/"):
        return "video_clip"
    return "attachment_other"


def _store_attachments(
    alert_id: str, attachments: Iterable, *, created_at: datetime,
) -> list[dict]:
    """Upload each attachment to MinIO, return media-row dicts ready for insert."""
    rows: list[dict] = []
    for filename, ctype, payload in attachments:
        media_id = str(uuid.uuid4())
        digest = hashlib.sha256(payload).hexdigest()
        ext = _extension_for(filename, ctype)
        try:
            uri = storage.put_alert_media(
                alert_id, media_id, payload,
                content_type=ctype, extension=ext, created_at=created_at,
            )
        except MinioClientError:
            logger.exception(
                "alert media upload failed; skipping",
                extra={"alert_id": alert_id, "filename": filename},
            )
            continue
        rows.append({
            "id": media_id,
            "alert_id": alert_id,
            "kind": _classify_media(ctype, filename),
            "content_type": ctype,
            "size_bytes": len(payload),
            "storage_uri": uri,
            "original_filename": filename or None,
            "sha256": digest,
            "created_at": created_at,
        })
    return rows


def _enqueue_deliver(alert_id: str, received_at: datetime) -> None:
    """Hand off to the webhook delivery worker. Lazy import to avoid cycles."""
    try:
        from app.tasks.deliver_webhook import deliver_webhook
        deliver_webhook.apply_async(
            args=[alert_id, received_at.isoformat()],
            queue="alert_deliver",
        )
    except Exception:
        logger.exception("failed to enqueue deliver_webhook", extra={"alert_id": alert_id})


@shared_task(
    bind=True, name="app.tasks.process_inbound_alert",
    autoretry_for=(MinioClientError,),
    retry_backoff=True, retry_backoff_max=60, max_retries=3,
)
def process_inbound_alert(self, raw_message_id: str):
    """
    Parse one raw message. Idempotent: if an alert already exists for this raw,
    we just re-enqueue the webhook delivery (handles a previous mid-task crash).
    """
    with SessionLocal() as db:
        # raw_messages has composite PK (id, received_at). Query by id only — uuid4
        # collisions are negligible.
        raw = db.query(RawMessage).filter(RawMessage.id == raw_message_id).one_or_none()
        if raw is None:
            logger.warning("raw_message not found", extra={"raw_message_id": raw_message_id})
            return

        # Idempotency: if an Alert already exists for this raw, just re-enqueue.
        existing = db.query(Alert).filter(Alert.raw_message_id == raw_message_id).one_or_none()
        if existing is not None:
            _enqueue_deliver(existing.id, existing.received_at)
            return

        try:
            content = storage.fetch(raw.storage_uri)
        except MinioClientError:
            logger.exception(
                "MinIO fetch failed; will retry",
                extra={"raw_message_id": raw_message_id, "uri": raw.storage_uri},
            )
            raise

        try:
            msg = email.message_from_bytes(content)
            result = dispatch(msg)
        except Exception:
            logger.exception(
                "parse failed; recording as unparsed",
                extra={"raw_message_id": raw_message_id},
            )
            # Synthesize an unparsed result rather than dropping the message.
            from app.services.alert_parsers import unparsed_fallback
            try:
                msg = email.message_from_bytes(content)
            except Exception:
                # Even the email header parse failed — that's exceptional.
                raw.status = "parse_failed"
                db.commit()
                return
            result = unparsed_fallback(msg)

        alert_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        attachments_rows = _store_attachments(
            alert_id, extract_attachments(msg), created_at=now,
        )

        # Insert alerts row + alert_media rows in one transaction. raw_messages
        # FK to camera is preserved via raw.camera_id (kept on the alert too).
        if raw.camera_id is None:
            logger.error(
                "raw_message has no camera_id; cannot persist alert",
                extra={"raw_message_id": raw_message_id},
            )
            raw.status = "parse_failed"
            db.commit()
            return

        alert = Alert(
            id=alert_id,
            raw_message_id=raw.id,
            camera_id=raw.camera_id,
            received_at=raw.received_at,
            detected_at=result.detected_at,
            event_type=result.event_type or "unknown",
            event_subtype=result.event_subtype,
            confidence=result.confidence,
            parser_id=result.parser_id,
            parser_version=result.parser_version,
            parser_confidence=result.parser_confidence,
            subject=result.subject,
            body_text=result.body_text,
            extra=result.extra or {},
        )
        db.add(alert)

        for row in attachments_rows:
            db.add(AlertMedia(**row))

        raw.status = "parsed" if result.parser_confidence != "unparsed" else "parse_failed"
        db.commit()

        _enqueue_deliver(alert_id, raw.received_at)
        logger.info(
            "alert parsed",
            extra={
                "raw_message_id": raw_message_id,
                "alert_id": alert_id,
                "event_type": result.event_type,
                "parser_id": result.parser_id,
                "parser_confidence": result.parser_confidence,
                "attachments": len(attachments_rows),
            },
        )
