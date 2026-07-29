"""
Pydantic schemas for stream configuration generation.
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Optional, Dict, Any

# Defense-in-depth cap on total camera slots for the views-format endpoint.
# The default request is 180 slots; this ceiling (~55x) is generous for any real
# video wall while preventing an authenticated caller from amplifying a tiny
# request body into billions of allocated slot objects.
MAX_TOTAL_SLOTS = 10000


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


class ViewGridInput(BaseModel):
    """Grid size for a single view (independent per view in the new format)."""
    n_rows: int = Field(3, ge=1, le=10, description="Number of rows in this view's grid (1-10)")
    n_cols: int = Field(3, ge=1, le=10, description="Number of columns in this view's grid (1-10)")


class MonitorConfigInput(BaseModel):
    """Configuration for a single monitor (screen) in the new views-format request.

    Either give an explicit per-view grid list (``views``) for heterogeneous
    grids (e.g. view 0 = 2x2, view 1 = 3x3), or omit it to fall back to
    ``num_views`` copies of the default 3x3 grid.
    """
    views: Optional[List[ViewGridInput]] = Field(
        None,
        max_length=64,
        description="Explicit per-view grids. If omitted, num_views copies of the default 3x3 grid are used",
    )
    num_views: int = Field(
        5, ge=1, le=64, description="Views for this monitor when 'views' is omitted (each a default 3x3 grid)"
    )
    name: Optional[str] = Field(None, max_length=255, description="Optional monitor/screen name")


class GenerateStreamConfigViewsRequest(BaseModel):
    """Request body for the new views-format stream configuration.

    Optionally controls the number of monitors, the number of views per monitor,
    and the grid size of each individual view. All fields are optional; omitting
    everything yields 4 monitors x 5 views x 3x3 grid (180 camera slots), matching
    the legacy default.
    """
    monitors: Optional[List[MonitorConfigInput]] = Field(
        None,
        max_length=64,
        description="Explicit monitor configs. If omitted, num_monitors default monitors are used",
    )
    num_monitors: int = Field(
        4, ge=1, le=64, description="Number of monitors when 'monitors' is omitted (each 5 views of 3x3)"
    )
    camera_ids: Optional[List[str]] = Field(
        None,
        description="List of camera IDs to use. If not provided, uses available cameras from database",
    )
    exclude_camera_ids: Optional[List[str]] = Field(
        None,
        description="Camera IDs to exclude. If an ID is in both camera_ids and exclude_camera_ids, exclusion wins",
    )
    width: int = Field(640, ge=320, le=3840, description="Display width in pixels")
    height: int = Field(480, ge=240, le=2160, description="Display height in pixels")
    switch_interval: int = Field(10, ge=0, description="Seconds between view rotations (0 = no rotation)")

    @field_validator('camera_ids', 'exclude_camera_ids')
    @classmethod
    def deduplicate_camera_ids(cls, v):
        """Remove duplicate camera IDs (order-preserving)."""
        if v is not None:
            return list(dict.fromkeys(v))
        return v

    @model_validator(mode="after")
    def _cap_total_slots(self):
        """Reject requests whose resolved slot count would be excessive.

        Mirrors the fallback resolution in the service (monitors omitted ->
        num_monitors default monitors; a monitor's views omitted -> num_views
        copies of the default grid) so per-field caps alone can't be combined
        into a huge explicit request. Raises a validation error (HTTP 422).
        """
        default = ViewGridInput()  # 3x3 by field defaults
        default_slots = default.n_rows * default.n_cols

        monitors = self.monitors
        if monitors is None:
            # Omitted monitors resolve to num_monitors default MonitorConfigInput()
            # instances, each with its own default num_views.
            total = self.num_monitors * MonitorConfigInput().num_views * default_slots
        else:
            total = 0
            for monitor in monitors:
                if monitor.views is not None:
                    total += sum(v.n_rows * v.n_cols for v in monitor.views)
                else:
                    total += monitor.num_views * default_slots

        if total > MAX_TOTAL_SLOTS:
            raise ValueError(
                f"Requested configuration is too large ({total} camera slots); "
                f"maximum is {MAX_TOTAL_SLOTS}"
            )
        return self


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
