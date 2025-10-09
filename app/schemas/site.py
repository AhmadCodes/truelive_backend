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


class SiteCreate(SiteBase):
    """Schema for creating a new site."""
    pass


class SiteUpdate(BaseModel):
    """Schema for updating site details."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    nvr_username: Optional[str] = Field(None, min_length=1, max_length=255)
    nvr_password: Optional[str] = Field(None, min_length=1)


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


class SiteResponse(SiteBase):
    """Site response schema."""
    id: str
    sureview_site: bool = False
    new: bool = True
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
