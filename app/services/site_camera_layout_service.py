"""
Service for managing site camera layout auto-population.
"""

import logging
from typing import Tuple, List, Dict, Any
from sqlalchemy.orm import Session

from app.models.site import Site
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


def auto_populate_site_cameras(
    site_id: str,
    db: Session,
    max_cameras: int = 16
) -> Dict[str, Any]:
    """
    Auto-populate site camera layout for a single site.

    Creates or updates:
    - SiteCamerasLayoutConfig with optimal grid dimensions
    - SiteCamerasLayout entries for each camera

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

    # Get cameras for site (ordered by name)
    cameras = db.query(Camera).filter(
        Camera.site_id == site_id
    ).order_by(Camera.name).limit(max_cameras).all()

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
    Auto-populate site camera layouts for all sites that have cameras.

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
            # Check if site has cameras
            camera_count = db.query(Camera).filter(
                Camera.site_id == site.id
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
