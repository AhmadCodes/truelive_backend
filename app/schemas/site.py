"""
Pydantic schemas for Site management endpoints.

As of migration 008 a **Site** is the parent "place" that owns one or more
Devices (NVR/DVRs). It carries only the location/contact data; NVR credentials
and flags live on the Device — see ``app/schemas/device.py``.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.schemas.actor import ActorStampsMixin
from app.schemas.device import DeviceResponse
from app.schemas.team import TeamResponse


class SiteBase(BaseModel):
    """Base site schema (the physical location)."""

    name: str = Field(..., min_length=1, max_length=255)
    customer_id: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    telephone: Optional[str] = Field(None, max_length=255)
    telephone2: Optional[str] = Field(None, max_length=255)
    telephone_police: Optional[str] = Field(None, max_length=100)
    telephone_fire: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    lat_long: Optional[str] = Field(None, max_length=100)


class SiteCreate(SiteBase):
    """Schema for creating a new site."""

    team_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Teams to assign this site to (at least one; a site may belong to many teams)",
    )


class SiteUpdate(BaseModel):
    """Schema for updating site details. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    customer_id: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    telephone: Optional[str] = Field(None, max_length=255)
    telephone2: Optional[str] = Field(None, max_length=255)
    telephone_police: Optional[str] = Field(None, max_length=100)
    telephone_fire: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    lat_long: Optional[str] = Field(None, max_length=100)


class SiteResponse(ActorStampsMixin):
    """Site response schema."""

    id: str
    name: str
    customer_id: Optional[str] = None
    address: Optional[str] = None
    telephone: Optional[str] = None
    telephone2: Optional[str] = None
    telephone_police: Optional[str] = None
    telephone_fire: Optional[str] = None
    notes: Optional[str] = None
    lat_long: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SiteSummaryResponse(SiteResponse):
    """Lightweight site response used in list views — no nested devices."""

    device_count: Optional[int] = 0
    teams: List[TeamResponse] = []

    class Config:
        from_attributes = True


class SiteDetailResponse(SiteResponse):
    """Detailed site response with its devices and team memberships."""

    device_count: Optional[int] = 0
    devices: List[DeviceResponse] = []
    teams: List[TeamResponse] = []

    class Config:
        from_attributes = True


class SiteListResponse(BaseModel):
    """Paginated site list response."""

    sites: List[SiteSummaryResponse]
    total: int
    page: int
    per_page: int
