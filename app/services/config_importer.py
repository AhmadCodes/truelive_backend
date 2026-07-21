"""
Config importer service for importing device configuration JSON files.

This is the reverse of config_generator - it takes a device JSON config
and imports it into the database as screens, views, and mappings.
"""

import math
import uuid
import logging
from typing import Any
from dataclasses import dataclass, field
from sqlalchemy.orm import Session

from app.models.pc import PC
from app.models.screen import Screen
from app.models.view import View
from app.models.screen_mapping import ScreenMapping
from app.models.camera import Camera
from app.models.device import Device

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Result of config import operation."""
    success: bool
    pc_id: str
    screens_created: int = 0
    views_created: int = 0
    mappings_created: int = 0
    cameras_skipped: int = 0
    devices_skipped: int = 0
    message: str = ""
    errors: list = field(default_factory=list)


def import_config_for_pc(db: Session, pc_id: str, config: dict) -> ImportResult:
    """
    Import device config JSON and create/update screens, views, and mappings.

    Args:
        db: Database session
        pc_id: ID of the PC to import config for
        config: Device configuration JSON (same format as config_generator output)

    Returns:
        ImportResult with statistics and status
    """
    result = ImportResult(success=False, pc_id=pc_id)

    # Validate PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        result.message = f"PC with id '{pc_id}' not found"
        return result

    # Validate config structure
    if 'screens' not in config:
        result.message = "Config must contain 'screens' array"
        return result

    screens_config = config.get('screens', [])
    if not screens_config:
        result.message = "Config 'screens' array is empty"
        return result

    # Get existing cameras and devices for validation
    existing_cameras = {c.id for c in db.query(Camera.id).all()}
    existing_devices = {d.id for d in db.query(Device.id).all()}

    # Track skipped items
    skipped_camera_ids = set()
    skipped_device_ids = set()

    try:
        # Clear existing configuration for this PC
        _clear_pc_config(db, pc_id)

        # Process each screen
        for screen_idx, screen_config in enumerate(screens_config):
            screen_result = _import_screen(
                db=db,
                pc_id=pc_id,
                screen_config=screen_config,
                screen_idx=screen_idx,
                existing_cameras=existing_cameras,
                existing_devices=existing_devices,
                skipped_camera_ids=skipped_camera_ids,
                skipped_device_ids=skipped_device_ids
            )

            result.screens_created += screen_result['screens_created']
            result.views_created += screen_result['views_created']
            result.mappings_created += screen_result['mappings_created']

        result.cameras_skipped = len(skipped_camera_ids)
        result.devices_skipped = len(skipped_device_ids)

        db.commit()

        result.success = True
        result.message = (
            f"Successfully imported config: "
            f"{result.screens_created} screens, "
            f"{result.views_created} views, "
            f"{result.mappings_created} mappings"
        )

        if skipped_camera_ids:
            result.message += f" (skipped {len(skipped_camera_ids)} missing cameras)"
        if skipped_device_ids:
            result.message += f" (skipped {len(skipped_device_ids)} missing devices)"

        logger.info(f"Config import for PC {pc_id}: {result.message}")

    except Exception as e:
        db.rollback()
        result.message = f"Import failed: {str(e)}"
        result.errors.append(str(e))
        logger.error(f"Config import failed for PC {pc_id}: {e}")

    return result


def _clear_pc_config(db: Session, pc_id: str) -> None:
    """Clear existing screens, views, and mappings for a PC."""
    # Delete mappings first (they reference views and screens)
    db.query(ScreenMapping).filter(ScreenMapping.pc_id == pc_id).delete()

    # Get screens for this PC
    screens = db.query(Screen).filter(Screen.pc_id == pc_id).all()

    # Delete views (they cascade from screens, but let's be explicit)
    for screen in screens:
        db.query(View).filter(View.screen_id == screen.id).delete()

    # Delete screens
    db.query(Screen).filter(Screen.pc_id == pc_id).delete()

    db.flush()
    logger.debug(f"Cleared existing config for PC {pc_id}")


def _import_screen(
    db: Session,
    pc_id: str,
    screen_config: dict,
    screen_idx: int,
    existing_cameras: set,
    existing_devices: set,
    skipped_camera_ids: set,
    skipped_device_ids: set
) -> dict:
    """Import a single screen configuration."""
    result = {
        'screens_created': 0,
        'views_created': 0,
        'mappings_created': 0
    }

    source_groups = screen_config.get('source_groups', [])
    if not source_groups:
        logger.warning(f"Screen {screen_idx} has no source_groups, skipping")
        return result

    # Calculate grid dimensions from source_groups count
    num_tiles = len(source_groups)
    grid_size = int(math.ceil(math.sqrt(num_tiles)))

    # Clamp to valid range (1-4)
    rows = min(max(grid_size, 1), 4)
    cols = min(max(grid_size, 1), 4)

    # Extract screen properties
    screen_id = screen_config.get('id') or f"{pc_id}_screen_{uuid.uuid4()}"
    screen_name = screen_config.get('title') or f"Screen {screen_idx + 1}"
    switching_interval = screen_config.get('switchInterval', 10)

    # Ensure switching_interval is at least 1
    if switching_interval < 1:
        switching_interval = 1

    # Create screen
    screen = Screen(
        id=screen_id,
        pc_id=pc_id,
        name=screen_name,
        rows=rows,
        columns=cols,
        switching_interval=switching_interval
    )
    db.add(screen)
    db.flush()
    result['screens_created'] = 1

    # Extract unique views from source_groups
    views_info = _extract_views_from_source_groups(source_groups)

    # Create views
    created_views = {}
    for view_n, view_info in views_info.items():
        view_id = view_info.get('view_id') or f"{screen_id}_view_{view_n}"
        view_name = view_info.get('view_name') or f"View {view_n + 1}"

        view = View(
            id=view_id,
            screen_id=screen_id,
            name=view_name[:50],  # Limit to 50 chars
            layout_rows=rows,
            layout_columns=cols,
            view_number=view_n
        )
        db.add(view)
        created_views[view_n] = view
        result['views_created'] += 1

    db.flush()

    # Create mappings from source_groups
    for tile_idx, tile in enumerate(source_groups):
        # Calculate grid position (1-indexed for database)
        slot_row = (tile_idx // cols) + 1
        slot_col = (tile_idx % cols) + 1

        # Each camera in the tile belongs to a different view
        for camera_info in tile:
            view_n = camera_info.get('view_n', 0)
            camera_id = camera_info.get('camera_id')
            # 'site_id' is a FROZEN wire-format key in the device JSON being
            # imported; it carries the Device id, hence the device_id variable.
            device_id = camera_info.get('site_id')

            # Skip if camera_id is empty or missing
            if not camera_id or camera_id.strip() == '':
                continue

            # Skip if device id is empty or missing
            if not device_id or device_id.strip() == '':
                continue

            # Skip if camera doesn't exist in database
            if camera_id not in existing_cameras:
                skipped_camera_ids.add(camera_id)
                continue

            # Skip if device doesn't exist in database
            if device_id not in existing_devices:
                skipped_device_ids.add(device_id)
                continue

            # Get the view for this camera
            if view_n not in created_views:
                logger.warning(f"View {view_n} not found for camera {camera_id}, skipping")
                continue

            view = created_views[view_n]

            # Create mapping
            mapping = ScreenMapping(
                pc_id=pc_id,
                screen_id=screen_id,
                view_id=view.id,
                slot_row=slot_row,
                slot_col=slot_col,
                device_id=device_id,
                camera_id=camera_id,
                playing_state=False
            )
            db.add(mapping)
            result['mappings_created'] += 1

    db.flush()
    return result


def _extract_views_from_source_groups(source_groups: list) -> dict:
    """
    Extract unique views from source_groups.

    Returns:
        Dict mapping view_n to view info (view_id, view_name)
    """
    views = {}

    for tile in source_groups:
        for camera in tile:
            view_n = camera.get('view_n', 0)
            if view_n not in views:
                views[view_n] = {
                    'view_id': camera.get('view_id'),
                    'view_name': camera.get('view_name')
                }

    return views


@dataclass
class CopyLayoutResult:
    """Result of copy layout operation."""
    success: bool
    source_pc_id: str
    target_pc_id: str
    screens_copied: int = 0
    views_copied: int = 0
    mappings_copied: int = 0
    message: str = ""


def copy_layout_from_pc(db: Session, target_pc_id: str, source_pc_id: str) -> CopyLayoutResult:
    """
    Copy the entire screen layout from one PC to another.

    This copies all screens, views, and mappings from the source PC to the target PC.
    The target PC's existing layout is cleared before copying.

    Args:
        db: Database session
        target_pc_id: ID of the PC to copy layout TO
        source_pc_id: ID of the PC to copy layout FROM

    Returns:
        CopyLayoutResult with statistics and status
    """
    result = CopyLayoutResult(
        success=False,
        source_pc_id=source_pc_id,
        target_pc_id=target_pc_id
    )

    # Validate target PC exists
    target_pc = db.query(PC).filter(PC.id == target_pc_id).first()
    if not target_pc:
        result.message = f"Target PC with id '{target_pc_id}' not found"
        return result

    # Validate source PC exists
    source_pc = db.query(PC).filter(PC.id == source_pc_id).first()
    if not source_pc:
        result.message = f"Source PC with id '{source_pc_id}' not found"
        return result

    # Prevent copying to self
    if target_pc_id == source_pc_id:
        result.message = "Cannot copy layout to the same PC"
        return result

    # Get source screens
    source_screens = db.query(Screen).filter(Screen.pc_id == source_pc_id).all()
    if not source_screens:
        result.message = f"Source PC '{source_pc_id}' has no screens to copy"
        return result

    try:
        # Clear existing configuration for target PC
        _clear_pc_config(db, target_pc_id)

        # Map old screen/view IDs to new IDs
        screen_id_map = {}  # old_screen_id -> new_screen_id
        view_id_map = {}    # old_view_id -> new_view_id

        # Copy each screen
        for source_screen in source_screens:
            # Generate new screen ID
            new_screen_id = f"{target_pc_id}_screen_{uuid.uuid4()}"
            screen_id_map[source_screen.id] = new_screen_id

            # Create new screen
            new_screen = Screen(
                id=new_screen_id,
                pc_id=target_pc_id,
                name=source_screen.name,
                rows=source_screen.rows,
                columns=source_screen.columns,
                switching_interval=source_screen.switching_interval
            )
            db.add(new_screen)
            result.screens_copied += 1

            # Copy views for this screen
            source_views = db.query(View).filter(View.screen_id == source_screen.id).all()
            for source_view in source_views:
                # Generate new view ID
                new_view_id = f"{new_screen_id}_view_{uuid.uuid4()}"
                view_id_map[source_view.id] = new_view_id

                # Create new view
                new_view = View(
                    id=new_view_id,
                    screen_id=new_screen_id,
                    name=source_view.name,
                    layout_rows=source_view.layout_rows,
                    layout_columns=source_view.layout_columns,
                    view_number=source_view.view_number
                )
                db.add(new_view)
                result.views_copied += 1

        db.flush()

        # Copy mappings
        source_mappings = db.query(ScreenMapping).filter(
            ScreenMapping.pc_id == source_pc_id
        ).all()

        for source_mapping in source_mappings:
            # Get new IDs
            new_screen_id = screen_id_map.get(source_mapping.screen_id)
            new_view_id = view_id_map.get(source_mapping.view_id)

            if not new_screen_id or not new_view_id:
                logger.warning(
                    f"Skipping mapping - missing new IDs for screen {source_mapping.screen_id} "
                    f"or view {source_mapping.view_id}"
                )
                continue

            # Create new mapping
            new_mapping = ScreenMapping(
                pc_id=target_pc_id,
                screen_id=new_screen_id,
                view_id=new_view_id,
                slot_row=source_mapping.slot_row,
                slot_col=source_mapping.slot_col,
                device_id=source_mapping.device_id,
                camera_id=source_mapping.camera_id,
                playing_state=False
            )
            db.add(new_mapping)
            result.mappings_copied += 1

        db.commit()

        result.success = True
        result.message = (
            f"Successfully copied layout from '{source_pc_id}' to '{target_pc_id}': "
            f"{result.screens_copied} screens, "
            f"{result.views_copied} views, "
            f"{result.mappings_copied} mappings"
        )

        logger.info(result.message)

    except Exception as e:
        db.rollback()
        result.message = f"Copy layout failed: {str(e)}"
        logger.error(f"Copy layout failed from {source_pc_id} to {target_pc_id}: {e}")

    return result
