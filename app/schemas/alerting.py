"""
Pydantic schemas for the alerting feature API.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------- alert addresses ---------- #

class AlertAddressResponse(BaseModel):
    id: str
    camera_id: str
    local_part: str
    domain: str
    is_active: bool
    is_quarantined: bool
    revoked_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @property
    def address(self) -> str:
        return f"{self.local_part}@{self.domain}"

    class Config:
        from_attributes = True


class AlertAddressWithEmail(AlertAddressResponse):
    """Convenience response shape that materializes the full email address."""
    email: str


class AlertAddressCreate(BaseModel):
    """No body — server generates the local part. Kept as a schema so future
    fields (e.g. label) have a place to land without an endpoint signature change."""
    pass


class AlertAddressRotateResponse(BaseModel):
    revoked_address: AlertAddressResponse
    new_address: AlertAddressResponse


# ---------- alerts ---------- #

class AlertMediaResponse(BaseModel):
    media_id: str = Field(..., description="UUID of the media record (alert_media.id)")
    kind: str
    content_type: Optional[str] = None
    size_bytes: int
    sha256: str
    url: Optional[str] = Field(None, description="Presigned MinIO URL, valid ~7 days")
    url_expires_at: Optional[datetime] = None
    original_filename: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AlertParserInfo(BaseModel):
    id: Optional[str] = None
    version: Optional[int] = None
    confidence: str


class AlertResponse(BaseModel):
    """Full normalized alert payload — used both for GET /alerts/{id} and webhook bodies."""
    schema_version: str = "1.0"
    alert_id: str
    camera_id: str
    received_at: datetime
    detected_at: Optional[datetime] = None
    event_type: str
    event_subtype: Optional[str] = None
    confidence: Optional[float] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    media: list[AlertMediaResponse] = Field(default_factory=list)
    parser: AlertParserInfo
    raw_message_id: str
    extra: dict[str, Any] = Field(default_factory=dict)


class AlertListItem(BaseModel):
    alert_id: str
    camera_id: str
    received_at: datetime
    event_type: str
    event_subtype: Optional[str] = None
    parser_confidence: str
    subject: Optional[str] = None

    class Config:
        from_attributes = True


class AlertListResponse(BaseModel):
    items: list[AlertListItem]
    next_cursor: Optional[str] = None


# ---------- webhook consumers ---------- #

class WebhookConsumerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: HttpUrl
    secret: str = Field(..., min_length=16, max_length=512, description="HMAC shared secret. Min 16 chars.")


class WebhookConsumerUpdate(BaseModel):
    url: Optional[HttpUrl] = None
    secret: Optional[str] = Field(None, min_length=16, max_length=512)
    is_active: Optional[bool] = None


class WebhookConsumerResponse(BaseModel):
    id: str
    name: str
    url: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WebhookDeliveryResponse(BaseModel):
    id: str
    alert_id: str
    consumer_id: str
    attempt: int
    status: str
    http_status: Optional[int] = None
    response_excerpt: Optional[str] = None
    error: Optional[str] = None
    attempted_at: datetime
    next_retry_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WebhookTestResponse(BaseModel):
    delivery_id: str
    enqueued: bool
