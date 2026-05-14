"""
API endpoints for alert retrieval.

These endpoints give human admins and downstream platforms read access to the
normalized alerts produced by the SMTP ingest pipeline, plus tools to inspect
and re-fire webhook deliveries.

Auth model:
- All endpoints accept an admin JWT.
- The same endpoints are reachable by a service-account bearer token holding
  the `alerts:read` scope (or `alerts:raw:read` for `GET /alerts/{id}/raw`).

Data shapes are documented on each endpoint and on the Pydantic models in
`app/schemas/alerting.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import or_

from app.api.deps import DBSession, admin_or_scope
from app.models.alerting import Alert, AlertMedia, RawMessage
from app.models.webhook import WebhookConsumer, WebhookDelivery
from app.schemas.alerting import (
    AlertListItem, AlertListResponse, AlertResponse, AlertMediaResponse,
    AlertParserInfo, WebhookDeliveryResponse, WebhookTestResponse,
    EventType,
)
from app.services.minio_client import storage, MinioClientError


router = APIRouter()


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


@router.get(
    "/",
    response_model=AlertListResponse,
    summary="List alerts (newest first, filterable)",
    description=(
        "Returns a compact list of alerts ordered by `received_at` descending. "
        "Use the filter parameters to narrow by camera, time range, or event type.\n\n"
        "## Filters\n\n"
        "All filters are AND-combined.\n\n"
        "- **`camera_id`** — exact match against `alerts.camera_id`.\n"
        "- **`received_after`** — inclusive lower bound on `received_at`. "
        "Accepts ISO-8601 with timezone, e.g. `2026-05-14T00:00:00Z`.\n"
        "- **`received_before`** — **exclusive** upper bound. Together with "
        "`received_after` this defines a half-open `[after, before)` window, "
        "which is the standard for time-range pagination.\n"
        "- **`event_type`** — exact match. One of: `motion`, `person`, `vehicle`, "
        "`intrusion`, `unknown`. Use `unknown` to find alerts where the parser "
        "couldn't classify the event.\n"
        "- **`limit`** — max items returned (1-500, default 50).\n\n"
        "## Pagination\n\n"
        "v1 returns `next_cursor: null`. To page backwards in time, take the "
        "oldest `received_at` from the current page and pass it as "
        "`received_before` on the next call.\n\n"
        "## Retention\n\n"
        "Alerts are retained 90 days. Older alerts return zero rows. The "
        "underlying tables are partitioned monthly — filtering by time is "
        "very efficient at any scale."
    ),
)
def list_alerts(
    db: DBSession,
    _auth = Depends(admin_or_scope("alerts:read")),
    camera_id: Optional[str] = Query(
        None,
        description="Filter to one specific camera's alerts.",
        examples=["9D7Q"],
    ),
    received_after: Optional[datetime] = Query(
        None,
        description="Inclusive lower bound on `received_at` (ISO-8601 with timezone).",
        examples=["2026-05-14T00:00:00Z"],
    ),
    received_before: Optional[datetime] = Query(
        None,
        description="Exclusive upper bound on `received_at` (ISO-8601 with timezone).",
        examples=["2026-05-15T00:00:00Z"],
    ),
    event_type: Optional[EventType] = Query(
        None,
        description=(
            "Filter to one event type. Valid values: `motion`, `person`, `vehicle`, "
            "`intrusion`, `unknown`."
        ),
        examples=["motion"],
    ),
    limit: int = Query(
        50, ge=1, le=500,
        description="Max items to return (1-500, default 50).",
    ),
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


@router.get(
    "/{alert_id}",
    response_model=AlertResponse,
    summary="Get one alert with full payload + fresh signed media URLs",
    description=(
        "Returns the full normalized alert — same shape as outbound webhook "
        "bodies. Each media object gets a fresh 7-day presigned URL minted at "
        "request time (so even if the webhook delivery's URLs have expired, "
        "this endpoint always gives you working ones — until the media itself "
        "ages out at 30 days).\n\n"
        "**Retention tombstone:** alerts live 90 days but media lives only 30. "
        "An alert older than 30 days returns `media: []` even if it originally "
        "had attachments. The parsed text (`subject`, `body_text`, `event_type`, "
        "etc.) is still available."
    ),
    responses={404: {"description": "Alert not found (typo or past 90-day retention)."}},
)
def get_alert(alert_id: str, db: DBSession, _auth = Depends(admin_or_scope("alerts:read"))):
    alert = db.query(Alert).filter(Alert.id == alert_id).one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return _build_response(db, alert)


@router.get(
    "/{alert_id}/raw",
    summary="Download the raw RFC822 source",
    description=(
        "Returns the original `.eml` message exactly as the upstream sender "
        "delivered it (post-MIME, pre-parse). Useful for forensics or to "
        "re-run a parser locally.\n\n"
        "## `format` options\n\n"
        "- **`url` (default)** — HTTP 307 redirect to a 7-day presigned MinIO URL. "
        "Best for browsers, `curl -L`, or anywhere that follows redirects. The "
        "response body is empty; the URL is in the `Location` header.\n"
        "- **`stream`** — server proxies the raw bytes back as "
        "`Content-Type: message/rfc822` with a `Content-Disposition: attachment` "
        "header. Best for clients that can't follow redirects, are behind a "
        "strict egress firewall that blocks `s3.usvg.ai`, or want to pipe the "
        "bytes directly without a second hop.\n\n"
        "Raw mail is retained 90 days (same as alerts)."
    ),
    responses={
        307: {"description": "Redirect to a presigned MinIO URL (when `format=url`)."},
        200: {"description": "Raw RFC822 bytes (when `format=stream`).", "content": {"message/rfc822": {}}},
        404: {"description": "Alert not found, or its raw message has aged out (past 90-day retention)."},
        503: {"description": "Storage backend unavailable — retry shortly."},
    },
)
def get_alert_raw(
    alert_id: str,
    db: DBSession,
    _auth = Depends(admin_or_scope("alerts:raw:read")),
    format: str = Query(
        "url", regex="^(url|stream)$",
        description=(
            "Delivery mode: `url` (default) returns a 307 redirect to a "
            "presigned URL; `stream` returns the bytes directly."
        ),
    ),
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
    summary="Mint a fresh presigned URL for one media object",
    description=(
        "Returns the media metadata and a freshly-signed URL valid for 7 days. "
        "Use this when the URL embedded in an older webhook payload or in a "
        "previous GET response has expired but the media is still within its "
        "30-day retention.\n\n"
        "The `url` field is the only fresh value — all other fields are "
        "identical to what `GET /alerts/{id}` returns under `media[]`."
    ),
    responses={
        404: {"description": "Media not found, doesn't belong to this alert, or aged out (past 30-day retention)."},
        503: {"description": "Storage backend unavailable."},
    },
)
def get_alert_media_url(alert_id: str, media_id: str, db: DBSession, _auth = Depends(admin_or_scope("alerts:read"))):
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
    description=(
        "Returns every POST attempt made for this alert across all consumers, "
        "newest first. Each row captures one attempt: HTTP status, response "
        "excerpt (first 1 KB), error string on failure, and `next_retry_at` "
        "if another attempt is scheduled.\n\n"
        "Use this to:\n"
        "- Confirm a particular alert was delivered (`status=success`).\n"
        "- Diagnose a stuck delivery (`status=failed` with repeated timeouts).\n"
        "- See the give-up history (`status=giving_up` after 6 attempts).\n\n"
        "Delivery rows are retained ~30 days."
    ),
)
def list_deliveries(alert_id: str, db: DBSession, _auth = Depends(admin_or_scope("alerts:read"))):
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
    description=(
        "Enqueues a brand-new delivery for the given alert, starting at "
        "`attempt=1` with a fresh retry chain. Useful when:\n\n"
        "- A consumer was down during the original window and has since "
        "recovered; you want to push the alert through without waiting for the "
        "next scheduled retry (or after the alert has reached `giving_up`).\n"
        "- You're testing changes to the downstream signature/idempotency "
        "verification and want to retrigger a known-good payload.\n\n"
        "Returns the Celery task ID of the enqueued job (`delivery_id`). The "
        "actual POST happens asynchronously — check "
        "`GET /alerts/{id}/deliveries` to see the new attempt row.\n\n"
        "This does **not** dedupe with prior deliveries — the downstream "
        "platform should already handle `alert_id` idempotency."
    ),
    responses={404: {"description": "Alert not found."}},
)
def redeliver(alert_id: str, db: DBSession, _auth = Depends(admin_or_scope("alerts:read"))):
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
