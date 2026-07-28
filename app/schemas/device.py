"""
Pydantic schemas for Device management endpoints.

A **Device** is a single NVR/DVR recorder. It is the entity that was called
``Site`` prior to migration 008 — it keeps the NVR credentials and flags, while
all location/contact data moved up to the parent Site (``app/schemas/site.py``).
Every Device belongs to exactly one Site and may be reparented via
``PUT /api/v1/devices/{id}``.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import uuid

from app.schemas.actor import ActorStampsMixin


class DeviceBase(BaseModel):
    """Base device schema."""
    name: str = Field(..., min_length=1, max_length=255)
    nvr_username: str = Field(..., min_length=1, max_length=255)
    nvr_password: str = Field(..., min_length=1)
    use_tcp: bool = Field(
        False,
        description="Device-wide default for RTSP TCP transport (overridable per camera)"
    )


class DeviceCreate(DeviceBase):
    """Schema for creating a new device. Requires a parent site."""
    site_id: str = Field(
        ..., min_length=1, max_length=255,
        description="Parent Site this device belongs to"
    )


class DeviceUpdate(BaseModel):
    """Schema for updating device details.

    Setting ``site_id`` reparents the device to another Site.
    """
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    site_id: Optional[str] = Field(
        None, min_length=1, max_length=255,
        description="Move this device to another Site"
    )
    nvr_username: Optional[str] = Field(None, min_length=1, max_length=255)
    nvr_password: Optional[str] = Field(None, min_length=1)
    use_tcp: Optional[bool] = Field(
        None, description="Device-wide default for RTSP TCP transport"
    )


class CategoryAssignment(BaseModel):
    """Schema for assigning a category to a site."""
    category_id: uuid.UUID


class SiteCategoryResponse(BaseModel):
    """Category response as attached to a device."""
    id: uuid.UUID
    name: str
    color: int
    color_hex: str

    class Config:
        from_attributes = True


class DeviceResponse(ActorStampsMixin):
    """Device response schema."""
    id: str
    name: str
    site_id: str
    site_name: Optional[str] = None
    nvr_username: Optional[str] = None  # Allow empty strings from database
    nvr_password: Optional[str] = None  # Allow empty strings from database
    new: bool = True
    use_tcp: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeviceDetailResponse(DeviceResponse):
    """Detailed device response with relationships."""
    camera_count: Optional[int] = 0
    categories: List[SiteCategoryResponse] = []

    class Config:
        from_attributes = True


class DeviceListResponse(BaseModel):
    """Paginated device list response."""
    devices: List[DeviceDetailResponse]
    total: int
    page: int
    per_page: int
