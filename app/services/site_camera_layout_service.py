"""
Service for managing site camera layout auto-population.

Camera layouts hang off a **Site** (the physical place) via the ``site_id``
column as of migration 010. A site's grid may draw cameras from any device
belonging to that site — cameras themselves still belong to a device.
"""

import logging
from typing import Tuple, List, Dict, Any
from sqlalchemy.orm import Session

from app.models.site import Site
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


def _site_cameras(site_id: str, db: Session, limit: int = None) -> List[Camera]:
    """
    Cameras belonging to every device at a site, ordered by device name then
    camera name.

    A site may hold more than one recorder; the grid draws from all of them.
    The ordering keeps a single-device site's output identical to the previous
    device-scoped behaviour.
    """
    query = db.query(Camera).join(
        Device, Camera.device_id == Device.id
    ).filter(
        Device.site_id == site_id
    ).order_by(Device.name, Camera.name)

    if limit is not None:
        query = query.limit(limit)

    return query.all()


def auto_populate_site_cameras(
    site_id: str,
    db: Session,
    max_cameras: int = 16
) -> Dict[str, Any]:
    """
    Auto-populate the camera layout for a single site.

    Creates or updates:
    - SiteCamerasLayoutConfig with optimal grid dimensions
    - SiteCamerasLayout entries for each camera

    Cameras are pulled from **every device** belonging to the site, ordered by
    device name then camera name.

    Args:
        site_id: Site identifier
        db: Database session
        max_cameras: Maximum cameras to include (default 16)

    Returns:
        Dict with results including camera count, grid size, etc.

    Raises:
        ValueError: If site not found or has no cameras
    """
    # Get site
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise ValueError(f"Site {site_id} not found")

    # Get cameras across all of the site's devices
    cameras = _site_cameras(site_id, db, limit=max_cameras)

    if not cameras:
        raise ValueError(f"Site {site_id} has no cameras")

    camera_count = len(cameras)

    # Calculate grid dimensions
    rows, cols = calculate_grid_dimensions(camera_count)

    logger.info(
        f"Auto-populating site {site_id} ({site.name}): "
        f"{camera_count} cameras → {rows}×{cols} grid"
    )

    # Delete existing configuration and layouts (transactional)
    try:
        # Delete existing layout entries
        db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.site_id == site_id
        ).delete()

        # Delete existing config
        db.query(SiteCamerasLayoutConfig).filter(
            SiteCamerasLayoutConfig.site_id == site_id
        ).delete()

        # Create new config
        config = SiteCamerasLayoutConfig(
            site_id=site_id,
            site_name=site.name,
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
                        site_id=site_id,
                        site_name=site.name,
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
            f"for site {site_id} in {rows}×{cols} grid"
        )

        return {
            "site_id": site_id,
            "site_name": site.name,
            "camera_count": camera_count,
            "grid_size": f"{rows}×{cols}",
            "cameras_populated": cameras_populated,
            "message": f"Successfully populated {cameras_populated} cameras in {rows}×{cols} grid"
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Error populating site cameras for {site_id}: {e}")
        raise


def auto_populate_all_sites(db: Session) -> Dict[str, Any]:
    """
    Auto-populate camera layouts for all sites that have cameras.

    Args:
        db: Database session

    Returns:
        Dict with summary of results
    """
    # Get all sites
    sites = db.query(Site).all()

    results = []
    errors = []
    sites_processed = 0
    sites_skipped = 0
    total_cameras = 0

    for site in sites:
        try:
            # Check if the site has cameras on any of its devices
            camera_count = db.query(Camera).join(
                Device, Camera.device_id == Device.id
            ).filter(
                Device.site_id == site.id
            ).count()

            if camera_count == 0:
                logger.info(f"Skipping site {site.id} ({site.name}): no cameras")
                sites_skipped += 1
                continue

            # Auto-populate this site
            result = auto_populate_site_cameras(site.id, db)
            results.append(result)
            sites_processed += 1
            total_cameras += result["cameras_populated"]

            logger.info(
                f"Processed site {site.id} ({site.name}): "
                f"{result['cameras_populated']} cameras"
            )

        except Exception as e:
            logger.error(f"Error processing site {site.id}: {e}")
            errors.append({
                "site_id": site.id,
                "site_name": site.name,
                "error": str(e)
            })
            sites_skipped += 1

    return {
        "total_sites": len(sites),
        "sites_processed": sites_processed,
        "sites_skipped": sites_skipped,
        "total_cameras_populated": total_cameras,
        "results": results,
        "errors": errors
    }


# Manual layout management functions

def get_site_camera_layout(site_id: str, db: Session) -> Dict[str, Any]:
    """
    Get the camera layout configuration for a site.

    Args:
        site_id: Site identifier
        db: Database session

    Returns:
        Dict with layout configuration and camera slots

    Raises:
        ValueError: If site or layout not found
    """
    # Get site
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise ValueError(f"Site {site_id} not found")

    # Get layout config
    config = db.query(SiteCamerasLayoutConfig).filter(
        SiteCamerasLayoutConfig.site_id == site_id
    ).first()

    if not config:
        raise ValueError(f"No layout configuration found for site {site_id}")

    # Get layout slots with camera names
    layout_slots = db.query(
        SiteCamerasLayout.slot_row,
        SiteCamerasLayout.slot_col,
        SiteCamerasLayout.camera_id,
        Camera.name.label('camera_name')
    ).join(
        Camera, Camera.id == SiteCamerasLayout.camera_id
    ).filter(
        SiteCamerasLayout.site_id == site_id
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
        "site_id": site_id,
        "site_name": config.site_name,
        "n_rows": config.n_rows,
        "n_cols": config.n_cols,
        "total_slots": config.n_rows * config.n_cols,
        "cameras_populated": len(cameras),
        "cameras": cameras,
        "created_at": config.created_at,
        "updated_at": config.updated_at
    }


def save_site_camera_layout(
    site_id: str,
    n_rows: int,
    n_cols: int,
    camera_slots: List[Dict[str, Any]],
    db: Session
) -> Dict[str, Any]:
    """
    Manually save or update the camera layout configuration for a site.

    A slot may reference a camera on **any** device belonging to the site; a
    camera on a device at a different site is rejected.

    Args:
        site_id: Site identifier
        n_rows: Number of rows in grid (1-4)
        n_cols: Number of columns in grid (1-4)
        camera_slots: List of camera slot assignments
        db: Database session

    Returns:
        Dict with summary of saved layout

    Raises:
        ValueError: If the site is not found, a camera is not available at the
            site, or validation fails
    """
    # Get site
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise ValueError(f"Site {site_id} not found")

    # Validate all camera IDs exist and are available at this site
    if camera_slots:
        camera_ids = [slot['camera_id'] for slot in camera_slots]

        # One query resolves both existence and the camera's owning site
        rows = db.query(Camera.id, Device.site_id).join(
            Device, Camera.device_id == Device.id
        ).filter(
            Camera.id.in_(camera_ids)
        ).all()
        camera_site = {camera_id: owning_site for camera_id, owning_site in rows}

        for camera_id in camera_ids:
            if camera_id not in camera_site:
                raise ValueError(f"Camera {camera_id} not found")

            if camera_site[camera_id] != site_id:
                raise ValueError(
                    f"Camera {camera_id} is not available at site "
                    f"'{site.name}' and cannot be placed in its layout"
                )

    logger.info(
        f"Saving manual layout for site {site_id} ({site.name}): "
        f"{n_rows}×{n_cols} grid with {len(camera_slots)} cameras"
    )

    try:
        # Delete existing layout entries
        db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.site_id == site_id
        ).delete()

        # Delete existing config
        db.query(SiteCamerasLayoutConfig).filter(
            SiteCamerasLayoutConfig.site_id == site_id
        ).delete()

        # Create new config
        config = SiteCamerasLayoutConfig(
            site_id=site_id,
            site_name=site.name,
            n_rows=n_rows,
            n_cols=n_cols
        )
        db.add(config)

        # Create new layout entries
        for slot in camera_slots:
            layout_entry = SiteCamerasLayout(
                site_id=site_id,
                site_name=site.name,
                slot_row=slot['slot_row'],
                slot_col=slot['slot_col'],
                camera_id=slot['camera_id']
            )
            db.add(layout_entry)

        # Commit transaction
        db.commit()

        logger.info(
            f"Successfully saved layout for site {site_id}: "
            f"{len(camera_slots)} cameras in {n_rows}×{n_cols} grid"
        )

        return {
            "site_id": site_id,
            "site_name": site.name,
            "n_rows": n_rows,
            "n_cols": n_cols,
            "total_slots": n_rows * n_cols,
            "cameras_populated": len(camera_slots),
            "message": "Camera layout saved successfully"
        }

    except Exception as e:
        db.rollback()
        logger.error(f"Error saving layout for site {site_id}: {e}")
        raise


def delete_site_camera_layout(site_id: str, db: Session) -> None:
    """
    Delete the camera layout configuration for a site.

    Args:
        site_id: Site identifier
        db: Database session

    Raises:
        ValueError: If site not found or no layout exists
    """
    # Check if site exists
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise ValueError(f"Site {site_id} not found")

    # Check if layout exists
    config = db.query(SiteCamerasLayoutConfig).filter(
        SiteCamerasLayoutConfig.site_id == site_id
    ).first()

    if not config:
        raise ValueError(f"No layout configuration found for site {site_id}")

    logger.info(f"Deleting layout for site {site_id} ({site.name})")

    try:
        # site_cameras_layout has no FK to the config table (both key off
        # sites.id) and the config→slots relationship is viewonly, so nothing
        # cascades: the slots must be removed explicitly, as save_ does.
        db.query(SiteCamerasLayout).filter(
            SiteCamerasLayout.site_id == site_id
        ).delete()
        db.delete(config)
        db.commit()

        logger.info(f"Successfully deleted layout for site {site_id}")

    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting layout for site {site_id}: {e}")
        raise
