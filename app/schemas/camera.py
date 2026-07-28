"""
Pydantic schemas for Camera CRUD operations.
"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

from app.schemas.actor import ActorStampsMixin


class CameraBase(BaseModel):
    """Base schema with common camera fields."""

    name: str = Field(..., min_length=1, max_length=255, description="Display name of the camera")
    rtsp_url: str = Field(..., description="RTSP URL for camera streaming")
    main_stream_url: Optional[str] = Field(None, description="Main stream URL for camera (optional)")
    new: bool = Field(True, description="Flag indicating if this is a newly added camera")
    use_tcp: Optional[bool] = Field(None, description="Force RTSP over TCP; null to inherit from device")


class CameraCreate(CameraBase):
    """Schema for creating a new camera."""

    id: str = Field(..., min_length=1, max_length=255, description="Unique identifier for the camera")
    device_id: str = Field(..., min_length=1, max_length=255, description="Device this camera belongs to")


class CameraUpdate(BaseModel):
    """Schema for updating an existing camera. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Display name of the camera")
    rtsp_url: Optional[str] = Field(None, description="RTSP URL for camera streaming")
    main_stream_url: Optional[str] = Field(None, description="Main stream URL for camera")
    new: Optional[bool] = Field(None, description="Flag indicating if this is a newly added camera")
    use_tcp: Optional[bool] = Field(
        None,
        description="Force RTSP over TCP; null to inherit from device (PUT null to clear override)"
    )
    device_id: Optional[str] = Field(None, min_length=1, max_length=255, description="Device this camera belongs to")


class CameraResponse(ActorStampsMixin):
    """Basic camera response schema."""

    id: str
    device_id: str
    device_name: Optional[str] = None
    name: str
    rtsp_url: str
    main_stream_url: Optional[str]
    new: bool
    use_tcp: Optional[bool] = None

    class Config:
        from_attributes = True


class CameraDetailResponse(CameraResponse):
    """Detailed camera response schema with timestamps."""

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CameraSummary(BaseModel):
    """Summary schema for listing cameras."""

    id: str
    device_id: str
    name: str
    new: bool
    use_tcp: Optional[bool] = None

    class Config:
        from_attributes = True
