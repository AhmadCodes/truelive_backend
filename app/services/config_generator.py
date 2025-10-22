"""
Configuration generator service.

Transforms database structure to device JSON format for PC applications.
"""
import logging
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session

from app.utils.url_processor import try_encode_rtsp_password
from app.models.site import Site
from app.models.category import SiteCategory
from app.models.camera import Camera
from app.models.screen import Screen
from app.models.site_camera_layout import SiteCamerasLayout

logger = logging.getLogger(__name__)


def generate_config(site_config: Dict[str, Any], db: Session) -> Dict[str, Any]:
    """
    Generate device configuration from database structure.

    Transforms the site configuration (from database) into the JSON format
    expected by PC applications.

    Args:
        site_config: Configuration dict with structure:
            {
                "pcs": {
                    "pc_id": {
                        "name": "PC Name",
                        "screens": {
                            "screen_id": {
                                "name": "Screen Name",
                                "layout": {"rows": 2, "columns": 2},
                                "switching_interval": 10
                            }
                        }
                    }
                },
                "mappings": {
                    "screen_to_cameras": {
                        "pc_id": {
                            "screen_id": {
                                "view_name": {
                                    "slot_1_1": {
                                        "site_id": "...",
                                        "camera_id": "...",
                                        "site_name": "...",
                                        "camera_name": "...",
                                        "rtsp_url": "...",
                                        "use_tcp": false
                                    }
                                }
                            }
                        }
                    }
                }
            }
        db: Database session for additional queries

    Returns:
        Device configuration dict:
            {
                "width": 640,
                "height": 480,
                "screens": [
                    {
                        "id": "screen_id",
                        "display_idx": 0,
                        "switchInterval": 10,
                        "title": "Screen Name",
                        "source_groups": [
                            [
                                {
                                    "id": "site_id_camera_id",
                                    "osd_text": "Camera Name (Site Name)",
                                    "url": "rtsp://encoded_url",
                                    "osd_color": "0xFFFFFFFF",
                                    "LocationUris": ["rtsp://...", "rtsp://..."],
                                    "use_tcp": false
                                },
                                ...
                            ],
                            ...
                        ]
                    }
                ]
            }
    """
    # Initialize default configuration
    config = {
        "width": 640,
        "height": 480,
        "screens": []
    }

    pcs = site_config.get("pcs", {})
    mappings = site_config.get("mappings", {}).get("screen_to_cameras", {})

    for pc_id, pc_data in pcs.items():
        screens = pc_data.get("screens", {})

        for screen_id, screen_data in screens.items():
            layout = screen_data.get("layout", {})

            # Get screen name/title from database
            try:
                screen = db.query(Screen).filter(Screen.id == screen_id).first()
                screen_title = screen.name if screen else f"Screen {len(config['screens']) + 1}"
            except Exception as e:
                logger.error(f"Error getting screen title for screen {screen_id}: {e}")
                screen_title = f"Screen {len(config['screens']) + 1}"

            # Calculate display_idx based on current screen count
            display_idx = len(config["screens"])

            screen_config = {
                "id": f"pc{pc_id}_screen_{screen_id}",
                "display_idx": display_idx,
                "switchInterval": screen_data.get("switching_interval", 10),
                "title": screen_title,
                "source_groups": []
            }

            screen_views = mappings.get(pc_id, {}).get(screen_id, {})
            valid_views = {}

            logger.info(f"Processing screen {screen_id}, found {len(screen_views)} views in mappings")

            # Filter views that have at least one camera (omit entirely empty views)
            for view_key, view_data in screen_views.items():
                logger.info(f"Found view '{view_key}', view_data type: {type(view_data)}, bool: {bool(view_data)}")
                if view_data:  # Check if view exists
                    has_camera = False
                    logger.info(f"Checking view '{view_key}', has {len(view_data)} slots")

                    # Check if any slot has actual camera data (not empty)
                    for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                        row_num = (slot_num - 1) // layout["columns"] + 1
                        col_num = (slot_num - 1) % layout["columns"] + 1
                        slot_key = f"slot_{row_num}_{col_num}"

                        slot_data = view_data.get(slot_key)
                        if slot_data:
                            logger.info(f"Slot {slot_key} has data: {list(slot_data.keys())}")
                            if slot_data.get('camera_id'):  # Has actual camera
                                logger.info(f"Slot {slot_key} has camera_id: {slot_data.get('camera_id')}")
                                has_camera = True
                                break

                    if has_camera:
                        logger.info(f"View '{view_key}' has cameras, adding to valid_views")
                        # Extract view number for sorting (handles both "view_1" and "view 1" formats)
                        view_lower = view_key.lower()
                        if view_lower.startswith('view'):
                            try:
                                # Try to extract number from "view_1" or "view 1" format
                                view_parts = view_key.replace('_', ' ').split()
                                if len(view_parts) >= 2:
                                    view_num = int(view_parts[1])
                                    valid_views[view_num] = view_data
                                else:
                                    valid_views[view_key] = view_data
                            except (ValueError, IndexError):
                                valid_views[view_key] = view_data
                        else:  # For named views
                            valid_views[view_key] = view_data
                    else:
                        logger.info(f"View '{view_key}' has no cameras, omitting")

            logger.info(f"Screen {screen_id} has {len(valid_views)} valid views after filtering")

            if valid_views:
                # Sort views by key (numeric keys first, then string keys)
                # This ensures view_1, view_2, view_3, etc. are in correct rotation order
                sorted_view_keys = sorted(
                    valid_views.keys(),
                    key=lambda x: (isinstance(x, str), x)
                )

                # Process each slot position across all views
                for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                    slot_sources = []
                    row_num = (slot_num - 1) // layout["columns"]
                    col_num = (slot_num - 1) % layout["columns"]
                    slot_key = f"slot_{row_num + 1}_{col_num + 1}"

                    # Build sources for this slot from all valid views
                    for view_key in sorted_view_keys:
                        view_data = valid_views[view_key]
                        slot_data = view_data.get(slot_key)

                        if slot_data and slot_data.get('camera_id'):
                            # Get site category color
                            osd_color = _get_site_color(
                                slot_data.get('site_id', ''), db
                            )

                            # Get LocationUris from site_cameras_layout
                            location_uris = _get_location_uris(
                                slot_data.get('site_id', ''), db
                            )

                            # Create source entry
                            try:
                                source_entry = {
                                    "id": f"{slot_data.get('site_id', '')}_{slot_data.get('camera_id', '')}",
                                    "osd_text": f"{slot_data.get('camera_name', '')} ({slot_data.get('site_name', '')})",
                                    "url": try_encode_rtsp_password(slot_data.get('rtsp_url', '')),
                                    "osd_color": osd_color,
                                    "LocationUris": location_uris,
                                    "use_tcp": slot_data.get("use_tcp", False)
                                }
                                slot_sources.append(source_entry)
                            except Exception as e:
                                logger.error(f"Error creating slot source entry: {e}")
                                # On error, add empty to maintain alignment
                                slot_sources.append(_create_empty_source())
                        else:
                            # View doesn't have camera for this slot - add empty for alignment
                            slot_sources.append(_create_empty_source())

                    # Add slot to source_groups
                    screen_config["source_groups"].append(slot_sources)

                config["screens"].append(screen_config)

    return config


def _get_site_color(site_id: str, db: Session) -> str:
    """
    Get OSD color for a site from its category.

    Args:
        site_id: Site identifier
        db: Database session

    Returns:
        Hex color string (e.g., "0xFFFFFFFF")
    """
    try:
        if not site_id:
            return "0xFFFFFFFF"  # Default white

        # Query site categories through the mapping table
        from app.models.category import SiteCategoryMapping

        mapping = db.query(SiteCategoryMapping).filter(
            SiteCategoryMapping.site_id == site_id
        ).first()

        if mapping:
            category = db.query(SiteCategory).filter(
                SiteCategory.id == mapping.category_id
            ).first()

            if category:
                # Format as 0xAARRGGBB (ARGB: Alpha, Red, Green, Blue)
                return f"0x{category.color:08X}"

        return "0xFFFFFFFF"  # Default white

    except Exception as e:
        logger.error(f"Error getting site color for site {site_id}: {e}")
        return "0xFFFFFFFF"  # Default white


def _get_location_uris(site_id: str, db: Session) -> List[str]:
    """
    Get all camera URLs for a site from site_cameras_layout table.

    Args:
        site_id: Site identifier
        db: Database session

    Returns:
        List of RTSP URLs
    """
    location_uris = []

    try:
        if not site_id:
            logger.warning("_get_location_uris called with empty site_id")
            return location_uris

        logger.debug(f"Getting LocationUris for site_id: {site_id}")

        # Get all site_cameras_layout entries for this site
        site_layouts = db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.site_id == site_id
        ).all()

        logger.debug(f"Found {len(site_layouts)} site_cameras_layout entries for site {site_id}")

        # Extract camera URLs
        for site_layout in site_layouts:
            try:
                camera = db.query(Camera).filter(
                    Camera.id == site_layout.camera_id
                ).first()

                if camera and camera.rtsp_url:
                    # URL-encode the password in RTSP URL
                    encoded_url = try_encode_rtsp_password(camera.rtsp_url)
                    location_uris.append(encoded_url)
                    logger.debug(f"Added camera {camera.id} URL to LocationUris for site {site_id}")
                else:
                    logger.warning(f"Camera {site_layout.camera_id} not found or has no RTSP URL for site {site_id}")

            except Exception as e:
                logger.error(f"Error getting camera for LocationUris: {e}")
                continue

        logger.info(f"Total LocationUris for site {site_id}: {len(location_uris)}")

    except Exception as e:
        logger.error(f"Error getting LocationUris for site {site_id}: {e}")

    return location_uris


def _create_empty_source() -> Dict[str, Any]:
    """
    Create an empty source entry for unpopulated slots.

    Returns:
        Empty source dict
    """
    return {
        "id": "",
        "osd_text": "",
        "url": "",
        "osd_color": "0xFFFFFFFF",
        "LocationUris": [],
        "use_tcp": False
    }
