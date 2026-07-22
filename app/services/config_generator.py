"""
Configuration generator service.

Transforms database structure to device JSON format for PC applications.
"""
import logging
from typing import Dict, Any, List
from sqlalchemy.orm import Session

from app.utils.url_processor import try_encode_rtsp_password
from app.models.category import SiteCategory
from app.models.camera import Camera
from app.models.device import Device
from app.models.screen import Screen
from app.models.view import View
from app.models.site_camera_layout import SiteCamerasLayout

logger = logging.getLogger(__name__)


def generate_config(site_config: Dict[str, Any], db: Session) -> Dict[str, Any]:
    """
    Generate device configuration from database structure.

    Transforms the PC configuration (from database) into the JSON format
    expected by PC applications.

    WIRE FORMAT IS FROZEN. The ``site_id`` / ``site_name`` / ``osd_text`` keys
    below (both in the input mapping structure produced by
    ``config_loader.load_pc_config`` and in the emitted source entries) keep
    meaning the **Device** (the NVR/DVR — what used to be called a Site before
    migration 008). They must never be renamed or re-pointed at the new parent
    Site: PC clients in the field consume them as-is.

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
    config = {"width": 640, "height": 480, "screens": []}

    pcs = site_config.get("pcs", {})
    mappings = site_config.get("mappings", {}).get("screen_to_cameras", {})

    # The slot "site_id" key carries a **Device** id (frozen wire format), but
    # categories and camera layouts hang off the parent Site. Resolve the whole
    # device → parent-site mapping once rather than per slot.
    try:
        device_to_site = {
            device_id: parent_site_id
            for device_id, parent_site_id in db.query(Device.id, Device.site_id).all()
        }
    except Exception as e:
        logger.error(f"Error loading device → site mapping: {e}")
        device_to_site = {}

    for pc_id, pc_data in pcs.items():
        layout_id = pc_data.get("screen_layout_id")
        screens = pc_data.get("screens", {})

        for screen_id, screen_data in screens.items():
            layout = screen_data.get("layout", {})

            # Get screen name/title from database
            try:
                screen = db.query(Screen).filter(Screen.id == screen_id).first()
                screen_title = (
                    screen.name if screen else f"Screen {len(config['screens']) + 1}"
                )
            except Exception as e:
                logger.error(f"Error getting screen title for screen {screen_id}: {e}")
                screen_title = f"Screen {len(config['screens']) + 1}"

            # Calculate display_idx based on current screen count
            display_idx = len(config["screens"])

            screen_config = {
                "id": f"pc{pc_id}_layout{layout_id}_screen{screen_id}",
                "display_idx": display_idx,
                "switchInterval": screen_data.get("switching_interval", 10),
                "title": screen_title,
                "source_groups": [],
            }

            screen_views = mappings.get(pc_id, {}).get(screen_id, {})
            valid_views = {}
            view_metadata = {}  # Map view_key to View object metadata

            logger.info(
                f"Processing screen {screen_id}, found {len(screen_views)} views in mappings"
            )

            # Query View objects from database for this screen to get metadata.
            # Key by view.id (UUID): the loader passes the same key, and unlike
            # view.name it tolerates duplicate view names within a screen.
            try:
                db_views = db.query(View).filter(View.screen_id == screen_id).all()
                view_id_to_obj = {str(view.id): view for view in db_views}
                logger.info(
                    f"Found {len(db_views)} views in database for screen {screen_id}"
                )
            except Exception as e:
                logger.error(f"Error querying views for screen {screen_id}: {e}")
                view_id_to_obj = {}

            # Filter views that have at least one camera (omit entirely empty views)
            for view_key, view_data in screen_views.items():
                logger.info(
                    f"Found view '{view_key}', view_data type: {type(view_data)}, bool: {bool(view_data)}"
                )
                if view_data:  # Check if view exists
                    has_camera = False
                    logger.info(
                        f"Checking view '{view_key}', has {len(view_data)} slots"
                    )

                    # Check if any slot has actual camera data (not empty)
                    for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                        row_num = (slot_num - 1) // layout["columns"] + 1
                        col_num = (slot_num - 1) % layout["columns"] + 1
                        slot_key = f"slot_{row_num}_{col_num}"

                        slot_data = view_data.get(slot_key)
                        if slot_data:
                            logger.info(
                                f"Slot {slot_key} has data: {list(slot_data.keys())}"
                            )
                            if slot_data.get("camera_id"):  # Has actual camera
                                logger.info(
                                    f"Slot {slot_key} has camera_id: {slot_data.get('camera_id')}"
                                )
                                has_camera = True
                                break

                    if has_camera:
                        logger.info(
                            f"View '{view_key}' has cameras, adding to valid_views"
                        )
                        valid_views[view_key] = view_data

                        # Store View object metadata if found in database
                        if view_key in view_id_to_obj:
                            view_metadata[view_key] = view_id_to_obj[view_key]
                        else:
                            logger.warning(f"View '{view_key}' not found in database")
                    else:
                        logger.info(f"View '{view_key}' has no cameras, omitting")

            logger.info(
                f"Screen {screen_id} has {len(valid_views)} valid views after filtering"
            )

            if valid_views:
                # Sort views by created_at timestamp from database (fallback to name if not in DB)
                def get_sort_key(view_key):
                    if view_key in view_metadata:
                        return (
                            0,
                            view_metadata[view_key].created_at,
                        )  # Sort by timestamp
                    else:
                        return (1, view_key)  # Fallback to name sorting

                sorted_view_keys = sorted(valid_views.keys(), key=get_sort_key)

                # Process each slot position across all views
                for slot_num in range(1, layout["rows"] * layout["columns"] + 1):
                    slot_sources = []
                    row_num = (slot_num - 1) // layout["columns"]
                    col_num = (slot_num - 1) % layout["columns"]
                    slot_key = f"slot_{row_num + 1}_{col_num + 1}"

                    # Build sources for this slot from all valid views
                    for view_n, view_key in enumerate(sorted_view_keys):
                        view_data = valid_views[view_key]
                        slot_data = view_data.get(slot_key)

                        # Get view metadata (needed for both empty and filled slots)
                        view_obj = view_metadata.get(view_key)
                        view_id = str(view_obj.id) if view_obj else ""
                        view_name = view_obj.name if view_obj else view_key

                        if slot_data and slot_data.get("camera_id"):
                            # NOTE: 'site_id' here is a FROZEN wire-format key that
                            # carries the Device id (see module docstring). The
                            # colour and the layout are looked up from that
                            # device's parent Site.
                            parent_site_id = device_to_site.get(
                                slot_data.get("site_id", ""), ""
                            )

                            # Get site category color
                            osd_color = _get_site_color(parent_site_id, db)

                            # Get LocationUris from site_cameras_layout
                            location_uris = _get_location_uris(parent_site_id, db)

                            # Create source entry with all new fields.
                            # FROZEN wire format: "site_id"/"site_name"/"osd_text"
                            # carry the Device id/name — do NOT rename or re-point.
                            try:
                                source_entry = {
                                    "LocationUris": location_uris,
                                    "id": f"{slot_data.get('site_id', '')}_{slot_data.get('camera_id', '')}",
                                    "camera_id": slot_data.get("camera_id", ""),
                                    "camera_name": slot_data.get("camera_name", ""),
                                    "site_id": slot_data.get("site_id", ""),
                                    "site_name": slot_data.get("site_name", ""),
                                    "view_n": view_n,  # Integer, not string
                                    "view_id": view_id,
                                    "view_name": view_name,
                                    "pos_x": row_num,
                                    "pos_y": col_num,
                                    "osd_color": osd_color,
                                    "osd_text": f"{slot_data.get('camera_name', '')} ({slot_data.get('site_name', '')})",
                                    "url": try_encode_rtsp_password(
                                        slot_data.get("rtsp_url", "")
                                    ),
                                    "use_tcp": slot_data.get("use_tcp", False),
                                }
                                slot_sources.append(source_entry)
                            except Exception as e:
                                logger.error(f"Error creating slot source entry: {e}")
                                # On error, add empty with position info to maintain alignment
                                slot_sources.append(
                                    _create_empty_source(
                                        view_n=view_n,
                                        view_id=view_id,
                                        view_name=view_name,
                                        pos_x=row_num,
                                        pos_y=col_num,
                                    )
                                )
                        else:
                            # View doesn't have camera for this slot - add empty with position info for alignment
                            slot_sources.append(
                                _create_empty_source(
                                    view_n=view_n,
                                    view_id=view_id,
                                    view_name=view_name,
                                    pos_x=row_num,
                                    pos_y=col_num,
                                )
                            )

                    # Add slot to source_groups
                    screen_config["source_groups"].append(slot_sources)

                config["screens"].append(screen_config)

    return config


def _get_site_color(site_id: str, db: Session) -> str:
    """
    Get OSD color for a site from its category.

    Args:
        site_id: Site identifier (the physical place)
        db: Database session

    Returns:
        Hex color string (e.g., "0xFFFFFFFF")
    """
    try:
        if not site_id:
            return "0xFFFFFFFF"  # Default white

        # Query site categories through the mapping table
        from app.models.category import SiteCategoryMapping

        mapping = (
            db.query(SiteCategoryMapping)
            .filter(SiteCategoryMapping.site_id == site_id)
            .first()
        )

        if mapping:
            category = (
                db.query(SiteCategory)
                .filter(SiteCategory.id == mapping.category_id)
                .first()
            )

            if category:
                # Format as 0xAARRGGBB (ARGB: Alpha, Red, Green, Blue)
                return f"0x{category.color:08X}"

        return "0xFFFFFFFF"  # Default white

    except Exception as e:
        logger.error(f"Error getting site color for site {site_id}: {e}")
        return "0xFFFFFFFF"  # Default white


def _get_location_uris(site_id: str, db: Session) -> List[Dict[str, str]]:
    """
    Get all camera URLs for a site from the site_cameras_layout table.

    Args:
        site_id: Site identifier (the physical place)
        db: Database session

    Returns:
        List of dicts with 'url' and 'osd_text' (camera name) keys
    """
    location_uris = []

    try:
        if not site_id:
            logger.warning("_get_location_uris called with empty site_id")
            return location_uris

        logger.debug(f"Getting LocationUris for site_id: {site_id}")

        # Get all site_cameras_layout entries for this site.
        #
        # ORDER BY is required, not cosmetic: without it Postgres returns heap
        # order, which is stable only until something rewrites the rows. Any
        # UPDATE (such as a migration re-pointing these rows) silently reshuffles
        # the LocationUris sequence an operator sees. Grid position is the
        # meaningful order for a camera wall, and because auto-populate inserts
        # slots ordered by device name then camera name, it also reproduces the
        # historical ordering for auto-populated layouts.
        site_layouts = (
            db.query(SiteCamerasLayout)
            .filter(SiteCamerasLayout.site_id == site_id)
            .order_by(
                SiteCamerasLayout.slot_row,
                SiteCamerasLayout.slot_col,
            )
            .all()
        )

        logger.debug(
            f"Found {len(site_layouts)} site_cameras_layout entries "
            f"for site {site_id}"
        )

        # Extract camera URLs and names
        for site_layout in site_layouts:
            try:
                camera = (
                    db.query(Camera).filter(Camera.id == site_layout.camera_id).first()
                )

                if camera and camera.rtsp_url:
                    # URL-encode the password in RTSP URL
                    encoded_url = try_encode_rtsp_password(camera.rtsp_url)
                    location_uris.append(
                        {
                            "url": encoded_url,
                            "osd_text": camera.name if camera.name else "",
                        }
                    )
                    logger.debug(
                        f"Added camera {camera.id} URL to LocationUris "
                        f"for site {site_id}"
                    )
                else:
                    logger.warning(
                        f"Camera {site_layout.camera_id} not found or has no "
                        f"RTSP URL for site {site_id}"
                    )

            except Exception as e:
                logger.error(f"Error getting camera for LocationUris: {e}")
                continue

        logger.info(f"Total LocationUris for site {site_id}: {len(location_uris)}")

    except Exception as e:
        logger.error(f"Error getting LocationUris for site {site_id}: {e}")

    return location_uris


def _create_empty_source(
    view_n: int = 0,
    view_id: str = "",
    view_name: str = "",
    pos_x: int = 0,
    pos_y: int = 0,
) -> Dict[str, Any]:
    """
    Create an empty source entry for unpopulated slots.

    Args:
        view_n: View sequence number (integer)
        view_id: View UUID
        view_name: View name
        pos_x: Row position in grid (0-indexed)
        pos_y: Column position in grid (0-indexed)

    Returns:
        Empty source dict with all fields including position metadata
    """
    # FROZEN wire format: "site_id"/"site_name" are Device-scoped keys that PC
    # clients depend on. Keep the names and the empty-string defaults as-is.
    return {
        "LocationUris": [],
        "id": "",
        "camera_id": "",
        "camera_name": "",
        "site_id": "",
        "site_name": "",
        "view_n": view_n,  # Integer
        "view_id": view_id,
        "view_name": view_name,
        "pos_x": pos_x,
        "pos_y": pos_y,
        "osd_color": "0xFFFFFFFF",
        "osd_text": "",
        "url": "",
        "use_tcp": False,
    }
