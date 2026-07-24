"""
Pydantic schemas for PC operations.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Literal
from datetime import datetime


class PCBase(BaseModel):
    """Base schema with common PC fields."""

    name: str = Field(..., min_length=1, max_length=255, description="PC name")
    ip_address: Optional[str] = Field(
        None, max_length=50, description="IP address of the PC"
    )
    gpu_type: Optional[str] = Field(None, max_length=100, description="GPU type/model")
    role: Literal["controller", "manager"] = Field(
        "controller", description="PC role (controller or manager)"
    )
    manager_id: Optional[str] = Field(
        None, max_length=50, description="Manager PC ID (for controller PCs)"
    )


class PCCreate(PCBase):
    """Schema for creating a new PC."""

    id: str = Field(
        ..., min_length=1, max_length=50, description="Unique PC identifier"
    )
    team_id: str = Field(
        ..., min_length=1, max_length=50, description="Team this PC belongs to"
    )

    @field_validator("manager_id")
    @classmethod
    def validate_manager_id(cls, v: Optional[str], info) -> Optional[str]:
        """Validate that manager_id is only set for controller PCs."""
        # Convert empty string to None
        if v == "":
            v = None

        if v is not None:
            role = info.data.get("role")
            if role == "manager":
                raise ValueError("Manager PCs cannot have a manager_id")
        return v


class PCUpdate(BaseModel):
    """Schema for updating an existing PC. All fields are optional."""

    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="PC name"
    )
    ip_address: Optional[str] = Field(
        None, max_length=50, description="IP address of the PC"
    )
    gpu_type: Optional[str] = Field(None, max_length=100, description="GPU type/model")
    role: Optional[Literal["controller", "manager"]] = Field(
        None, description="PC role (controller or manager)"
    )
    manager_id: Optional[str] = Field(
        None, max_length=50, description="Manager PC ID (for controller PCs)"
    )
    screen_layout_id: Optional[str] = Field(
        None, max_length=100, description="Screen layout ID this PC is assigned to"
    )
    team_id: Optional[str] = Field(
        None, max_length=50, description="Team this PC belongs to"
    )


class PCResponse(BaseModel):
    """Basic PC response schema."""

    id: str
    name: str
    ip_address: Optional[str] = None
    gpu_type: Optional[str] = None
    role: str
    manager_id: Optional[str] = None
    screen_layout_id: Optional[str] = None
    team_id: Optional[str] = None
    team_name: Optional[str] = None
    last_connected: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_applied: Optional[datetime] = None

    class Config:
        from_attributes = True


class PCDetailResponse(PCResponse):
    """Detailed PC response schema with timestamps."""

    auth_token: Optional[str] = None
    token_expiry: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PCWithScreenCount(PCResponse):
    """PC response with count of screens connected to this PC."""

    screen_count: int = 0
    auth_token: Optional[str] = None
    token_expiry: Optional[datetime] = None

    class Config:
        from_attributes = True


class PCWithManager(PCResponse):
    """PC response with manager PC information."""

    manager: Optional[PCResponse] = None

    class Config:
        from_attributes = True


class PCWithControlled(PCResponse):
    """PC response with controlled PCs (for manager PCs)."""

    controlled_pcs: List[PCResponse] = []
    screen_count: int = 0

    class Config:
        from_attributes = True


class ScreenSummary(BaseModel):
    """Summary information for a screen."""

    id: str
    name: str
    rows: int
    columns: int
    total_slots: int
    switching_interval: Optional[int] = None

    class Config:
        from_attributes = True


class PCWithScreens(PCWithScreenCount):
    """PC response with detailed screen information."""

    screens: List[ScreenSummary] = []

    class Config:
        from_attributes = True


# Screen Configuration Schemas


class ScreenConfigRequest(BaseModel):
    """Configuration for a single screen in PC setup."""

    layout_rows: int = Field(
        ..., ge=1, le=10, description="Number of rows in view grid (1-10)"
    )
    layout_cols: int = Field(
        ..., ge=1, le=10, description="Number of columns in view grid (1-10)"
    )
    num_views: int = Field(
        ..., ge=1, description="Number of views per screen (rotation depth)"
    )
    name: str = Field(..., min_length=1, max_length=100, description="Screen name")
    switch_interval: int = Field(
        ..., ge=1, description="Seconds between view rotations"
    )


class ConfigurePCScreensRequest(BaseModel):
    """Request body for configuring PC screens and camera mappings."""

    screens: List[ScreenConfigRequest] = Field(..., description="Screen configurations")
    camera_ids: List[str] = Field(
        ..., description="List of camera IDs to distribute across screens and views"
    )
    width: Optional[int] = Field(
        None, description="Display width (optional, for future use)"
    )
    height: Optional[int] = Field(
        None, description="Display height (optional, for future use)"
    )

    @field_validator("camera_ids")
    @classmethod
    def deduplicate_camera_ids(cls, v):
        """Remove duplicate camera IDs while preserving order."""
        if v is not None:
            return list(dict.fromkeys(v))
        return v


class ConfigurePCScreensResponse(BaseModel):
    """Response for PC screen configuration."""

    pc_id: str = Field(..., description="PC identifier")
    screens_created: int = Field(..., description="Number of new screens created")
    screens_updated: int = Field(..., description="Number of existing screens updated")
    views_created: int = Field(..., description="Total number of views created")
    mappings_created: int = Field(
        ..., description="Total number of camera mappings created"
    )
    cameras_used: int = Field(..., description="Number of cameras actually mapped")
    message: str = Field(..., description="Status message")


class PCTokenResponse(BaseModel):
    """Response for PC token generation."""

    pc_id: str = Field(..., description="PC identifier")
    pc_name: str = Field(..., description="PC name")
    auth_token: str = Field(..., description="JWT authentication token for PC")
    token_expiry: int = Field(..., description="Unix timestamp when token expires")
    expires_in_hours: int = Field(..., description="Token validity in hours")
    message: str = Field(..., description="Status message")


class PCConnectionStatus(BaseModel):
    """Response for PC connection status."""

    pc_id: str = Field(..., description="PC identifier")
    pc_name: str = Field(..., description="PC name")
    is_connected: bool = Field(
        ..., description="Whether PC is currently connected to WebSocket server"
    )
    last_connected: Optional[datetime] = Field(
        None, description="Last connection timestamp from database"
    )
    last_seen: Optional[datetime] = Field(
        None,
        description="Last time the PC was seen alive on the websocket heartbeat",
    )
    last_applied: Optional[datetime] = Field(
        None, description="Last configuration applied timestamp"
    )
    websocket_connected_at: Optional[str] = Field(
        None, description="Current WebSocket connection start time (ISO format)"
    )

    class Config:
        from_attributes = True


class AllPCsConnectionStatus(BaseModel):
    """Response for all PCs connection status."""

    total_pcs: int = Field(..., description="Total number of PCs in database")
    connected_count: int = Field(..., description="Number of currently connected PCs")
    disconnected_count: int = Field(..., description="Number of disconnected PCs")
    pcs: List[PCConnectionStatus] = Field(
        ..., description="List of PCs with their connection status"
    )


# Import Config Schemas


class ImportConfigRequest(BaseModel):
    """Request body for importing PC configuration from device JSON."""

    config: dict = Field(
        ..., description="Device configuration JSON (same format as deploy config)"
    )


class ImportConfigResponse(BaseModel):
    """Response for PC config import operation."""

    success: bool = Field(..., description="Whether import was successful")
    pc_id: str = Field(..., description="PC identifier")
    screens_created: int = Field(..., description="Number of screens created")
    views_created: int = Field(..., description="Number of views created")
    mappings_created: int = Field(..., description="Number of screen mappings created")
    cameras_skipped: int = Field(
        0, description="Number of cameras skipped (not found in database)"
    )
    devices_skipped: int = Field(
        0, description="Number of devices skipped (not found in database)"
    )
    message: str = Field(..., description="Status message")


class CopyLayoutResponse(BaseModel):
    """Response for copy layout operation."""

    success: bool = Field(..., description="Whether copy was successful")
    source_pc_id: str = Field(..., description="Source PC identifier")
    target_pc_id: str = Field(..., description="Target PC identifier")
    screens_copied: int = Field(..., description="Number of screens copied")
    views_copied: int = Field(..., description="Number of views copied")
    mappings_copied: int = Field(..., description="Number of screen mappings copied")
    message: str = Field(..., description="Status message")
