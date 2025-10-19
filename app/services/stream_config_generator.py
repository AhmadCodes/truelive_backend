"""
Service for generating multi-stream device configurations.

Generates device JSON configs matching json_format.md specification
for 180-camera (or custom) streaming setups.
"""

import logging
from typing import List, Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.site import Site
from app.models.site_camera_layout import SiteCamerasLayout
from app.models.category import SiteCategoryMapping
from app.schemas.stream_config import ScreenConfigInput
from app.utils.url_processor import encode_rtsp_password

logger = logging.getLogger(__name__)


def validate_camera_ids(camera_ids: List[str], db: Session) -> List[str]:
    """
    Validate that camera IDs exist in the database.

    Args:
        camera_ids: List of camera IDs to validate
        db: Database session

    Returns:
        List of camera IDs that don't exist in database (empty if all valid)
    """
    if not camera_ids:
        return []

    # Query for existing cameras
    existing_cameras = db.query(Camera.id).filter(Camera.id.in_(camera_ids)).all()
    existing_ids = {cam.id for cam in existing_cameras}

    # Find IDs that don't exist
    invalid_ids = [cam_id for cam_id in camera_ids if cam_id not in existing_ids]

    return invalid_ids


def get_cameras_for_config(
    camera_ids: Optional[List[str]],
    exclude_camera_ids: Optional[List[str]],
    count: int,
    db: Session
) -> List[Camera]:
    """
    Fetch cameras for configuration.

    Args:
        camera_ids: Optional list of specific camera IDs to use
        exclude_camera_ids: Optional list of camera IDs to exclude
        count: Number of cameras needed
        db: Database session

    Returns:
        List of Camera objects (may be fewer than count if not enough available)
    """
    exclude_set = set(exclude_camera_ids) if exclude_camera_ids else set()

    if camera_ids:
        # Use specific cameras in the order provided, excluding any in exclude list
        cameras = []
        for cam_id in camera_ids:
            if cam_id not in exclude_set:
                camera = db.query(Camera).filter(Camera.id == cam_id).first()
                if camera:
                    cameras.append(camera)
                    if len(cameras) >= count:
                        break
        return cameras
    else:
        # Fetch available cameras ordered by site name, camera name
        query = db.query(Camera).join(Site).order_by(Site.name, Camera.name)

        # Exclude cameras if specified
        if exclude_set:
            query = query.filter(Camera.id.notin_(exclude_set))

        cameras = query.limit(count).all()
        return cameras


def get_location_uris_for_camera(camera: Camera, db: Session) -> List[str]:
    """
    Get LocationUris array for a camera (all cameras from its site).

    Args:
        camera: Camera object
        db: Database session

    Returns:
        List of RTSP URLs for all cameras at this camera's site
    """
    location_uris = []

    if not camera.site_id:
        return location_uris

    # Get all site_cameras_layout entries for this site
    site_layouts = db.query(SiteCamerasLayout).filter(
        SiteCamerasLayout.site_id == camera.site_id
    ).all()

    # Get cameras from layouts
    for layout in site_layouts:
        site_camera = db.query(Camera).filter(Camera.id == layout.camera_id).first()
        if site_camera and site_camera.rtsp_url:
            # Process URL to encode passwords
            processed_url = encode_rtsp_password(site_camera.rtsp_url)
            location_uris.append(processed_url)

    return location_uris


def get_osd_color_for_camera(camera: Camera, db: Session) -> str:
    """
    Get OSD color for a camera from its site category.

    Args:
        camera: Camera object
        db: Database session

    Returns:
        Hex color string in format "0xFFRRGGBB" (default: white)
    """
    default_color = "0xFFFFFFFF"  # White

    if not camera.site_id:
        return default_color

    # Get site category mapping
    mapping = db.query(SiteCategoryMapping).filter(
        SiteCategoryMapping.site_id == camera.site_id
    ).first()

    if mapping and mapping.category and mapping.category.color:
        return mapping.category.color

    return default_color


def create_camera_object(
    camera: Camera,
    db: Session,
    use_tcp: bool = False
) -> Dict[str, Any]:
    """
    Create a camera object matching json_format.md specification.

    Args:
        camera: Camera database object
        db: Database session
        use_tcp: Force TCP transport

    Returns:
        Camera object dict with id, osd_text, url, osd_color, LocationUris, use_tcp
    """
    # Get site for camera
    site = db.query(Site).filter(Site.id == camera.site_id).first()
    site_name = site.name if site else "Unknown Site"

    # Process RTSP URL to encode passwords
    processed_url = encode_rtsp_password(camera.rtsp_url) if camera.rtsp_url else ""

    # Get OSD color from site category
    osd_color = get_osd_color_for_camera(camera, db)

    # Get LocationUris for this camera's site
    location_uris = get_location_uris_for_camera(camera, db)

    return {
        "id": f"{camera.site_id}_{camera.id}",
        "osd_text": f"{camera.name} ({site_name})",
        "url": processed_url,
        "osd_color": osd_color,
        "LocationUris": location_uris,
        "use_tcp": camera.use_tcp if hasattr(camera, 'use_tcp') and camera.use_tcp is not None else use_tcp
    }


def create_empty_camera_object() -> Dict[str, Any]:
    """
    Create an empty camera object for unused tiles.

    Returns:
        Empty camera object matching json_format.md spec
    """
    return {
        "id": "",
        "osd_text": "",
        "url": "",
        "osd_color": "0xFFFFFFFF",
        "LocationUris": [],
        "use_tcp": False
    }


def distribute_cameras_to_screens(
    cameras: List[Camera],
    screens_config: List[ScreenConfigInput],
    db: Session
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Distribute cameras across screens, tiles, and views.

    Args:
        cameras: List of Camera objects to distribute
        screens_config: Screen configuration list
        db: Database session

    Returns:
        Tuple of (list of screen data dicts, number of cameras used)
    """
    screens_data = []
    camera_index = 0
    cameras_used = 0

    for screen_idx, screen_config in enumerate(screens_config):
        # Calculate total tiles for this screen
        total_tiles = screen_config.total_tiles

        # Create source_groups array (one tile array per grid position)
        source_groups = []

        # Iterate through grid positions in row-major order
        for tile_idx in range(total_tiles):
            tile_cameras = []

            # Add cameras for each view in this tile
            for view_idx in range(screen_config.num_views):
                if camera_index < len(cameras):
                    # Use actual camera
                    camera = cameras[camera_index]
                    camera_obj = create_camera_object(camera, db)
                    tile_cameras.append(camera_obj)
                    camera_index += 1
                    cameras_used += 1
                else:
                    # Not enough cameras - use empty object
                    tile_cameras.append(create_empty_camera_object())

            source_groups.append(tile_cameras)

        # Create screen data
        screen_name = screen_config.name or f"Screen {screen_idx + 1}"
        screen_data = {
            "config": screen_config,
            "source_groups": source_groups,
            "name": screen_name,
            "display_idx": screen_idx
        }
        screens_data.append(screen_data)

    return screens_data, cameras_used


def build_device_config(
    screens_data: List[Dict[str, Any]],
    width: int,
    height: int,
    switch_interval: int
) -> Dict[str, Any]:
    """
    Build final device configuration JSON matching json_format.md.

    Args:
        screens_data: List of screen data dicts from distribute_cameras_to_screens
        width: Display width in pixels
        height: Display height in pixels
        switch_interval: Seconds between view rotations

    Returns:
        Device config dict matching json_format.md specification
    """
    screens = []

    for screen_data in screens_data:
        screen_id = f"generated_screen_{screen_data['display_idx']}"

        screen_obj = {
            "id": screen_id,
            "display_idx": screen_data["display_idx"],
            "switchInterval": switch_interval,
            "title": screen_data["name"],
            "source_groups": screen_data["source_groups"]
        }
        screens.append(screen_obj)

    return {
        "width": width,
        "height": height,
        "screens": screens
    }


def generate_stream_config(
    screens_config: List[ScreenConfigInput],
    camera_ids: Optional[List[str]],
    exclude_camera_ids: Optional[List[str]],
    width: int,
    height: int,
    switch_interval: int,
    db: Session
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Generate complete stream configuration.

    Args:
        screens_config: List of screen configurations
        camera_ids: Optional list of specific camera IDs to use
        exclude_camera_ids: Optional list of camera IDs to exclude
        width: Display width
        height: Display height
        switch_interval: View rotation interval
        db: Database session

    Returns:
        Tuple of (device config dict, stats dict)
    """
    # Calculate total cameras needed
    total_camera_slots = sum(screen.total_camera_slots for screen in screens_config)
    total_tiles = sum(screen.total_tiles for screen in screens_config)

    logger.info(
        f"Generating stream config: {len(screens_config)} screens, "
        f"{total_tiles} tiles, {total_camera_slots} camera slots"
    )

    # Get cameras
    cameras = get_cameras_for_config(camera_ids, exclude_camera_ids, total_camera_slots, db)
    cameras_available = db.query(Camera).count()

    logger.info(f"Fetched {len(cameras)} cameras ({cameras_available} available in database)")

    # Distribute cameras to screens
    screens_data, cameras_used = distribute_cameras_to_screens(cameras, screens_config, db)

    # Build device config
    config = build_device_config(screens_data, width, height, switch_interval)

    # Calculate stats
    stats = {
        "total_screens": len(screens_config),
        "total_tiles": total_tiles,
        "total_camera_slots": total_camera_slots,
        "cameras_used": cameras_used,
        "cameras_available": cameras_available,
        "empty_slots": total_camera_slots - cameras_used
    }

    logger.info(
        f"Generated config: {stats['cameras_used']}/{stats['total_camera_slots']} slots filled, "
        f"{stats['empty_slots']} empty"
    )

    return config, stats
