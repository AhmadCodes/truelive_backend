"""
Configuration loader service.

Loads camera and site configurations from database in formats needed by other services.
"""
import logging
from typing import Dict, Any
from sqlalchemy.orm import Session

from app.models.site import Site
from app.models.camera import Camera
from app.models.pc import PC
from app.models.screen import Screen, View, ScreenMapping

logger = logging.getLogger(__name__)


def load_camera_config(db: Session) -> Dict[str, Any]:
    """
    Load all sites and cameras from database.

    Used for populating site/camera dropdowns and getting camera information.

    Args:
        db: Database session

    Returns:
        Configuration dict in format:
            {
                "sites": {
                    "site_id": {
                        "name": "Site Name",
                        "nvr_username": "username",
                        "nvr_password": "password",
                        "cameras": {
                            "camera_id": {
                                "name": "Camera Name",
                                "rtsp_url": "rtsp://..."
                            }
                        }
                    }
                }
            }
    """
    config = {"sites": {}}

    try:
        # Get all sites
        sites = db.query(Site).all()

        for site in sites:
            site_config = {
                "name": site.name,
                "nvr_username": site.nvr_username,
                "nvr_password": site.nvr_password,
                "cameras": {}
            }

            # Get all cameras for this site
            cameras = db.query(Camera).filter(Camera.site_id == site.id).all()

            for camera in cameras:
                site_config["cameras"][camera.id] = {
                    "name": camera.name,
                    "rtsp_url": camera.rtsp_url,
                    "main_stream_url": camera.main_stream_url
                }

            config["sites"][site.id] = site_config

        logger.info(f"Loaded camera config: {len(config['sites'])} sites")
        return config

    except Exception as e:
        logger.error(f"Error loading camera config: {e}")
        return {"sites": {}}


def load_pc_config(pc_id: str, db: Session) -> Dict[str, Any]:
    """
    Load complete PC configuration including all screens, views, and camera mappings.

    This is the configuration structure that feeds into generate_config().

    Args:
        pc_id: PC identifier
        db: Database session

    Returns:
        Configuration dict in format:
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
    """
    config = {
        "pcs": {},
        "mappings": {
            "screen_to_cameras": {}
        }
    }

    try:
        # Get PC
        pc = db.query(PC).filter(PC.id == pc_id).first()

        if not pc:
            logger.warning(f"PC {pc_id} not found")
            return config

        # Build PC config
        pc_config = {
            "name": pc.name,
            "screens": {}
        }

        # Get all screens for this PC
        screens = db.query(Screen).filter(Screen.pc_id == pc_id).all()

        for screen in screens:
            screen_config = {
                "name": screen.name,
                "layout": {
                    "rows": screen.rows,
                    "columns": screen.columns
                },
                "switching_interval": screen.switching_interval
            }

            pc_config["screens"][screen.id] = screen_config

        config["pcs"][pc_id] = pc_config

        # Build mappings
        pc_mappings = {}

        for screen in screens:
            screen_mappings = {}

            # Get all views for this screen
            views = db.query(View).filter(View.screen_id == screen.id).all()

            for view in views:
                view_mappings = {}

                # Get all slot mappings for this view
                mappings = db.query(ScreenMapping).filter(
                    ScreenMapping.view_id == view.id
                ).all()

                for mapping in mappings:
                    slot_key = f"slot_{mapping.slot_row}_{mapping.slot_col}"

                    # Get camera and site info
                    camera = db.query(Camera).filter(
                        Camera.id == mapping.camera_id
                    ).first()

                    site = db.query(Site).filter(
                        Site.id == mapping.site_id
                    ).first()

                    if camera and site:
                        view_mappings[slot_key] = {
                            "slot_row": mapping.slot_row,
                            "slot_col": mapping.slot_col,
                            "site_id": site.id,
                            "camera_id": camera.id,
                            "site_name": site.name,
                            "camera_name": camera.name,
                            "rtsp_url": camera.rtsp_url,
                            "use_tcp": False,
                            "playing_state": mapping.playing_state
                        }

                screen_mappings[view.name] = view_mappings

            pc_mappings[screen.id] = screen_mappings

        config["mappings"]["screen_to_cameras"][pc_id] = pc_mappings

        logger.info(f"Loaded PC config for {pc_id}: {len(screens)} screens")
        return config

    except Exception as e:
        logger.error(f"Error loading PC config for {pc_id}: {e}")
        return config


def load_site_config(db: Session) -> Dict[str, Any]:
    """
    Load complete site configuration for all PCs.

    This aggregates all PC configurations.

    Args:
        db: Database session

    Returns:
        Complete configuration dict with all PCs
    """
    config = {
        "pcs": {},
        "mappings": {
            "screen_to_cameras": {}
        }
    }

    try:
        # Get all PCs
        pcs = db.query(PC).all()

        for pc in pcs:
            pc_config = load_pc_config(pc.id, db)

            # Merge PC configs
            if pc.id in pc_config.get("pcs", {}):
                config["pcs"][pc.id] = pc_config["pcs"][pc.id]

            # Merge mappings
            if pc.id in pc_config.get("mappings", {}).get("screen_to_cameras", {}):
                config["mappings"]["screen_to_cameras"][pc.id] = \
                    pc_config["mappings"]["screen_to_cameras"][pc.id]

        logger.info(f"Loaded site config: {len(config['pcs'])} PCs")
        return config

    except Exception as e:
        logger.error(f"Error loading site config: {e}")
        return config
