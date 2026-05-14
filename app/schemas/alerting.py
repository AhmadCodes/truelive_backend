"""
Pydantic schemas for the alerting feature API.

These schemas drive both the FastAPI request validation and the auto-generated
OpenAPI (Swagger) documentation. Every field carries a `description` and one or
more `examples` so a downstream integrator can read `/api/v1/docs` and know
exactly what to send.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------- enumerations ---------- #
#
# These string literals are the canonical valid values across the alerting
# pipeline. Centralized here so they render as `enum` lists in the OpenAPI
# schema and Swagger UI surfaces them as a dropdown.

EventType = Literal["alert"]
"""Normalized event classification.

In v1 every alert is the constant `alert` — the upstream AI filter (e.g.
Calipsa) already classified by sending the email, and this producer does not
parse email bodies to derive a finer category. The schema reserves a Literal
field shape so future enrichment can add types here and bump `schema_version`
on a contract break.

If you need finer categorization on the consumer side, derive it from
`subject` or fetch the raw `.eml` via `GET /alerts/{id}/raw`.
"""

ParserConfidence = Literal["exact", "heuristic", "llm_generated", "unparsed"]
"""How sure the producer is about the structured fields.

- `exact`         — current passthrough always emits this (no body parsing, so
                    nothing to be uncertain about)
- `heuristic`     — reserved for future template-aware parsing
- `llm_generated` — reserved for future LLM-driven enrichment
- `unparsed`      — reserved for future raw fallbacks

Today, expect `exact` on every alert.
"""

AlertMediaKind = Literal["snapshot", "video_clip", "attachment_other"]
"""Classification of an attached media object.

- `snapshot`         — single still image (typically JPEG/PNG)
- `video_clip`       — short video file
- `attachment_other` — non-media attachment (logs, JSON, etc.)
"""

DeliveryStatus = Literal["pending", "success", "failed", "giving_up"]
"""State of one webhook delivery attempt.

- `pending`    — row created, POST in flight
- `success`    — consumer responded 2xx
- `failed`     — non-2xx or timeout; another retry is scheduled
- `giving_up`  — exhausted retry chain (6 attempts over ~15h); ops alerted
"""


# ---------- alert addresses ---------- #

class AlertAddressResponse(BaseModel):
    """A per-camera inbound email address used by the upstream alert source.

    The address `{local_part}@{domain}` is what an operator pastes into the
    upstream system (e.g. the Calipsa per-camera alert destination). When mail
    arrives at this address it's routed through Postfix → LMTP →
    truelive-smtp-ingest → parsed into an alert.
    """
    id: str = Field(
        ..., description="UUID of the alert_address row.",
        examples=["a3f9c2e1-b3d7-4a2f-9c8b-1b3c4d5e6f70"],
    )
    camera_id: str = Field(
        ..., description="ID of the camera this address routes to.",
        examples=["9D7Q"],
    )
    local_part: str = Field(
        ...,
        description=(
            "Opaque per-camera token in the form `cam-<16-char base64url>`. "
            "Treated as case-insensitive by the LMTP recipient validator."
        ),
        examples=["cam-Xb3p9Hf2NkLqW8aZ"],
    )
    domain: str = Field(
        ..., description="Email domain (always `alerts.usvg.ai` in v1).",
        examples=["alerts.usvg.ai"],
    )
    is_active: bool = Field(
        ...,
        description=(
            "False = revoked (soft delete). The LMTP server rejects mail to "
            "revoked addresses with 550 5.1.1 No such recipient."
        ),
    )
    is_quarantined: bool = Field(
        ...,
        description=(
            "True = address is hard-blocked (e.g. runaway camera). Mail is "
            "rejected at LMTP RCPT TO with 550 5.7.1 Recipient quarantined. "
            "Unlike revoke, this is reversible — see `/unquarantine`."
        ),
    )
    revoked_at: Optional[datetime] = Field(
        None,
        description="UTC timestamp when `is_active` was flipped to false. Null if still active.",
    )
    created_at: datetime
    updated_at: datetime

    @property
    def address(self) -> str:
        return f"{self.local_part}@{self.domain}"

    class Config:
        from_attributes = True


class AlertAddressWithEmail(AlertAddressResponse):
    """Convenience response shape that materializes the full email address as `email`."""
    email: str = Field(
        ..., description="The full email address `{local_part}@{domain}`.",
        examples=["cam-Xb3p9Hf2NkLqW8aZ@alerts.usvg.ai"],
    )


class AlertAddressCreate(BaseModel):
    """Empty request body — the server generates the opaque local part.

    Provisioning is idempotent: if the camera already has an active address,
    that existing row is returned (HTTP 201 either way).
    """
    pass


class AlertAddressRotateResponse(BaseModel):
    """Atomic revoke+provision result. The old address becomes immediately
    invalid; the new one is the camera's new active address."""
    revoked_address: AlertAddressResponse = Field(
        ..., description="The previously-active address, now `is_active=false` with `revoked_at` set.",
    )
    new_address: AlertAddressResponse = Field(
        ..., description="The freshly-provisioned active address.",
    )


# ---------- alerts ---------- #

class AlertMediaResponse(BaseModel):
    """One attached media object (snapshot, clip, or other file)."""
    media_id: str = Field(
        ..., description="UUID of the media record (`alert_media.id`).",
        examples=["0193f8a3-7c8b-7d6e-9a2f-1b3c4d5e6f70"],
    )
    kind: AlertMediaKind = Field(
        ...,
        description=(
            "Media classification. One of:\n\n"
            "- `snapshot` — single still image (typically JPEG/PNG)\n"
            "- `video_clip` — short video file\n"
            "- `attachment_other` — non-media attachment (logs, JSON, etc.)"
        ),
        examples=["snapshot"],
    )
    content_type: Optional[str] = Field(
        None, description="MIME type as declared by the source.", examples=["image/jpeg"],
    )
    size_bytes: int = Field(..., description="Byte count of the stored object.", examples=[184320])
    sha256: str = Field(
        ..., description="SHA-256 hex digest of the stored object — use to verify integrity. Always exactly 64 lowercase hex chars.",
        examples=["4b54c69cd7c4a3d8e2f1b9a7c5d3e8f10c2a4b6d8e1f3a5c7e9b1d3f5a7c9e1b3"],
    )
    url: Optional[str] = Field(
        None,
        description=(
            "Presigned MinIO/S3 GET URL, valid for ~7 days from generation. "
            "Real URLs are 400–800 chars long (X-Amz-* query string carries the "
            "signature). If null, storage retrieval failed or the blob has aged "
            "out (media is retained 30 days). Use `GET /alerts/{id}/media/{media_id}` "
            "to mint a fresh URL."
        ),
        examples=[
            "https://s3.usvg.ai/truelive-alert-media/2026/05/14/74a06272-5833-41c5-86be-1edcb48a715d/93eda4f5-1208-417e-83b4-c0d0aa6e7eed.jpg"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=truelive-alerting-xxxxxxxx%2F20260514%2Fus-east-1%2Fs3%2Faws4_request"
            "&X-Amz-Date=20260514T143433Z"
            "&X-Amz-Expires=604800"
            "&X-Amz-SignedHeaders=host"
            "&X-Amz-Signature=4b54c69cd7c4a3d8e2f1b9a7c5d3e8f10c2a4b6d8e1f3a5c7e9b1d3f5a7c9e1b3"
        ],
    )
    url_expires_at: Optional[datetime] = Field(
        None, description="UTC time at which `url` stops working (if `url` is non-null).",
    )
    original_filename: Optional[str] = Field(
        None, description="Filename as declared by the upstream sender, if any.",
        examples=["D04-1.jpg"],
    )
    created_at: datetime

    class Config:
        from_attributes = True


class AlertParserInfo(BaseModel):
    """Provenance of the structured fields — which producer built this alert."""
    id: Optional[str] = Field(
        None, description="Stable producer identifier. Today always `passthrough_v1`.",
        examples=["passthrough_v1"],
    )
    version: Optional[int] = Field(
        None, description="Producer version (incremented if its output shape changes).", examples=[1],
    )
    confidence: ParserConfidence = Field(
        ...,
        description=(
            "How sure the producer is about the structured fields. Always `exact` in v1 — "
            "the passthrough producer doesn't parse email bodies so there's nothing to "
            "be uncertain about. `heuristic` / `llm_generated` / `unparsed` are reserved "
            "for future enrichment."
        ),
        examples=["exact"],
    )


class AlertResponse(BaseModel):
    """Full normalized alert payload.

    Same shape is used in two places: the response from `GET /alerts/{id}` and
    the body of outbound webhook deliveries (HMAC-signed). The `schema_version`
    field is the contract — additive changes don't bump it; consumers MUST
    ignore unknown fields. Renames/removes bump the version.
    """
    schema_version: str = Field(
        default="1.0",
        description="Payload schema version. Currently always `1.0`.",
        examples=["1.0"],
    )
    alert_id: str = Field(
        ..., description="UUID — primary handle for this alert.",
        examples=["0193f8a1-7c8b-7d6e-9a2f-1b3c4d5e6f70"],
    )
    camera_id: str = Field(..., description="Source camera ID.", examples=["9D7Q"])
    received_at: datetime = Field(..., description="UTC time the SMTP layer accepted the message.")
    detected_at: Optional[datetime] = Field(
        None,
        description=(
            "Best-effort event time (from email Date or body text). Null if the "
            "parser couldn't extract one — fall back to `received_at`."
        ),
    )
    event_type: EventType = Field(
        ...,
        description=(
            "Normalized event classification. Always `alert` in v1 — the upstream "
            "AI filter already classified by sending the email, and this producer "
            "does not parse email bodies. Reserved as a Literal so future per-type "
            "classification can be added with a `schema_version` bump."
        ),
        examples=["alert"],
    )
    event_subtype: Optional[str] = Field(
        None,
        description=(
            "Sub-classification. Always `ai_alert` in v1 (constant; producer does "
            "not derive subtype from body content). Free-form string so future "
            "enrichment can populate it without a schema break."
        ),
        examples=["ai_alert"],
    )
    confidence: Optional[float] = Field(
        None,
        description="Source-provided confidence score (0.0-1.0), or null if not provided.",
        ge=0.0, le=1.0,
    )
    subject: Optional[str] = Field(
        None, description="Email Subject header — always populated, useful for unparsed alerts.",
        examples=["Network Video Recorder: Motion Detected On Channel D4"],
    )
    body_text: Optional[str] = Field(
        None,
        description="Normalized plain-text body. HTML emails are stripped to text.",
    )
    media: list[AlertMediaResponse] = Field(
        default_factory=list,
        description="Attached media. May be empty. URLs auto-expire after ~7 days.",
    )
    parser: AlertParserInfo = Field(..., description="Parser provenance.")
    raw_message_id: str = Field(
        ..., description="Handle to the raw RFC822 source (use `GET /alerts/{id}/raw`).",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Reserved for future header- or MIME-level metadata. Always `{}` in v1 "
            "(producer does not derive any fields from body content). Consumers "
            "should treat all keys as optional."
        ),
        examples=[{}],
    )


class AlertListItem(BaseModel):
    """Compact alert summary returned by list endpoints."""
    alert_id: str
    camera_id: str
    received_at: datetime
    event_type: EventType = Field(
        ...,
        description="Always `alert` in v1. See AlertResponse.event_type for the full reservation rationale.",
        examples=["alert"],
    )
    event_subtype: Optional[str] = Field(None, examples=["ai_alert"])
    parser_confidence: ParserConfidence = Field(
        ...,
        description=(
            "Parser confidence. Always `exact` in v1; `heuristic` / `llm_generated` / "
            "`unparsed` are reserved for future enrichment that doesn't currently exist."
        ),
        examples=["exact"],
    )
    subject: Optional[str] = None

    class Config:
        from_attributes = True


class AlertListResponse(BaseModel):
    """Paginated list of alerts."""
    items: list[AlertListItem] = Field(..., description="Alerts matching the filter, newest first.")
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Pagination cursor for the next page. Always null in v1 — caller uses "
            "`received_before` with the oldest `received_at` from this page to "
            "fetch the next batch."
        ),
    )


# ---------- webhook consumers ---------- #

class WebhookConsumerCreate(BaseModel):
    """Register a downstream consumer that should receive alert webhooks."""
    name: str = Field(
        ..., min_length=1, max_length=255,
        description="Human-readable label, unique across consumers.",
        examples=["acme-monitoring-prod"],
    )
    url: HttpUrl = Field(
        ...,
        description=(
            "HTTPS endpoint that will receive POST `application/json` bodies. "
            "Must ack 2xx within 5 seconds; slower responses are treated as "
            "failures and retried."
        ),
        examples=["https://hooks.example.com/truelive/alerts"],
    )
    secret: str = Field(
        ..., min_length=16, max_length=512,
        description=(
            "Shared secret used by TrueLive to HMAC-SHA256 sign every outbound "
            "delivery (header `X-TrueLive-Signature: sha256=<hex>`). The "
            "consumer uses the same secret to verify signatures. **Never "
            "returned by any GET** — store it on your side at registration time. "
            "Minimum 16 chars; 32+ recommended."
        ),
        examples=["use-a-cryptographically-random-string-here-32-bytes-or-more"],
    )


class WebhookConsumerUpdate(BaseModel):
    """Partial update — only fields you want to change need to be present."""
    url: Optional[HttpUrl] = Field(None, description="New delivery URL.")
    secret: Optional[str] = Field(
        None, min_length=16, max_length=512,
        description=(
            "New HMAC secret. Rotating: future deliveries are signed with this; "
            "in-flight retries already in the queue still use the previous secret."
        ),
    )
    is_active: Optional[bool] = Field(
        None,
        description="Set to false to suspend deliveries without deleting the row.",
    )


class WebhookConsumerResponse(BaseModel):
    """A registered downstream consumer. `secret` is never returned."""
    id: str = Field(..., description="UUID of the consumer row.")
    name: str
    url: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WebhookDeliveryResponse(BaseModel):
    """One delivery attempt log entry. Multiple rows per alert when retried."""
    id: str = Field(..., description="UUID of the delivery attempt.")
    alert_id: str
    consumer_id: str
    attempt: int = Field(
        ...,
        description=(
            "1-indexed attempt number for this `(alert_id, consumer_id)` pair. "
            "Attempts 1-6 are made over ~15h before giving up."
        ),
        examples=[1],
    )
    status: DeliveryStatus = Field(
        ...,
        description=(
            "Lifecycle state of this delivery attempt. One of:\n\n"
            "- `pending` — row created, POST in flight\n"
            "- `success` — consumer responded 2xx\n"
            "- `failed` — non-2xx or timeout; another retry is scheduled\n"
            "- `giving_up` — exhausted retry chain (6 attempts over ~15h); ops alerted"
        ),
        examples=["success"],
    )
    http_status: Optional[int] = Field(
        None,
        description=(
            "HTTP status code returned by the consumer. Null if the POST never "
            "got a response (timeout / connection refused)."
        ),
        examples=[200],
    )
    response_excerpt: Optional[str] = Field(
        None,
        description="First 1 KB of the consumer's response body. Useful for debug.",
    )
    error: Optional[str] = Field(
        None, description="Network/transport error string. Null on success.",
    )
    attempted_at: datetime = Field(..., description="UTC time the POST was sent.")
    next_retry_at: Optional[datetime] = Field(
        None,
        description=(
            "Scheduled time of the next retry, or null if this was the final attempt."
        ),
    )

    class Config:
        from_attributes = True


class WebhookTestResponse(BaseModel):
    """Confirmation that a test delivery was enqueued."""
    delivery_id: str = Field(
        ...,
        description=(
            "Celery task ID for the enqueued delivery, or empty string if "
            "enqueue failed."
        ),
    )
    enqueued: bool = Field(
        ..., description="True if the delivery was queued; false if enqueue failed.",
    )
