"""
Pydantic schemas for Site Category and Category Mapping operations.

Category mappings attach to a **Site** (the physical place) as of migration
010 — a category describes a place, and its OSD colour applies to every camera
at that place regardless of which recorder the camera hangs off.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime
import uuid


class CategoryBase(BaseModel):
    """Base schema with common category fields."""

    name: str = Field(..., min_length=1, max_length=100, description="Unique category name")
    color: int = Field(..., description="Color in 0xFFRRGGBBAA format (as integer)")

    @field_validator('color')
    @classmethod
    def validate_color(cls, v: int) -> int:
        """Validate color is a valid 32-bit integer."""
        if v < 0 or v > 0xFFFFFFFF:
            raise ValueError('Color must be a valid 32-bit integer (0x00000000 to 0xFFFFFFFF)')
        return v


class CategoryCreate(CategoryBase):
    """Schema for creating a new category."""
    pass


class CategoryUpdate(BaseModel):
    """Schema for updating an existing category. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=100, description="Unique category name")
    color: Optional[int] = Field(None, description="Color in 0xFFRRGGBBAA format (as integer)")

    @field_validator('color')
    @classmethod
    def validate_color(cls, v: Optional[int]) -> Optional[int]:
        """Validate color is a valid 32-bit integer."""
        if v is not None and (v < 0 or v > 0xFFFFFFFF):
            raise ValueError('Color must be a valid 32-bit integer (0x00000000 to 0xFFFFFFFF)')
        return v


class CategoryResponse(BaseModel):
    """Basic category response schema."""

    id: uuid.UUID
    name: str
    color: int

    class Config:
        from_attributes = True


class CategoryDetailResponse(CategoryResponse):
    """Detailed category response schema with timestamps."""

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CategoryWithSiteCount(CategoryResponse):
    """Category response with count of sites using this category."""

    site_count: int = 0

    class Config:
        from_attributes = True


# Category Mapping Schemas

class AssignCategoryRequest(BaseModel):
    """Request schema for assigning a category to a site."""

    site_id: str = Field(..., min_length=1, max_length=255, description="Site ID to assign category to")
    category_id: uuid.UUID = Field(..., description="Category ID to assign")


class UnassignCategoryRequest(BaseModel):
    """Request schema for unassigning a category from a site."""

    site_id: str = Field(..., min_length=1, max_length=255, description="Site ID to unassign category from")
    category_id: uuid.UUID = Field(..., description="Category ID to unassign")


class BulkAssignRequest(BaseModel):
    """Request schema for bulk assigning categories to a site."""

    site_id: str = Field(..., min_length=1, max_length=255, description="Site ID")
    category_ids: List[uuid.UUID] = Field(..., description="List of category IDs to assign")


class CategoryMappingResponse(BaseModel):
    """Response schema for category mapping."""

    site_id: str
    category_id: uuid.UUID
    assigned_at: datetime

    class Config:
        from_attributes = True


class CategoryMappingDetailResponse(CategoryMappingResponse):
    """Detailed mapping response with site and category info."""

    site_name: Optional[str] = None
    category_name: Optional[str] = None
    category_color: Optional[int] = None

    class Config:
        from_attributes = True


class SiteWithCategories(BaseModel):
    """Site response with its assigned categories."""

    site_id: str
    site_name: str
    categories: List[CategoryResponse]

    class Config:
        from_attributes = True


class CategoryWithSites(BaseModel):
    """Category response with sites using this category."""

    id: uuid.UUID
    name: str
    color: int
    sites: List[dict]  # Simple dict with site_id and name

    class Config:
        from_attributes = True
