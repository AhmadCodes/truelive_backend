"""
Configuration generation API endpoints.
"""

from fastapi import APIRouter, HTTPException, status
from app.api.deps import DBSession, AdminUser
from app.schemas.stream_config import (
    GenerateStreamConfigRequest,
    GenerateStreamConfigViewsRequest,
    GenerateStreamConfigResponse,
    StreamConfigStats,
    InvalidCameraIdsError
)
from app.services.stream_config_generator import (
    validate_camera_ids,
    generate_stream_config,
    generate_stream_config_views
)

router = APIRouter()


@router.post(
    "/generate-stream-config",
    status_code=status.HTTP_200_OK,
    responses={
        404: {
            "model": InvalidCameraIdsError,
            "description": "Some camera IDs not found in database"
        }
    }
)
async def generate_multi_stream_config(
    request: GenerateStreamConfigViewsRequest,
    db: DBSession,
    current_user: AdminUser
):
    """
    Generate multi-stream device configuration JSON (views format).

    Creates a device configuration where each screen carries a ``views`` dict
    (keyed "0","1",...), each view an independent grid with its own
    ``n_rows``/``n_cols`` and a flat ``sources`` list.

    **Default Configuration (if no parameters provided):**
    - 4 monitors
    - Each monitor: 5 views
    - Each view: 3×3 grid (9 sources)
    - Total: 4 × 5 × 9 = 180 camera slots

    **Custom Configuration:**
    - `monitors`: explicit per-monitor config; each monitor may give an explicit
      `views` list with a per-view grid (e.g. view 0 = 2×2, view 1 = 3×3), or omit
      `views` to get `num_views` copies of the default 3×3 grid
    - `num_monitors`: number of monitors when `monitors` is omitted
    - Optionally provide specific camera IDs to use
    - Configure display resolution and switch interval

    **Camera Selection:**
    - If `camera_ids` provided: Uses those specific cameras (in order)
    - If not provided: Uses available cameras from database (ordered by device, name)
    - `exclude_camera_ids`: Optionally exclude specific cameras from selection
    - If a camera appears in both lists, exclusion takes priority
    - Cameras fill each view's grid row-major, sequentially and distinctly,
      view-by-view across monitors; leftover slots become empty camera objects

    **Validation:**
    - All provided camera IDs must exist in database
    - Returns 404 error with list of invalid IDs if any don't exist

    **Returns:**
    - Device configuration JSON directly (no wrapper)

    Requires admin or super_admin privileges.
    """
    # Validate camera IDs if provided
    if request.camera_ids:
        invalid_ids = validate_camera_ids(request.camera_ids, db)
        if invalid_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "detail": "Some camera IDs not found in database",
                    "invalid_camera_ids": invalid_ids
                }
            )

    # Generate configuration
    try:
        config, stats_dict = generate_stream_config_views(
            monitors=request.monitors,
            num_monitors=request.num_monitors,
            camera_ids=request.camera_ids,
            exclude_camera_ids=request.exclude_camera_ids,
            width=request.width,
            height=request.height,
            switch_interval=request.switch_interval,
            db=db
        )

        # Return config directly without wrapper
        return config

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate stream configuration: {str(e)}"
        )


@router.post(
    "/generate-stream-config-legacy",
    status_code=status.HTTP_200_OK,
    responses={
        404: {
            "model": InvalidCameraIdsError,
            "description": "Some camera IDs not found in database"
        }
    }
)
async def generate_multi_stream_config_legacy(
    request: GenerateStreamConfigRequest,
    db: DBSession,
    current_user: AdminUser
):
    """
    Generate multi-stream device configuration JSON (legacy source_groups format).

    This is the previous behaviour of ``/generate-stream-config``, preserved
    verbatim: each screen carries a tile-major ``source_groups`` array with one
    uniform grid per screen.

    **Default Configuration (if no parameters provided):**
    - 4 screens
    - Each screen: 3×3 grid layout (9 tiles)
    - Each screen: 5 views per tile
    - Total: 4 × 9 × 5 = 180 camera slots

    **Custom Configuration:**
    - Specify number of screens and layout for each
    - Optionally provide specific camera IDs to use
    - Configure display resolution and switch interval

    **Camera Selection:**
    - If `camera_ids` provided: Uses those specific cameras
    - If not provided: Uses available cameras from database (ordered by device, name)
    - `exclude_camera_ids`: Optionally exclude specific cameras from selection
    - If a camera appears in both lists, exclusion takes priority
    - If not enough cameras: Fills remaining slots with empty camera objects

    **Validation:**
    - All provided camera IDs must exist in database
    - Returns 404 error with list of invalid IDs if any don't exist

    **Returns:**
    - Device configuration JSON directly (no wrapper)

    Requires admin or super_admin privileges.
    """
    # Validate camera IDs if provided
    if request.camera_ids:
        invalid_ids = validate_camera_ids(request.camera_ids, db)
        if invalid_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "detail": "Some camera IDs not found in database",
                    "invalid_camera_ids": invalid_ids
                }
            )

    # Generate configuration
    try:
        config, stats_dict = generate_stream_config(
            screens_config=request.screens,
            camera_ids=request.camera_ids,
            exclude_camera_ids=request.exclude_camera_ids,
            width=request.width,
            height=request.height,
            switch_interval=request.switch_interval,
            db=db
        )

        # Return config directly without wrapper
        return config

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate stream configuration: {str(e)}"
        )
