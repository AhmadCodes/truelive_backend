"""
Pydantic schemas for device camera layout management.

Note: despite the historical ``Site*`` class names, camera layouts attach to a
**Device** (NVR/DVR) as of migration 008.
"""

from pydantic import BaseModel, Field, validator
from typing import List, Optional
from datetime import datetime


class SiteCameraLayoutSlot(BaseModel):
    """Schema for a single camera slot in the layout."""
    slot_row: int = Field(..., ge=1, le=4, description="Row position (1-4)")
    slot_col: int = Field(..., ge=1, le=4, description="Column position (1-4)")
    camera_id: str = Field(..., description="Camera ID")
    camera_name: str = Field(..., description="Camera name")


class SiteCameraLayoutConfigResponse(BaseModel):
    """Response schema for device camera layout configuration."""
    device_id: str = Field(..., description="Device ID")
    device_name: str = Field(..., description="Device name")
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
    device_id: str
    device_name: str
    camera_count: int
    grid_size: str  # e.g., "2×2", "3×4"
    cameras_populated: int
    message: str


class BulkAutoPopulateResponse(BaseModel):
    """Response for bulk auto-populate endpoint."""
    total_devices: int
    devices_processed: int
    devices_skipped: int
    total_cameras_populated: int
    results: List[AutoPopulateResponse]
    errors: List[dict] = Field(default_factory=list)


# Input schemas for manual layout configuration

class CameraSlotInput(BaseModel):
    """Input schema for a camera slot assignment."""
    slot_row: int = Field(..., ge=1, le=4, description="Row position (1-4)")
    slot_col: int = Field(..., ge=1, le=4, description="Column position (1-4)")
    camera_id: str = Field(..., description="Camera ID to assign to this slot")


class SaveLayoutRequest(BaseModel):
    """Request schema for saving/updating device camera layout."""
    n_rows: int = Field(..., ge=1, le=4, description="Number of rows in grid (1-4)")
    n_cols: int = Field(..., ge=1, le=4, description="Number of columns in grid (1-4)")
    camera_slots: List[CameraSlotInput] = Field(
        ...,
        max_items=16,
        description="Camera slot assignments (max 16 for 4×4 grid)"
    )

    @validator('camera_slots')
    def validate_slots(cls, v, values):
        """Validate camera slots for duplicates and grid bounds."""
        if not v:
            return v

        # Check for duplicate camera IDs
        camera_ids = [slot.camera_id for slot in v]
        if len(camera_ids) != len(set(camera_ids)):
            raise ValueError("Duplicate camera IDs found in camera_slots")

        # Check for duplicate slot positions
        positions = [(slot.slot_row, slot.slot_col) for slot in v]
        if len(positions) != len(set(positions)):
            raise ValueError("Duplicate slot positions found in camera_slots")

        # Check that all positions are within grid bounds
        if 'n_rows' in values and 'n_cols' in values:
            n_rows = values['n_rows']
            n_cols = values['n_cols']
            for slot in v:
                if slot.slot_row > n_rows:
                    raise ValueError(f"slot_row {slot.slot_row} exceeds grid rows {n_rows}")
                if slot.slot_col > n_cols:
                    raise ValueError(f"slot_col {slot.slot_col} exceeds grid columns {n_cols}")

        return v


class SaveLayoutResponse(BaseModel):
    """Response schema for save layout operation."""
    device_id: str
    device_name: str
    n_rows: int
    n_cols: int
    total_slots: int
    cameras_populated: int
    message: str
