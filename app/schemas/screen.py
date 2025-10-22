"""
Pydantic schemas for Screen and View operations.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime


class ScreenBase(BaseModel):
    """Base schema with common screen fields."""

    name: str = Field(..., min_length=1, max_length=255, description="Screen name")
    pc_id: str = Field(..., min_length=1, max_length=50, description="PC ID this screen is connected to")
    rows: int = Field(..., ge=1, le=4, description="Number of rows in the screen grid (1-4)")
    columns: int = Field(..., ge=1, le=4, description="Number of columns in the screen grid (1-4)")
    switching_interval: Optional[int] = Field(None, ge=1, description="Seconds for view switching (>=1)")


class ScreenCreate(ScreenBase):
    """Schema for creating a new screen."""

    id: str = Field(..., min_length=1, max_length=100, description="Unique screen identifier")


class ScreenUpdate(BaseModel):
    """Schema for updating an existing screen. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Screen name")
    pc_id: Optional[str] = Field(None, min_length=1, max_length=50, description="PC ID this screen is connected to")
    rows: Optional[int] = Field(None, ge=1, le=4, description="Number of rows in the screen grid (1-4)")
    columns: Optional[int] = Field(None, ge=1, le=4, description="Number of columns in the screen grid (1-4)")
    switching_interval: Optional[int] = Field(None, ge=1, description="Seconds for view switching (>=1)")


class ScreenResponse(BaseModel):
    """Basic screen response schema."""

    id: str
    name: str
    pc_id: str
    rows: int
    columns: int
    total_slots: int
    switching_interval: Optional[int] = None

    class Config:
        from_attributes = True


class ScreenDetailResponse(ScreenResponse):
    """Detailed screen response schema with timestamps."""

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PCInfo(BaseModel):
    """PC information for screen responses."""

    id: str
    name: str
    ip_address: Optional[str] = None
    role: str

    class Config:
        from_attributes = True


class ScreenWithPC(ScreenResponse):
    """Screen response with PC information."""

    pc: Optional[PCInfo] = None

    class Config:
        from_attributes = True


# View Schemas

class ViewBase(BaseModel):
    """Base schema with common view fields."""

    name: str = Field(..., min_length=1, max_length=50, description="View name")
    layout_rows: int = Field(..., ge=1, le=10, description="Number of rows in the view layout grid (1-10)")
    layout_columns: int = Field(..., ge=1, le=10, description="Number of columns in the view layout grid (1-10)")
    view_number: int = Field(..., ge=1, description="Sequential number of this view on the screen")


class ViewCreate(ViewBase):
    """Schema for creating a new view."""

    id: str = Field(..., min_length=1, max_length=255, description="Unique view identifier")
    screen_id: str = Field(..., min_length=1, max_length=100, description="Screen ID this view belongs to")


class ViewUpdate(BaseModel):
    """Schema for updating an existing view. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=50, description="View name")
    layout_rows: Optional[int] = Field(None, ge=1, le=10, description="Number of rows in the view layout grid (1-10)")
    layout_columns: Optional[int] = Field(None, ge=1, le=10, description="Number of columns in the view layout grid (1-10)")
    view_number: Optional[int] = Field(None, ge=1, description="Sequential number of this view on the screen")


class ViewResponse(BaseModel):
    """Basic view response schema."""

    id: str
    screen_id: str
    name: str
    layout_rows: int
    layout_columns: int
    view_number: int
    total_slots: int

    class Config:
        from_attributes = True


class ViewDetailResponse(ViewResponse):
    """Detailed view response schema with timestamps."""

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CameraMappingInfo(BaseModel):
    """Camera mapping information for a slot."""

    slot_row: int
    slot_col: int
    site_id: Optional[str] = None
    site_name: Optional[str] = None
    camera_id: Optional[str] = None
    camera_name: Optional[str] = None
    playing_state: bool = False

    class Config:
        from_attributes = True


class ViewWithMappings(ViewResponse):
    """View response with camera mappings."""

    mappings: List[CameraMappingInfo] = []

    class Config:
        from_attributes = True


class ScreenWithViews(ScreenWithPC):
    """Screen response with views."""

    views: List[ViewResponse] = []
    view_count: int = 0

    class Config:
        from_attributes = True


class ScreenLayoutResponse(ScreenWithPC):
    """Complete screen layout with views and camera mappings."""

    views: List[ViewWithMappings] = []
    view_count: int = 0

    class Config:
        from_attributes = True


# Screen Mapping Schemas

class ScreenMappingBase(BaseModel):
    """Base schema for screen mapping."""

    slot_row: int = Field(..., ge=1, description="Grid row position (1-indexed)")
    slot_col: int = Field(..., ge=1, description="Grid column position (1-indexed)")
    site_id: Optional[str] = Field(None, description="Site ID")
    camera_id: Optional[str] = Field(None, description="Camera ID")
    playing_state: bool = Field(False, description="Active playback state")


class ScreenMappingCreate(ScreenMappingBase):
    """Schema for creating a screen mapping."""

    view_id: str = Field(..., min_length=1, max_length=255, description="View ID")


class ScreenMappingUpdate(BaseModel):
    """Schema for updating a screen mapping."""

    site_id: Optional[str] = Field(None, description="Site ID")
    camera_id: Optional[str] = Field(None, description="Camera ID")
    playing_state: Optional[bool] = Field(None, description="Active playback state")


class ScreenMappingResponse(BaseModel):
    """Screen mapping response schema."""

    id: int
    pc_id: str
    screen_id: str
    view_id: str
    slot_row: int
    slot_col: int
    site_id: Optional[str] = None
    camera_id: Optional[str] = None
    playing_state: bool

    class Config:
        from_attributes = True
