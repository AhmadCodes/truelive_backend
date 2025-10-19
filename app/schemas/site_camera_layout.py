"""
Pydantic schemas for site camera layout management.
"""

from pydantic import BaseModel, Field
from typing import List
from datetime import datetime


class SiteCameraLayoutSlot(BaseModel):
    """Schema for a single camera slot in the layout."""
    slot_row: int = Field(..., ge=1, le=4, description="Row position (1-4)")
    slot_col: int = Field(..., ge=1, le=4, description="Column position (1-4)")
    camera_id: str = Field(..., description="Camera ID")
    camera_name: str = Field(..., description="Camera name")


class SiteCameraLayoutConfigResponse(BaseModel):
    """Response schema for site camera layout configuration."""
    site_id: str = Field(..., description="Site ID")
    site_name: str = Field(..., description="Site name")
    n_rows: int = Field(..., ge=1, le=4, description="Number of rows in grid")
    n_cols: int = Field(..., ge=1, le=4, description="Number of columns in grid")
    total_slots: int = Field(..., description="Total grid positions (rows × cols)")
    cameras_populated: int = Field(..., description="Number of cameras actually assigned")
    cameras: List[SiteCameraLayoutSlot] = Field(..., description="Camera assignments")
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AutoPopulateResponse(BaseModel):
    """Response for auto-populate endpoints."""
    site_id: str
    site_name: str
    camera_count: int
    grid_size: str  # e.g., "2×2", "3×4"
    cameras_populated: int
    message: str


class BulkAutoPopulateResponse(BaseModel):
    """Response for bulk auto-populate endpoint."""
    total_sites: int
    sites_processed: int
    sites_skipped: int
    total_cameras_populated: int
    results: List[AutoPopulateResponse]
    errors: List[dict] = Field(default_factory=list)
