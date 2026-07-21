"""
Service for managing device camera layout auto-population.

Note: the ``Site*`` model/table names (``SiteCamerasLayoutConfig``,
``SiteCamerasLayout``) are frozen, but as of migration 008 camera layouts hang
off a **Device** (NVR/DVR) via the ``device_id`` column.
"""

import logging
from typing import Tuple, List, Dict, Any
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.camera import Camera
from app.models.site_camera_layout import SiteCamerasLayoutConfig, SiteCamerasLayout

logger = logging.getLogger(__name__)


def calculate_grid_dimensions(camera_count: int) -> Tuple[int, int]:
    """
    Calculate optimal grid dimensions based on camera count.

    Returns (rows, cols) with preference for wider layouts.
    Maximum grid size is 4×4 (16 cameras).

    Args:
        camera_count: Number of cameras to arrange

    Returns:
        Tuple of (rows, cols)
    """
    if camera_count <= 0:
        return (1, 1)

    # Limit to maximum 16 cameras (4×4 grid)
    count = min(camera_count, 16)

    if count == 1:
        return (1, 1)
    elif count == 2:
        return (1, 2)  # Prefer horizontal layout
    elif count <= 4:
        return (2, 2)
    elif count <= 6:
        return (2, 3)
    elif count <= 9:
        return (3, 3)
    elif count <= 12:
        return (3, 4)
    else:  # 13-16
        return (4, 4)


def auto_populate_device_cameras(
    device_id: str,
    db: Session,
    max_cameras: int = 16
) -> Dict[str, Any]:
    """
    Auto-populate device camera layout for a single device.

    Creates or updates:
    - SiteCamerasLayoutConfig with optimal grid dimensions
    - SiteCamerasLayout entries for each camera

    Args:
        device_id: Device identifier
        db: Database session
        max_cameras: Maximum cameras to include (default 16)

    Returns:
        Dict with results including camera count, grid size, etc.

    Raises:
        ValueError: If device not found or has no cameras
    """
    # Get device
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    # Get cameras for device (ordered by name)
    cameras = db.query(Camera).filter(
        Camera.device_id == device_id
    ).order_by(Camera.name).limit(max_cameras).all()

    if not cameras:
        raise ValueError(f"Device {device_id} has no cameras")

    camera_count = len(cameras)

    # Calculate grid dimensions
    rows, cols = calculate_grid_dimensions(camera_count)

    logger.info(
        f"Auto-populating device {device_id} ({device.name}): "
        f"{camera_count} cameras → {rows}×{cols} grid"
    )

    # Delete existing configuration and layouts (transactional)
    try:
        # Delete existing layout entries
        db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.device_id == device_id
        ).delete()

        # Delete existing config
        db.query(SiteCamerasLayoutConfig).filter(
            SiteCamerasLayoutConfig.device_id == device_id
        ).delete()

        # Create new config
        config = SiteCamerasLayoutConfig(
            device_id=device_id,
            device_name=device.name,
            n_rows=rows,
            n_cols=cols
        )
        db.add(config)

        # Create layout entries in row-major order
        camera_idx = 0
        cameras_populated = 0

        for row in range(1, rows + 1):
            for col in range(1, cols + 1):
                if camera_idx < len(cameras):
                    camera = cameras[camera_idx]

                    layout_entry = SiteCamerasLayout(
                        device_id=device_id,
                        device_name=device.name,
                        slot_row=row,
                        slot_col=col,
                        camera_id=camera.id
                    )
                    db.add(layout_entry)

                    cameras_populated += 1
                    camera_idx += 1

        # Commit transaction
        db.commit()

        logger.info(
            f"Successfully populated {cameras_populated} cameras "
            f"for device {device_id} in {rows}×{cols} grid"
        )

        return {
            "device_id": device_id,
            "device_name": device.name,
            "camera_count": camera_count,
            "grid_size": f"{rows}×{cols}",
            "cameras_populated": cameras_populated,
            "message": f"Successfully populated {cameras_populated} cameras in {rows}×{cols} grid"
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Error populating device cameras for {device_id}: {e}")
        raise


def auto_populate_all_devices(db: Session) -> Dict[str, Any]:
    """
    Auto-populate device camera layouts for all devices that have cameras.

    Args:
        db: Database session

    Returns:
        Dict with summary of results
    """
    # Get all devices
    devices = db.query(Device).all()

    results = []
    errors = []
    devices_processed = 0
    devices_skipped = 0
    total_cameras = 0

    for device in devices:
        try:
            # Check if device has cameras
            camera_count = db.query(Camera).filter(
                Camera.device_id == device.id
            ).count()

            if camera_count == 0:
                logger.info(f"Skipping device {device.id} ({device.name}): no cameras")
                devices_skipped += 1
                continue

            # Auto-populate this device
            result = auto_populate_device_cameras(device.id, db)
            results.append(result)
            devices_processed += 1
            total_cameras += result["cameras_populated"]

            logger.info(
                f"Processed device {device.id} ({device.name}): "
                f"{result['cameras_populated']} cameras"
            )

        except Exception as e:
            logger.error(f"Error processing device {device.id}: {e}")
            errors.append({
                "device_id": device.id,
                "device_name": device.name,
                "error": str(e)
            })
            devices_skipped += 1

    return {
        "total_devices": len(devices),
        "devices_processed": devices_processed,
        "devices_skipped": devices_skipped,
        "total_cameras_populated": total_cameras,
        "results": results,
        "errors": errors
    }


# Manual layout management functions

def get_device_camera_layout(device_id: str, db: Session) -> Dict[str, Any]:
    """
    Get the camera layout configuration for a device.

    Args:
        device_id: Device identifier
        db: Database session

    Returns:
        Dict with layout configuration and camera slots

    Raises:
        ValueError: If device or layout not found
    """
    # Get device
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    # Get layout config
    config = db.query(SiteCamerasLayoutConfig).filter(
        SiteCamerasLayoutConfig.device_id == device_id
    ).first()

    if not config:
        raise ValueError(f"No layout configuration found for device {device_id}")

    # Get layout slots with camera names
    layout_slots = db.query(
        SiteCamerasLayout.slot_row,
        SiteCamerasLayout.slot_col,
        SiteCamerasLayout.camera_id,
        Camera.name.label('camera_name')
    ).join(
        Camera, Camera.id == SiteCamerasLayout.camera_id
    ).filter(
        SiteCamerasLayout.device_id == device_id
    ).order_by(
        SiteCamerasLayout.slot_row,
        SiteCamerasLayout.slot_col
    ).all()

    # Format camera slots
    cameras = [
        {
            "slot_row": slot.slot_row,
            "slot_col": slot.slot_col,
            "camera_id": slot.camera_id,
            "camera_name": slot.camera_name
        }
        for slot in layout_slots
    ]

    return {
        "device_id": device_id,
        "device_name": config.device_name,
        "n_rows": config.n_rows,
        "n_cols": config.n_cols,
        "total_slots": config.n_rows * config.n_cols,
        "cameras_populated": len(cameras),
        "cameras": cameras,
        "created_at": config.created_at,
        "updated_at": config.updated_at
    }


def save_device_camera_layout(
    device_id: str,
    n_rows: int,
    n_cols: int,
    camera_slots: List[Dict[str, Any]],
    db: Session
) -> Dict[str, Any]:
    """
    Manually save or update camera layout configuration for a device.

    Args:
        device_id: Device identifier
        n_rows: Number of rows in grid (1-4)
        n_cols: Number of columns in grid (1-4)
        camera_slots: List of camera slot assignments
        db: Database session

    Returns:
        Dict with summary of saved layout

    Raises:
        ValueError: If device not found, cameras don't belong to device, or validation fails
    """
    # Get device
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    # Validate all camera IDs exist and belong to this device
    if camera_slots:
        camera_ids = [slot['camera_id'] for slot in camera_slots]
        cameras = db.query(Camera).filter(
            Camera.id.in_(camera_ids)
        ).all()

        # Check all cameras exist
        found_camera_ids = {cam.id for cam in cameras}
        for camera_id in camera_ids:
            if camera_id not in found_camera_ids:
                raise ValueError(f"Camera {camera_id} not found")

        # Check all cameras belong to this device
        for camera in cameras:
            if camera.device_id != device_id:
                raise ValueError(
                    f"Camera {camera.id} does not belong to device {device_id}"
                )

    logger.info(
        f"Saving manual layout for device {device_id} ({device.name}): "
        f"{n_rows}×{n_cols} grid with {len(camera_slots)} cameras"
    )

    try:
        # Delete existing layout entries
        db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.device_id == device_id
        ).delete()

        # Delete existing config
        db.query(SiteCamerasLayoutConfig).filter(
            SiteCamerasLayoutConfig.device_id == device_id
        ).delete()

        # Create new config
        config = SiteCamerasLayoutConfig(
            device_id=device_id,
            device_name=device.name,
            n_rows=n_rows,
            n_cols=n_cols
        )
        db.add(config)

        # Create new layout entries
        for slot in camera_slots:
            layout_entry = SiteCamerasLayout(
                device_id=device_id,
                device_name=device.name,
                slot_row=slot['slot_row'],
                slot_col=slot['slot_col'],
                camera_id=slot['camera_id']
            )
            db.add(layout_entry)

        # Commit transaction
        db.commit()

        logger.info(
            f"Successfully saved layout for device {device_id}: "
            f"{len(camera_slots)} cameras in {n_rows}×{n_cols} grid"
        )

        return {
            "device_id": device_id,
            "device_name": device.name,
            "n_rows": n_rows,
            "n_cols": n_cols,
            "total_slots": n_rows * n_cols,
            "cameras_populated": len(camera_slots),
            "message": f"Camera layout saved successfully"
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Error saving layout for device {device_id}: {e}")
        raise


def delete_device_camera_layout(device_id: str, db: Session) -> None:
    """
    Delete the camera layout configuration for a device.

    Args:
        device_id: Device identifier
        db: Database session

    Raises:
        ValueError: If device not found or no layout exists
    """
    # Check if device exists
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    # Check if layout exists
    config = db.query(SiteCamerasLayoutConfig).filter(
        SiteCamerasLayoutConfig.device_id == device_id
    ).first()

    if not config:
        raise ValueError(f"No layout configuration found for device {device_id}")

    logger.info(f"Deleting layout for device {device_id} ({device.name})")

    try:
        # Delete config (will cascade to layout slots)
        db.delete(config)
        db.commit()

        logger.info(f"Successfully deleted layout for device {device_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting layout for device {device_id}: {e}")
        raise
