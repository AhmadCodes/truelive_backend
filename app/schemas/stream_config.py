"""
Pydantic schemas for stream configuration generation.
"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any


class ScreenConfigInput(BaseModel):
    """Configuration for a single screen in the stream config."""
    layout_rows: int = Field(3, ge=1, le=10, description="Number of rows in grid (1-10)")
    layout_cols: int = Field(3, ge=1, le=10, description="Number of columns in grid (1-10)")
    num_views: int = Field(5, ge=1, description="Number of views per tile (rotation depth)")
    name: Optional[str] = Field(None, max_length=255, description="Optional screen name")

    @property
    def total_tiles(self) -> int:
        """Calculate total tiles in this screen's grid."""
        return self.layout_rows * self.layout_cols

    @property
    def total_camera_slots(self) -> int:
        """Calculate total camera slots (tiles × views)."""
        return self.total_tiles * self.num_views


class GenerateStreamConfigRequest(BaseModel):
    """Request body for generating stream configuration."""
    screens: List[ScreenConfigInput] = Field(
        default_factory=lambda: [
            ScreenConfigInput(layout_rows=3, layout_cols=3, num_views=5, name=f"Screen {i+1}")
            for i in range(4)
        ],
        description="Screen configurations. Default: 4 screens with 3×3 layout, 5 views each"
    )
    camera_ids: Optional[List[str]] = Field(
        None,
        description="List of camera IDs to use. If not provided, uses available cameras from database"
    )
    exclude_camera_ids: Optional[List[str]] = Field(
        None,
        description="List of camera IDs to exclude from selection. If a camera ID appears in both camera_ids and exclude_camera_ids, exclusion takes priority"
    )
    width: int = Field(640, ge=320, le=3840, description="Display width in pixels")
    height: int = Field(480, ge=240, le=2160, description="Display height in pixels")
    switch_interval: int = Field(10, ge=0, description="Seconds between view rotations (0 = no rotation)")

    @field_validator('camera_ids', 'exclude_camera_ids')
    @classmethod
    def deduplicate_camera_ids(cls, v):
        """Remove duplicate camera IDs."""
        if v is not None:
            return list(dict.fromkeys(v))  # Preserve order while removing duplicates
        return v


class StreamConfigStats(BaseModel):
    """Statistics about generated stream configuration."""
    total_screens: int = Field(..., description="Number of screens in config")
    total_tiles: int = Field(..., description="Total tiles across all screens")
    total_camera_slots: int = Field(..., description="Total camera slots (tiles × views)")
    cameras_used: int = Field(..., description="Number of actual cameras populated")
    cameras_available: int = Field(..., description="Total cameras available in database")
    empty_slots: int = Field(..., description="Number of empty slots (not filled)")


class GenerateStreamConfigResponse(BaseModel):
    """Response containing generated stream configuration."""
    config: Dict[str, Any] = Field(..., description="Device configuration JSON matching json_format.md spec")
    stats: StreamConfigStats = Field(..., description="Statistics about the generated configuration")


class InvalidCameraIdsError(BaseModel):
    """Error response when invalid camera IDs are provided."""
    detail: str = Field(..., description="Error message")
    invalid_camera_ids: List[str] = Field(..., description="List of camera IDs that don't exist in database")
