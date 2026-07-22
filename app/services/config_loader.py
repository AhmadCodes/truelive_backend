"""
Configuration loader service.

Loads camera and device configurations from database in formats needed by other services.
"""
import logging
from typing import Dict, Any
from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.camera import Camera
from app.models.pc import PC
from app.models.screen import Screen
from app.models.screen_mapping import ScreenMapping
from app.models.view import View

logger = logging.getLogger(__name__)


def load_camera_config(db: Session) -> Dict[str, Any]:
    """
    Load all devices and cameras from database.

    Used for populating device/camera dropdowns and getting camera information.

    Note: the top-level "sites" key and the "site_id" map keys are legacy
    names that carry Device ids — kept as-is for consumer compatibility.

    Args:
        db: Database session

    Returns:
        Configuration dict in format:
            {
                "sites": {
                    "site_id": {
                        "name": "Device Name",
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
        # Get all devices
        devices = db.query(Device).all()

        for device in devices:
            device_config = {
                "name": device.name,
                "nvr_username": device.nvr_username,
                "nvr_password": device.nvr_password,
                "cameras": {},
            }

            # Get all cameras for this device
            cameras = db.query(Camera).filter(Camera.device_id == device.id).all()

            for camera in cameras:
                device_config["cameras"][camera.id] = {
                    "name": camera.name,
                    "rtsp_url": camera.rtsp_url,
                    "main_stream_url": camera.main_stream_url,
                }

            # Legacy "sites" key: carries Device ids.
            config["sites"][device.id] = device_config

        logger.info(f"Loaded camera config: {len(config['sites'])} devices")
        return config

    except Exception as e:
        logger.error(f"Error loading camera config: {e}")
        return {"sites": {}}


def load_pc_config(pc_id: str, db: Session) -> Dict[str, Any]:
    """
    Load complete PC configuration including all screens, views, and camera mappings.

    This is the configuration structure that feeds into generate_config().

    WIRE FORMAT IS FROZEN: the "site_id"/"site_name" slot keys below carry the
    **Device** id/name (the NVR/DVR), not the new parent Site. Renaming them or
    re-pointing them at the parent would change the JSON delivered to live PC
    clients.

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
    config = {"pcs": {}, "mappings": {"screen_to_cameras": {}}}

    try:
        # Get PC
        pc = db.query(PC).filter(PC.id == pc_id).first()

        if not pc:
            logger.warning(f"PC {pc_id} not found")
            return config

        # Re-root the screen walk from the PC to the PC's screen layout.
        # A PC with no assigned layout deploys nothing (AC-13 no-op).
        layout_id = pc.screen_layout_id
        if layout_id is None:
            logger.info(f"PC {pc_id} has no screen layout assigned; empty config")
            return config

        # Build PC config
        pc_config = {"name": pc.name, "screen_layout_id": layout_id, "screens": {}}

        # Get all screens for this PC's layout, ordered deterministically by
        # name (screens are named "Monitor 1".."Monitor N") with id as a stable
        # tiebreak. Without an explicit order the row (heap) order is
        # non-deterministic and shuffles on any row rewrite (e.g. a migration).
        screens = (
            db.query(Screen)
            .filter(Screen.screen_layout_id == layout_id)
            .order_by(Screen.name, Screen.id)
            .all()
        )

        for screen in screens:
            screen_config = {
                "name": screen.name,
                "layout": {"rows": screen.rows, "columns": screen.columns},
                "switching_interval": screen.switching_interval,
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
                mappings = (
                    db.query(ScreenMapping)
                    .filter(ScreenMapping.view_id == view.id)
                    .all()
                )

                for mapping in mappings:
                    slot_key = f"slot_{mapping.slot_row}_{mapping.slot_col}"

                    # Get camera info
                    camera = (
                        db.query(Camera).filter(Camera.id == mapping.camera_id).first()
                    )

                    if camera:
                        # Get device from camera's device_id
                        # (mapping.device_id may be NULL)
                        device = (
                            db.query(Device)
                            .filter(Device.id == camera.device_id)
                            .first()
                        )

                        if device:
                            # Cascade: camera override wins, else inherit device default.
                            effective_use_tcp = (
                                camera.use_tcp
                                if camera.use_tcp is not None
                                else device.use_tcp
                            )
                            # FROZEN wire format: "site_id"/"site_name" are emitted
                            # into the intermediate structure consumed by
                            # generate_config() and carry the Device id/name.
                            # Do NOT rename or re-point at the parent Site.
                            view_mappings[slot_key] = {
                                "slot_row": mapping.slot_row,
                                "slot_col": mapping.slot_col,
                                "site_id": device.id,
                                "camera_id": camera.id,
                                "site_name": device.name,
                                "camera_name": camera.name,
                                "rtsp_url": camera.rtsp_url,
                                "use_tcp": effective_use_tcp,
                            }

                # Key by view.id (UUID, guaranteed unique) instead of view.name to
                # tolerate duplicate view names within a screen. Without this, two
                # views sharing a name would collide in this dict and the second
                # would silently overwrite the first's mappings.
                screen_mappings[str(view.id)] = view_mappings

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
    config = {"pcs": {}, "mappings": {"screen_to_cameras": {}}}

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
                config["mappings"]["screen_to_cameras"][pc.id] = pc_config["mappings"][
                    "screen_to_cameras"
                ][pc.id]

        logger.info(f"Loaded site config: {len(config['pcs'])} PCs")
        return config

    except Exception as e:
        logger.error(f"Error loading site config: {e}")
        return config
