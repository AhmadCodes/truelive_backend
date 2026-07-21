"""
Pydantic schemas for Site Category and Category Mapping operations.

Note: despite the historical ``Site*`` class names, category mappings attach to
a **Device** (NVR/DVR) as of migration 008.
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
    """Category response with count of devices using this category."""

    device_count: int = 0

    class Config:
        from_attributes = True


# Category Mapping Schemas

class AssignCategoryRequest(BaseModel):
    """Request schema for assigning a category to a device."""

    device_id: str = Field(..., min_length=1, max_length=255, description="Device ID to assign category to")
    category_id: uuid.UUID = Field(..., description="Category ID to assign")


class UnassignCategoryRequest(BaseModel):
    """Request schema for unassigning a category from a device."""

    device_id: str = Field(..., min_length=1, max_length=255, description="Device ID to unassign category from")
    category_id: uuid.UUID = Field(..., description="Category ID to unassign")


class BulkAssignRequest(BaseModel):
    """Request schema for bulk assigning categories to a device."""

    device_id: str = Field(..., min_length=1, max_length=255, description="Device ID")
    category_ids: List[uuid.UUID] = Field(..., description="List of category IDs to assign")


class CategoryMappingResponse(BaseModel):
    """Response schema for category mapping."""

    device_id: str
    category_id: uuid.UUID
    assigned_at: datetime

    class Config:
        from_attributes = True


class CategoryMappingDetailResponse(CategoryMappingResponse):
    """Detailed mapping response with device and category info."""

    device_name: Optional[str] = None
    category_name: Optional[str] = None
    category_color: Optional[int] = None

    class Config:
        from_attributes = True


class DeviceWithCategories(BaseModel):
    """Device response with its assigned categories."""

    device_id: str
    device_name: str
    categories: List[CategoryResponse]

    class Config:
        from_attributes = True


class CategoryWithDevices(BaseModel):
    """Category response with devices using this category."""

    id: uuid.UUID
    name: str
    color: int
    devices: List[dict]  # Simple dict with device_id and name

    class Config:
        from_attributes = True
