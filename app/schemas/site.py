"""
Pydantic schemas for site management endpoints.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import uuid


class SiteBase(BaseModel):
    """Base site schema."""
    name: str = Field(..., min_length=1, max_length=255)
    nvr_username: str = Field(..., min_length=1, max_length=255)
    nvr_password: str = Field(..., min_length=1)
    customer_id: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    telephone: Optional[str] = Field(None, max_length=255)
    telephone2: Optional[str] = Field(None, max_length=255)
    telephone_police: Optional[str] = Field(None, max_length=100)
    telephone_fire: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    lat_long: Optional[str] = Field(None, max_length=100)
    use_tcp: bool = Field(False, description="Site-wide default for RTSP TCP transport (overridable per camera)")


class SiteCreate(SiteBase):
    """Schema for creating a new site."""
    pass


class SiteUpdate(BaseModel):
    """Schema for updating site details."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    nvr_username: Optional[str] = Field(None, min_length=1, max_length=255)
    nvr_password: Optional[str] = Field(None, min_length=1)
    customer_id: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    telephone: Optional[str] = Field(None, max_length=255)
    telephone2: Optional[str] = Field(None, max_length=255)
    telephone_police: Optional[str] = Field(None, max_length=100)
    telephone_fire: Optional[str] = Field(None, max_length=100)
    notes: Optional[str] = None
    lat_long: Optional[str] = Field(None, max_length=100)
    use_tcp: Optional[bool] = Field(None, description="Site-wide default for RTSP TCP transport")


class CategoryAssignment(BaseModel):
    """Schema for assigning category to site."""
    category_id: uuid.UUID


class SiteCategoryResponse(BaseModel):
    """Site category response schema."""
    id: uuid.UUID
    name: str
    color: int
    color_hex: str

    class Config:
        from_attributes = True


class SiteResponse(BaseModel):
    """Site response schema."""
    id: str
    name: str
    nvr_username: Optional[str] = None  # Allow empty strings from database
    nvr_password: Optional[str] = None  # Allow empty strings from database
    sureview_site: bool = False
    new: bool = True
    customer_id: Optional[str] = None
    address: Optional[str] = None
    telephone: Optional[str] = None
    telephone2: Optional[str] = None
    telephone_police: Optional[str] = None
    telephone_fire: Optional[str] = None
    notes: Optional[str] = None
    lat_long: Optional[str] = None
    use_tcp: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SiteDetailResponse(SiteResponse):
    """Detailed site response with relationships."""
    camera_count: Optional[int] = 0
    categories: List[SiteCategoryResponse] = []

    class Config:
        from_attributes = True


class SiteListResponse(BaseModel):
    """Paginated site list response."""
    sites: List[SiteDetailResponse]
    total: int
    page: int
    per_page: int
