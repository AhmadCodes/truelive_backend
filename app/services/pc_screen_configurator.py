"""
Service for configuring PC screens, views, and camera mappings.

Handles creation and updates of screens, views, and screen mappings
for a PC based on provided camera list and layout specifications.
"""

import logging
from typing import List, Dict, Any, Tuple
from sqlalchemy.orm import Session

from app.models.pc import PC
from app.models.screen import Screen
from app.models.screen_layout import ScreenLayout
from app.models.view import View
from app.models.screen_mapping import ScreenMapping
from app.models.camera import Camera
from app.schemas.pc import ScreenConfigRequest
from app.services.team_enforcement import assert_cameras_in_layout_team
from app.services.actor import (
    ActorTriple,
    SYSTEM_ACTOR,
    stamp_created,
    stamp_updated,
    touch_layout,
)
from app.services import audit_service
from app.services.audit_service import ResourceType, AuditAction

logger = logging.getLogger(__name__)


def get_or_create_layout_for_pc(
    pc: PC, db: Session, actor: ActorTriple = SYSTEM_ACTOR
) -> str:
    """
    Resolve the screen layout id for a PC, auto-creating and assigning a layout
    if the PC has none.

    A PC decoupled from its screens points at a ScreenLayout via
    ``pc.screen_layout_id``. When that pointer is NULL, mint a layout named
    after the PC (id ``lay_{pc.id}``, matching the migration seed convention),
    assign it to the PC, and return its id.

    Reused by config_importer.copy_layout_from_pc for target-layout resolution.

    Args:
        pc: PC object (already loaded from the session)
        db: Database session

    Returns:
        The resolved (existing or newly created) screen layout id
    """
    if pc.screen_layout_id is not None:
        return pc.screen_layout_id

    layout_id = f"lay_{pc.id}"
    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if layout is None:
        # An auto-created layout inherits the PC's team (team_id is mandatory).
        layout = ScreenLayout(id=layout_id, name=pc.name, team_id=pc.team_id)
        stamp_created(layout, actor)
        db.add(layout)
        db.flush()
        logger.info(f"Auto-created screen layout '{layout_id}' for PC {pc.id}")

    pc.screen_layout_id = layout_id
    db.flush()

    return layout_id


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


def get_or_create_screen(
    layout_id: str,
    screen_config: ScreenConfigRequest,
    screen_index: int,
    db: Session,
    actor: ActorTriple = SYSTEM_ACTOR,
) -> Tuple[Screen, bool]:
    """
    Get existing screen by name or create new one.
    If screen exists, delete all its views and mappings.

    Args:
        layout_id: Screen layout identifier
        screen_config: Screen configuration
        screen_index: Index for generating screen ID
        db: Database session

    Returns:
        Tuple of (Screen object, is_new boolean)
    """
    # Check if screen with same name exists for this layout
    existing_screen = (
        db.query(Screen)
        .filter(Screen.screen_layout_id == layout_id, Screen.name == screen_config.name)
        .first()
    )

    if existing_screen:
        logger.info(
            f"Found existing screen '{screen_config.name}' for layout {layout_id}, updating..."
        )

        # Delete all views (which cascades to screen_mappings)
        db.query(View).filter(View.screen_id == existing_screen.id).delete()
        db.commit()

        # Update screen properties
        existing_screen.rows = min(screen_config.layout_rows, 4)
        existing_screen.columns = min(screen_config.layout_cols, 4)
        existing_screen.switching_interval = screen_config.switch_interval
        stamp_updated(existing_screen, actor)
        db.commit()

        return existing_screen, False
    else:
        logger.info(
            f"Creating new screen '{screen_config.name}' for layout {layout_id}..."
        )

        # Generate screen ID. KEEP the trailing "_screen_{int}" suffix so the
        # next-index split-parse in configure_pc_screens keeps working.
        screen_id = f"{layout_id}_screen_{screen_index}"

        # Create new screen
        new_screen = Screen(
            id=screen_id,
            screen_layout_id=layout_id,
            name=screen_config.name,
            rows=min(screen_config.layout_rows, 4),
            columns=min(screen_config.layout_cols, 4),
            switching_interval=screen_config.switch_interval,
        )
        stamp_created(new_screen, actor)

        db.add(new_screen)
        db.commit()
        db.refresh(new_screen)

        return new_screen, True


def create_views_for_screen(
    screen: Screen,
    screen_config: ScreenConfigRequest,
    db: Session,
    actor: ActorTriple = SYSTEM_ACTOR,
) -> List[View]:
    """
    Create views for a screen.

    Args:
        screen: Screen object
        screen_config: Screen configuration
        db: Database session

    Returns:
        List of created View objects
    """
    views = []

    for view_number in range(1, screen_config.num_views + 1):
        view_id = f"{screen.id}_view_{view_number}"

        view = View(
            id=view_id,
            screen_id=screen.id,
            name=f"{screen.name} - View {view_number}",
            layout_rows=screen_config.layout_rows,
            layout_columns=screen_config.layout_cols,
            view_number=view_number,
        )
        stamp_created(view, actor)

        db.add(view)
        views.append(view)

    db.commit()

    logger.info(f"Created {len(views)} views for screen '{screen.name}'")

    return views


def distribute_cameras_and_create_mappings(
    screens_views: List[Tuple[Screen, List[View]]],
    camera_ids: List[str],
    db: Session,
    actor: ActorTriple = SYSTEM_ACTOR,
) -> int:
    """
    Distribute cameras across screens and views, creating screen mappings.

    Cameras are distributed sequentially:
    - Fill Screen 1, View 1 completely (all slots)
    - Then Screen 1, View 2
    - Then Screen 2, View 1
    - etc.

    Empty slots are NOT created (no mappings for slots without cameras).

    Args:
        screens_views: List of (Screen, List[View]) tuples
        camera_ids: List of camera IDs to distribute
        db: Database session

    Returns:
        Number of mappings created
    """
    camera_index = 0
    mappings_created = 0

    # Get all cameras with their device_ids in one query
    cameras = db.query(Camera).filter(Camera.id.in_(camera_ids)).all()
    camera_dict = {cam.id: cam for cam in cameras}

    for screen, views in screens_views:
        for view in views:
            # Calculate total slots for this view
            total_slots = view.layout_rows * view.layout_columns

            # Fill slots in row-major order
            for row in range(1, view.layout_rows + 1):
                for col in range(1, view.layout_columns + 1):
                    # Check if we have more cameras to assign
                    if camera_index >= len(camera_ids):
                        logger.info(
                            f"Ran out of cameras at view '{view.name}' slot ({row},{col}). "
                            f"Created {mappings_created} mappings total."
                        )
                        return mappings_created

                    # Get camera
                    camera_id = camera_ids[camera_index]
                    camera = camera_dict.get(camera_id)

                    if not camera:
                        logger.warning(f"Camera {camera_id} not found, skipping...")
                        camera_index += 1
                        continue

                    # Create screen mapping
                    mapping = ScreenMapping(
                        screen_id=screen.id,
                        view_id=view.id,
                        slot_row=row,
                        slot_col=col,
                        device_id=camera.device_id,
                        camera_id=camera.id,
                    )
                    stamp_created(mapping, actor)

                    db.add(mapping)
                    camera_index += 1
                    mappings_created += 1

    db.commit()

    logger.info(
        f"Created {mappings_created} camera mappings across all screens and views"
    )

    return mappings_created


def configure_pc_screens(
    pc_id: str,
    request: "ConfigurePCScreensRequest",
    db: Session,
    actor: ActorTriple = SYSTEM_ACTOR,
) -> Dict[str, Any]:
    """
    Main orchestration function for configuring PC screens.

    Args:
        pc_id: PC identifier
        request: Screen configuration request
        db: Database session

    Returns:
        Dictionary with configuration statistics

    Raises:
        ValueError: If PC not found or validation fails
    """
    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise ValueError(f"PC with ID '{pc_id}' not found")

    logger.info(f"Configuring screens for PC '{pc.name}' ({pc_id})")

    # Resolve the PC's screen layout, auto-creating and assigning one if the PC
    # has no layout yet.
    layout_id = get_or_create_layout_for_pc(pc, db, actor=actor)

    # Team boundary: every camera must belong to a site in the layout's team.
    # CrossTeamError subclasses ValueError, so the endpoint surfaces it as a 400
    # with the customer-facing message.
    assert_cameras_in_layout_team(db, request.camera_ids, layout_id)

    # Get highest existing screen index for this layout to avoid ID conflicts
    existing_screens = (
        db.query(Screen).filter(Screen.screen_layout_id == layout_id).all()
    )
    existing_indices = []
    for screen in existing_screens:
        # Extract index from ID like "PC001_screen_3"
        if "_screen_" in screen.id:
            try:
                index = int(screen.id.split("_screen_")[-1])
                existing_indices.append(index)
            except ValueError:
                pass

    next_screen_index = max(existing_indices) + 1 if existing_indices else 1

    # Track statistics
    screens_created = 0
    screens_updated = 0
    views_created = 0
    screens_views = []

    # Process each screen configuration
    for screen_config in request.screens:
        # Get or create screen
        screen, is_new = get_or_create_screen(
            layout_id, screen_config, next_screen_index, db, actor=actor
        )

        # Only increment index if we created a new screen
        if is_new:
            next_screen_index += 1

        if is_new:
            screens_created += 1
        else:
            screens_updated += 1

        # Create views for this screen
        views = create_views_for_screen(screen, screen_config, db, actor=actor)
        views_created += len(views)

        # Collect for camera distribution
        screens_views.append((screen, views))

    # Distribute cameras and create mappings
    mappings_created = distribute_cameras_and_create_mappings(
        screens_views, request.camera_ids, db, actor=actor
    )

    # Calculate cameras used (may be less than total if we ran out of slots)
    cameras_used = min(len(request.camera_ids), mappings_created)

    result = {
        "pc_id": pc_id,
        "screens_created": screens_created,
        "screens_updated": screens_updated,
        "views_created": views_created,
        "mappings_created": mappings_created,
        "cameras_used": cameras_used,
        "message": f"PC screens configured successfully: {screens_created} created, {screens_updated} updated",
    }

    logger.info(f"Configuration complete: {result}")

    touch_layout(db, layout_id, actor)

    audit_service.record_event(
        db,
        action=AuditAction.PC_SCREENS_CONFIGURED,
        resource_type=ResourceType.PC,
        resource_id=pc_id,
        actor=actor,
        changes={
            "screens": screens_created + screens_updated,
            "views": views_created,
            "mappings": mappings_created,
            "cameras_used": cameras_used,
        },
        commit=True,
    )

    return result
