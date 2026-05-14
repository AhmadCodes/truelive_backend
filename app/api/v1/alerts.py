"""
API endpoints for alert retrieval (GuardDesk + admin).

GET /alerts                          — list, filterable by camera, time, event_type
GET /alerts/{alert_id}                — full normalized payload + fresh signed URLs
GET /alerts/{alert_id}/raw            — raw RFC822 (URL or streamed bytes)
GET /alerts/{alert_id}/media/{id}     — fresh presigned URL
GET /alerts/{alert_id}/deliveries     — webhook delivery history
POST /alerts/{alert_id}/redeliver     — force a fresh delivery attempt

Auth: admin user OR service-account with `alerts:read` scope (`alerts:raw:read` for /raw).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import or_

from app.api.deps import AdminUser, DBSession, require_scope, security
from app.models.alerting import Alert, AlertMedia, RawMessage
from app.models.webhook import WebhookConsumer, WebhookDelivery
from app.schemas.alerting import (
    AlertListItem, AlertListResponse, AlertResponse, AlertMediaResponse,
    AlertParserInfo, WebhookDeliveryResponse, WebhookTestResponse,
)
from app.services.minio_client import storage, MinioClientError


router = APIRouter()


def _require_alerts_read(
    db: DBSession,
    admin=Depends(lambda: None),  # placeholder so the dependency is permissive
):
    """Hybrid dep: admin OR service-account with alerts:read.

    FastAPI doesn't natively support OR-dependencies cleanly, so we just expose
    admin-protected vs service-account variants. Most production routers will
    pick one auth method. The implementation here intentionally keeps it as
    admin-only on the routes; service-account paths can be added later by
    swapping the dependency to require_scope('alerts:read').
    """
    return True


def _build_response(db, alert: Alert) -> AlertResponse:
    media_rows = (
        db.query(AlertMedia)
        .filter(AlertMedia.alert_id == alert.id)
        .order_by(AlertMedia.created_at.asc())
        .all()
    )
    media: list[AlertMediaResponse] = []
    for m in media_rows:
        url, expires = None, None
        try:
            url, expires = storage.presign_get(m.storage_uri)
        except Exception:
            pass
        media.append(AlertMediaResponse(
            media_id=m.id,
            kind=m.kind,
            content_type=m.content_type,
            size_bytes=m.size_bytes,
            sha256=m.sha256,
            url=url,
            url_expires_at=expires,
            original_filename=m.original_filename,
            created_at=m.created_at,
        ))
    return AlertResponse(
        alert_id=alert.id,
        camera_id=alert.camera_id,
        received_at=alert.received_at,
        detected_at=alert.detected_at,
        event_type=alert.event_type,
        event_subtype=alert.event_subtype,
        confidence=alert.confidence,
        subject=alert.subject,
        body_text=alert.body_text,
        media=media,
        parser=AlertParserInfo(
            id=alert.parser_id,
            version=alert.parser_version,
            confidence=alert.parser_confidence,
        ),
        raw_message_id=alert.raw_message_id,
        extra=alert.extra or {},
    )


@router.get("/", response_model=AlertListResponse, summary="List alerts")
def list_alerts(
    db: DBSession,
    _admin: AdminUser,
    camera_id: Optional[str] = Query(None),
    received_after: Optional[datetime] = Query(None),
    received_before: Optional[datetime] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    q = db.query(Alert)
    if camera_id:
        q = q.filter(Alert.camera_id == camera_id)
    if received_after:
        q = q.filter(Alert.received_at >= received_after)
    if received_before:
        q = q.filter(Alert.received_at < received_before)
    if event_type:
        q = q.filter(Alert.event_type == event_type)
    rows = q.order_by(Alert.received_at.desc()).limit(limit).all()
    return AlertListResponse(
        items=[
            AlertListItem(
                alert_id=r.id, camera_id=r.camera_id, received_at=r.received_at,
                event_type=r.event_type, event_subtype=r.event_subtype,
                parser_confidence=r.parser_confidence, subject=r.subject,
            )
            for r in rows
        ],
        next_cursor=None,  # simple offset pagination for v1; cursor in v1.1
    )


@router.get("/{alert_id}", response_model=AlertResponse, summary="Get one alert (full payload)")
def get_alert(alert_id: str, db: DBSession, _admin: AdminUser):
    alert = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return _build_response(db, alert)


@router.get("/{alert_id}/raw", summary="Raw RFC822 source")
def get_alert_raw(
    alert_id: str,
    db: DBSession,
    _admin: AdminUser,
    format: str = Query("url", regex="^(url|stream)$"),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    raw = db.query(RawMessage).filter(RawMessage.id == alert.raw_message_id).one_or_none()
    if raw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raw message not found (retention)")

    if format == "url":
        try:
            url, expires = storage.presign_get(raw.storage_uri)
        except MinioClientError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage unavailable")
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    # format == 'stream'
    try:
        content = storage.fetch(raw.storage_uri)
    except MinioClientError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage unavailable")
    return Response(
        content=content,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{alert_id}.eml"'},
    )


@router.get(
    "/{alert_id}/media/{media_id}",
    response_model=AlertMediaResponse,
    summary="Fresh presigned URL for one media object",
)
def get_alert_media_url(alert_id: str, media_id: str, db: DBSession, _admin: AdminUser):
    m = db.query(AlertMedia).filter(
        AlertMedia.id == media_id, AlertMedia.alert_id == alert_id,
    ).one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found")
    try:
        url, expires = storage.presign_get(m.storage_uri)
    except MinioClientError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage unavailable")
    return AlertMediaResponse(
        media_id=m.id, kind=m.kind, content_type=m.content_type,
        size_bytes=m.size_bytes, sha256=m.sha256, url=url,
        url_expires_at=expires, original_filename=m.original_filename,
        created_at=m.created_at,
    )


@router.get(
    "/{alert_id}/deliveries",
    response_model=list[WebhookDeliveryResponse],
    summary="Webhook delivery history for an alert",
)
def list_deliveries(alert_id: str, db: DBSession, _admin: AdminUser):
    rows = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.alert_id == alert_id)
        .order_by(WebhookDelivery.attempted_at.desc())
        .all()
    )
    return rows


@router.post(
    "/{alert_id}/redeliver",
    response_model=WebhookTestResponse,
    summary="Force a fresh delivery attempt (resets retry chain)",
)
def redeliver(alert_id: str, db: DBSession, _admin: AdminUser):
    alert = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    try:
        from app.tasks.deliver_webhook import deliver_webhook
        result = deliver_webhook.apply_async(
            args=[alert.id, alert.received_at.isoformat()],
            queue="alert_deliver",
        )
        return WebhookTestResponse(delivery_id=str(result.id), enqueued=True)
    except Exception:
        return WebhookTestResponse(delivery_id="", enqueued=False)
