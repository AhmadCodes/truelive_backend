"""
Screen management API endpoints.
Provides CRUD operations for Screens, Views, and Screen Mappings.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from typing import List, Optional
from datetime import datetime, timezone

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.pc import PC
from app.models.screen import Screen
from app.models.screen_layout import ScreenLayout
from app.models.view import View
from app.models.screen_mapping import ScreenMapping
from app.models.pc_screen_mapping_state import PcScreenMappingState
from app.models.camera import Camera
from app.models.device import Device
from app.services.team_enforcement import (
    assert_camera_in_screen_team,
    CrossTeamError,
)
from app.schemas.screen import (
    ScreenCreate,
    ScreenUpdate,
    ScreenResponse,
    ScreenDetailResponse,
    ScreenWithViews,
    ScreenCompositeResponse,
    ViewCreate,
    ViewUpdate,
    ViewResponse,
    ViewDetailResponse,
    ViewWithMappings,
    ScreenMappingCreate,
    ScreenMappingUpdate,
    ScreenMappingResponse,
    CameraMappingInfo,
)
from app.schemas.screen_layout import PlayingStateUpdate
from app.services.actor import (
    principal_to_actor,
    stamp_created,
    stamp_updated,
    snapshot,
    attach_actor_stamps,
    attach_actor_stamps_list,
    touch_layout,
)
from app.services import audit_service
from app.services.audit_service import ResourceType
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_playing_states(db, pc_id: Optional[str], mapping_ids: List[int]) -> dict:
    """
    Resolve per-PC playing_state for a set of screen mappings.

    Returns a dict of {mapping_id: playing_state}. When no pc_id is supplied,
    or a mapping has no stored state row for that pc_id, the mapping is absent
    from the dict and callers default its playing_state to False.
    """
    if not pc_id or not mapping_ids:
        return {}
    rows = (
        db.query(PcScreenMappingState)
        .filter(
            PcScreenMappingState.pc_id == pc_id,
            PcScreenMappingState.mapping_id.in_(mapping_ids),
        )
        .all()
    )
    return {row.mapping_id: row.playing_state for row in rows}


# ==================== Screen CRUD Endpoints ====================


@router.post("", response_model=ScreenResponse, status_code=status.HTTP_201_CREATED)
async def create_screen(
    screen_data: ScreenCreate, current_user: AdminUser, db: DBSession
):
    """
    Create a new screen.

    Only admins and super admins can create screens.

    Args:
        screen_data: Screen creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created screen

    Raises:
        HTTPException: If screen ID already exists or screen layout not found
    """
    actor = principal_to_actor(current_user)

    # Check if screen with this ID already exists
    existing_screen = db.query(Screen).filter(Screen.id == screen_data.id).first()
    if existing_screen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Screen with ID '{screen_data.id}' already exists",
        )

    # Verify screen layout exists
    screen_layout = (
        db.query(ScreenLayout)
        .filter(ScreenLayout.id == screen_data.screen_layout_id)
        .first()
    )
    if not screen_layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{screen_data.screen_layout_id}' not found",
        )

    # Create new screen
    new_screen = Screen(
        id=screen_data.id,
        name=screen_data.name,
        screen_layout_id=screen_data.screen_layout_id,
        rows=screen_data.rows,
        columns=screen_data.columns,
        switching_interval=screen_data.switching_interval,
    )

    db.add(new_screen)

    stamp_created(new_screen, actor)
    audit_service.record_create(
        db, resource_type=ResourceType.SCREEN, resource_id=new_screen.id, actor=actor
    )
    touch_layout(db, new_screen.screen_layout_id, actor)

    db.commit()
    db.refresh(new_screen)

    logger.info(f"Screen '{new_screen.id}' created by user {current_user.username}")
    return new_screen


@router.get("", response_model=List[ScreenWithViews])
async def list_screens(
    current_user: CurrentUser,
    db: DBSession,
    screen_layout_id: Optional[str] = Query(
        None, description="Filter by screen layout ID"
    ),
    search: Optional[str] = Query(None, description="Search by name"),
):
    """
    List all screens with optional filters.

    All authenticated users can view screens.

    Args:
        current_user: Current authenticated user
        db: Database session
        screen_layout_id: Optional screen layout ID filter
        search: Optional search term for name

    Returns:
        List of screens with view counts
    """
    query = db.query(Screen)

    # Apply filters
    if screen_layout_id:
        query = query.filter(Screen.screen_layout_id == screen_layout_id)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(Screen.name.ilike(search_pattern))

    screens = query.order_by(Screen.name).all()

    # Convert to response format with view count
    result = []
    for screen in screens:
        screen_data = ScreenWithViews.model_validate(screen)

        # Count views for this screen
        view_count = (
            db.query(func.count(View.id)).filter(View.screen_id == screen.id).scalar()
            or 0
        )
        screen_data.view_count = view_count

        result.append(screen_data)

    attach_actor_stamps_list(db, result, screens)

    return result


@router.get("/count")
async def get_screen_count(
    current_user: CurrentUser,
    db: DBSession,
    screen_layout_id: Optional[str] = Query(
        None, description="Filter by screen layout ID"
    ),
):
    """
    Get count of screens.

    All authenticated users can view screen counts.

    Args:
        current_user: Current authenticated user
        db: Database session
        screen_layout_id: Optional screen layout ID filter

    Returns:
        Screen count statistics
    """
    query = db.query(func.count(Screen.id))

    if screen_layout_id:
        query = query.filter(Screen.screen_layout_id == screen_layout_id)

    total_count = query.scalar() or 0

    return {"total": total_count}


@router.get("/{screen_id}", response_model=ScreenDetailResponse)
async def get_screen(screen_id: str, current_user: CurrentUser, db: DBSession):
    """
    Get a specific screen by ID.

    All authenticated users can view screen details.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Screen details

    Raises:
        HTTPException: If screen not found
    """
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    resp = ScreenDetailResponse.model_validate(screen)
    attach_actor_stamps(db, resp, screen)
    return resp


@router.get("/{screen_id}/with-views", response_model=ScreenWithViews)
async def get_screen_with_views(
    screen_id: str, current_user: CurrentUser, db: DBSession
):
    """
    Get a screen with all its views.

    All authenticated users can view screen details.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Screen with views

    Raises:
        HTTPException: If screen not found
    """
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    # Get views
    views = (
        db.query(View)
        .filter(View.screen_id == screen_id)
        .order_by(View.view_number)
        .all()
    )

    result = ScreenWithViews.model_validate(screen)
    result.views = [ViewResponse.model_validate(v) for v in views]
    result.view_count = len(views)

    attach_actor_stamps_list(db, result.views, views)
    attach_actor_stamps(db, result, screen)

    return result


@router.get("/{screen_id}/layout", response_model=ScreenCompositeResponse)
async def get_screen_layout(
    screen_id: str,
    current_user: CurrentUser,
    db: DBSession,
    pc_id: Optional[str] = Query(
        None, description="Resolve per-PC playing state for this PC"
    ),
):
    """
    Get complete screen layout with views and camera mappings.

    All authenticated users can view screen layouts.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated user
        db: Database session
        pc_id: Optional PC ID to resolve per-PC playing state; when omitted,
            playing_state is emitted as False

    Returns:
        Complete screen layout

    Raises:
        HTTPException: If screen not found
    """
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    # Get views with mappings
    views = (
        db.query(View)
        .filter(View.screen_id == screen_id)
        .order_by(View.view_number)
        .all()
    )

    views_with_mappings = []
    for view in views:
        # Get mappings for this view
        mappings = (
            db.query(ScreenMapping).filter(ScreenMapping.view_id == view.id).all()
        )
        state_map = _resolve_playing_states(db, pc_id, [m.id for m in mappings])

        mapping_infos = []
        for mapping in mappings:
            mapping_info = CameraMappingInfo(
                slot_row=mapping.slot_row,
                slot_col=mapping.slot_col,
                device_id=mapping.device_id,
                camera_id=mapping.camera_id,
                playing_state=state_map.get(mapping.id, False),
            )

            # Add device and camera names
            if mapping.device:
                mapping_info.device_name = mapping.device.name
            if mapping.camera:
                mapping_info.camera_name = mapping.camera.name

            mapping_infos.append(mapping_info)

        view_with_mappings = ViewWithMappings.model_validate(view)
        view_with_mappings.mappings = mapping_infos
        views_with_mappings.append(view_with_mappings)

    result = ScreenCompositeResponse.model_validate(screen)
    result.views = views_with_mappings
    result.view_count = len(views_with_mappings)

    attach_actor_stamps_list(db, views_with_mappings, views)
    attach_actor_stamps(db, result, screen)

    return result


@router.put("/{screen_id}", response_model=ScreenResponse)
async def update_screen(
    screen_id: str, screen_data: ScreenUpdate, current_user: AdminUser, db: DBSession
):
    """
    Update a screen.

    Only admins and super admins can update screens.

    Args:
        screen_id: Screen ID
        screen_data: Screen update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated screen

    Raises:
        HTTPException: If screen not found or validation fails
    """
    actor = principal_to_actor(current_user)

    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    before = snapshot(screen)

    # Update fields
    update_data = screen_data.model_dump(exclude_unset=True)

    # If updating screen_layout_id, verify it exists
    if "screen_layout_id" in update_data:
        screen_layout = (
            db.query(ScreenLayout)
            .filter(ScreenLayout.id == update_data["screen_layout_id"])
            .first()
        )
        if not screen_layout:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Screen layout with ID '{update_data['screen_layout_id']}' not found",
            )

    # Detect grid resize so we can cascade to views and prune out-of-bounds mappings.
    # Without this, views keep their old layout_rows/layout_columns and assign_camera_to_slot
    # rejects placements in the new cells (e.g., screen=3x4 with views still 3x3 -> col=4 -> 400).
    new_rows = update_data.get("rows", screen.rows)
    new_cols = update_data.get("columns", screen.columns)
    grid_resized = new_rows != screen.rows or new_cols != screen.columns

    for field, value in update_data.items():
        setattr(screen, field, value)

    pruned_mappings_snapshot: list = []
    if grid_resized:
        # Cascade new dimensions to all views of this screen. Bulk .update() bypasses
        # ORM stamping/onupdate, so the actor triple + updated_at are set explicitly.
        db.query(View).filter(View.screen_id == screen.id).update(
            {
                View.layout_rows: new_rows,
                View.layout_columns: new_cols,
                View.updated_by_type: actor[0],
                View.updated_by_id: actor[1],
                View.updated_by_label: actor[2],
                View.updated_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        # Capture the doomed mappings BEFORE the bulk delete so the audit trail
        # records what was pruned.
        doomed_mappings = (
            db.query(ScreenMapping)
            .filter(
                ScreenMapping.screen_id == screen.id,
                or_(
                    ScreenMapping.slot_row > new_rows, ScreenMapping.slot_col > new_cols
                ),
            )
            .all()
        )
        pruned_mappings_snapshot = [
            {
                "id": m.id,
                "slot_row": m.slot_row,
                "slot_col": m.slot_col,
                "camera_id": m.camera_id,
            }
            for m in doomed_mappings
        ]
        # Drop any mappings whose slot now falls outside the new grid.
        pruned = (
            db.query(ScreenMapping)
            .filter(
                ScreenMapping.screen_id == screen.id,
                or_(
                    ScreenMapping.slot_row > new_rows, ScreenMapping.slot_col > new_cols
                ),
            )
            .delete(synchronize_session=False)
        )
        if pruned:
            logger.info(
                f"Screen '{screen_id}' resized to {new_rows}x{new_cols}; "
                f"pruned {pruned} out-of-bounds mapping(s)"
            )

    stamp_updated(screen, actor)
    audit_service.record_update(
        db, resource_type=ResourceType.SCREEN, resource_id=screen.id, actor=actor,
        before=before, after=snapshot(screen),
        extra={"pruned_mappings": pruned_mappings_snapshot} if grid_resized else None,
    )
    touch_layout(db, screen.screen_layout_id, actor)

    db.commit()
    db.refresh(screen)

    logger.info(f"Screen '{screen_id}' updated by user {current_user.username}")
    return screen


@router.delete("/{screen_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_screen(screen_id: str, current_user: AdminUser, db: DBSession):
    """
    Delete a screen.

    Only admins and super admins can delete screens.
    All associated views and mappings will be deleted due to cascade.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If screen not found
    """
    actor = principal_to_actor(current_user)

    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    snap = snapshot(screen)
    layout_id = screen.screen_layout_id

    db.delete(screen)

    audit_service.record_delete(
        db, resource_type=ResourceType.SCREEN, resource_id=screen_id, actor=actor, snapshot=snap
    )
    touch_layout(db, layout_id, actor)

    db.commit()

    logger.info(f"Screen '{screen_id}' deleted by user {current_user.username}")


# ==================== View CRUD Endpoints ====================


@router.post(
    "/{screen_id}/views",
    response_model=ViewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_view(
    screen_id: str, view_data: ViewCreate, current_user: AdminUser, db: DBSession
):
    """
    Create a new view for a screen.

    Only admins and super admins can create views.

    Args:
        screen_id: Screen ID
        view_data: View creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created view

    Raises:
        HTTPException: If view ID already exists, screen not found, or view_number conflict
    """
    actor = principal_to_actor(current_user)

    # Verify screen exists
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    # Check if view with this ID already exists
    existing_view = db.query(View).filter(View.id == view_data.id).first()
    if existing_view:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"View with ID '{view_data.id}' already exists",
        )

    # Check for view_number conflict
    existing_view_number = (
        db.query(View)
        .filter(View.screen_id == screen_id, View.view_number == view_data.view_number)
        .first()
    )
    if existing_view_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"View number {view_data.view_number} already exists for screen '{screen_id}'",
        )

    # Create new view
    new_view = View(
        id=view_data.id,
        screen_id=screen_id,
        name=view_data.name,
        layout_rows=view_data.layout_rows,
        layout_columns=view_data.layout_columns,
        view_number=view_data.view_number,
    )

    db.add(new_view)

    stamp_created(new_view, actor)
    audit_service.record_create(
        db, resource_type=ResourceType.VIEW, resource_id=new_view.id, actor=actor
    )
    touch_layout(db, screen.screen_layout_id, actor)

    db.commit()
    db.refresh(new_view)

    logger.info(
        f"View '{new_view.id}' created for screen '{screen_id}' by user {current_user.username}"
    )
    return new_view


@router.get("/{screen_id}/views", response_model=List[ViewResponse])
async def list_views(screen_id: str, current_user: CurrentUser, db: DBSession):
    """
    List all views for a screen.

    All authenticated users can view views.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of views

    Raises:
        HTTPException: If screen not found
    """
    # Verify screen exists
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found",
        )

    views = (
        db.query(View)
        .filter(View.screen_id == screen_id)
        .order_by(View.view_number)
        .all()
    )

    responses = [ViewResponse.model_validate(v) for v in views]
    attach_actor_stamps_list(db, responses, views)
    return responses


@router.get("/views/{view_id}", response_model=ViewDetailResponse)
async def get_view(view_id: str, current_user: CurrentUser, db: DBSession):
    """
    Get a specific view by ID.

    All authenticated users can view view details.

    Args:
        view_id: View ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        View details

    Raises:
        HTTPException: If view not found
    """
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    resp = ViewDetailResponse.model_validate(view)
    attach_actor_stamps(db, resp, view)
    return resp


@router.get("/views/{view_id}/with-mappings", response_model=ViewWithMappings)
async def get_view_with_mappings(
    view_id: str,
    current_user: CurrentUser,
    db: DBSession,
    pc_id: Optional[str] = Query(
        None, description="Resolve per-PC playing state for this PC"
    ),
):
    """
    Get a view with all its camera mappings.

    All authenticated users can view view details.

    Args:
        view_id: View ID
        current_user: Current authenticated user
        db: Database session
        pc_id: Optional PC ID to resolve per-PC playing state; when omitted,
            playing_state is emitted as False

    Returns:
        View with camera mappings

    Raises:
        HTTPException: If view not found
    """
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    # Get mappings
    mappings = db.query(ScreenMapping).filter(ScreenMapping.view_id == view_id).all()
    state_map = _resolve_playing_states(db, pc_id, [m.id for m in mappings])

    mapping_infos = []
    for mapping in mappings:
        mapping_info = CameraMappingInfo(
            slot_row=mapping.slot_row,
            slot_col=mapping.slot_col,
            device_id=mapping.device_id,
            camera_id=mapping.camera_id,
            playing_state=state_map.get(mapping.id, False),
        )

        # Add device and camera names
        if mapping.device:
            mapping_info.device_name = mapping.device.name
        if mapping.camera:
            mapping_info.camera_name = mapping.camera.name

        mapping_infos.append(mapping_info)

    result = ViewWithMappings.model_validate(view)
    result.mappings = mapping_infos

    attach_actor_stamps(db, result, view)

    return result


@router.put("/views/{view_id}", response_model=ViewResponse)
async def update_view(
    view_id: str, view_data: ViewUpdate, current_user: AdminUser, db: DBSession
):
    """
    Update a view.

    Only admins and super admins can update views.

    Args:
        view_id: View ID
        view_data: View update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated view

    Raises:
        HTTPException: If view not found or validation fails
    """
    actor = principal_to_actor(current_user)

    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    before = snapshot(view)

    # Update fields
    update_data = view_data.model_dump(exclude_unset=True)

    # If updating view_number, check for conflicts
    if "view_number" in update_data:
        existing = (
            db.query(View)
            .filter(
                View.screen_id == view.screen_id,
                View.view_number == update_data["view_number"],
                View.id != view_id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"View number {update_data['view_number']} already exists for screen '{view.screen_id}'",
            )

    for field, value in update_data.items():
        setattr(view, field, value)

    stamp_updated(view, actor)
    audit_service.record_update(
        db, resource_type=ResourceType.VIEW, resource_id=view.id, actor=actor,
        before=before, after=snapshot(view),
    )
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
    )
    touch_layout(db, layout_id, actor)

    db.commit()
    db.refresh(view)

    logger.info(f"View '{view_id}' updated by user {current_user.username}")
    return view


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(view_id: str, current_user: AdminUser, db: DBSession):
    """
    Delete a view.

    Only admins and super admins can delete views.
    All associated mappings will be deleted due to cascade.

    Args:
        view_id: View ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If view not found
    """
    actor = principal_to_actor(current_user)

    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    snap = snapshot(view)
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
    )

    db.delete(view)

    audit_service.record_delete(
        db, resource_type=ResourceType.VIEW, resource_id=view_id, actor=actor, snapshot=snap
    )
    touch_layout(db, layout_id, actor)

    db.commit()

    logger.info(f"View '{view_id}' deleted by user {current_user.username}")


# ==================== Screen Mapping Endpoints ====================


@router.post(
    "/views/{view_id}/mappings",
    response_model=ScreenMappingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_screen_mapping(
    view_id: str,
    mapping_data: ScreenMappingCreate,
    current_user: AdminUser,
    db: DBSession,
):
    """
    Create a camera mapping for a view slot.

    Only admins and super admins can create mappings.

    Args:
        view_id: View ID
        mapping_data: Mapping creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created mapping

    Raises:
        HTTPException: If view not found, slot conflict, or camera/device not found
    """
    actor = principal_to_actor(current_user)

    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    # Validate slot position
    if mapping_data.slot_row > view.layout_rows or mapping_data.slot_row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid slot_row {mapping_data.slot_row}. Must be between 1 and {view.layout_rows}",
        )

    if mapping_data.slot_col > view.layout_columns or mapping_data.slot_col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid slot_col {mapping_data.slot_col}. Must be between 1 and {view.layout_columns}",
        )

    # Check for existing mapping at this slot
    existing_mapping = (
        db.query(ScreenMapping)
        .filter(
            ScreenMapping.view_id == view_id,
            ScreenMapping.slot_row == mapping_data.slot_row,
            ScreenMapping.slot_col == mapping_data.slot_col,
        )
        .first()
    )

    if existing_mapping:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slot ({mapping_data.slot_row}, {mapping_data.slot_col}) already has a mapping",
        )

    # Verify camera and device if provided
    if mapping_data.camera_id:
        camera = db.query(Camera).filter(Camera.id == mapping_data.camera_id).first()
        if not camera:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Camera with ID '{mapping_data.camera_id}' not found",
            )
        # Team boundary: the camera's site must belong to this layout's team.
        try:
            assert_camera_in_screen_team(db, mapping_data.camera_id, view.screen_id)
        except CrossTeamError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )

    if mapping_data.device_id:
        device = db.query(Device).filter(Device.id == mapping_data.device_id).first()
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device with ID '{mapping_data.device_id}' not found",
            )

    # Create mapping
    new_mapping = ScreenMapping(
        screen_id=view.screen_id,
        view_id=view_id,
        slot_row=mapping_data.slot_row,
        slot_col=mapping_data.slot_col,
        device_id=mapping_data.device_id,
        camera_id=mapping_data.camera_id,
    )

    db.add(new_mapping)
    db.flush()  # Get the autoincrement mapping id before recording the audit entry

    stamp_created(new_mapping, actor)
    audit_service.record_create(
        db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(new_mapping.id), actor=actor
    )
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
    )
    touch_layout(db, layout_id, actor)

    db.commit()
    db.refresh(new_mapping)

    logger.info(
        f"Screen mapping created for view '{view_id}' slot ({mapping_data.slot_row}, {mapping_data.slot_col}) by user {current_user.username}"
    )
    return new_mapping


@router.get("/views/{view_id}/mappings", response_model=List[ScreenMappingResponse])
async def list_view_mappings(view_id: str, current_user: CurrentUser, db: DBSession):
    """
    List all camera mappings for a view.

    All authenticated users can view mappings.

    Args:
        view_id: View ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of mappings

    Raises:
        HTTPException: If view not found
    """
    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    mappings = (
        db.query(ScreenMapping)
        .filter(ScreenMapping.view_id == view_id)
        .order_by(ScreenMapping.slot_row, ScreenMapping.slot_col)
        .all()
    )

    responses = [ScreenMappingResponse.model_validate(m) for m in mappings]
    attach_actor_stamps_list(db, responses, mappings)
    return responses


@router.put("/mappings/{mapping_id}", response_model=ScreenMappingResponse)
async def update_screen_mapping(
    mapping_id: int,
    mapping_data: ScreenMappingUpdate,
    current_user: AdminUser,
    db: DBSession,
):
    """
    Update a screen mapping.

    Only admins and super admins can update mappings.

    Args:
        mapping_id: Mapping ID
        mapping_data: Mapping update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated mapping

    Raises:
        HTTPException: If mapping not found or validation fails
    """
    actor = principal_to_actor(current_user)

    mapping = db.query(ScreenMapping).filter(ScreenMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen mapping with ID {mapping_id} not found",
        )

    before = snapshot(mapping)

    # Update fields
    update_data = mapping_data.model_dump(exclude_unset=True)

    # Verify camera if updating
    if "camera_id" in update_data and update_data["camera_id"]:
        camera = db.query(Camera).filter(Camera.id == update_data["camera_id"]).first()
        if not camera:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Camera with ID '{update_data['camera_id']}' not found",
            )
        # Team boundary: the camera's site must belong to this layout's team.
        try:
            assert_camera_in_screen_team(
                db, update_data["camera_id"], mapping.screen_id
            )
        except CrossTeamError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )

    # Verify device if updating
    if "device_id" in update_data and update_data["device_id"]:
        device = db.query(Device).filter(Device.id == update_data["device_id"]).first()
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device with ID '{update_data['device_id']}' not found",
            )

    for field, value in update_data.items():
        setattr(mapping, field, value)

    stamp_updated(mapping, actor)
    audit_service.record_update(
        db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(mapping.id), actor=actor,
        before=before, after=snapshot(mapping),
    )
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == mapping.screen_id).scalar()
    )
    touch_layout(db, layout_id, actor)

    db.commit()
    db.refresh(mapping)

    logger.info(f"Screen mapping {mapping_id} updated by user {current_user.username}")
    return mapping


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_screen_mapping(
    mapping_id: int, current_user: AdminUser, db: DBSession
):
    """
    Delete a screen mapping.

    Only admins and super admins can delete mappings.

    Args:
        mapping_id: Mapping ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If mapping not found
    """
    actor = principal_to_actor(current_user)

    mapping = db.query(ScreenMapping).filter(ScreenMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen mapping with ID {mapping_id} not found",
        )

    snap = snapshot(mapping)
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == mapping.screen_id).scalar()
    )

    db.delete(mapping)

    audit_service.record_delete(
        db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(mapping_id), actor=actor,
        snapshot=snap,
    )
    touch_layout(db, layout_id, actor)

    db.commit()

    logger.info(f"Screen mapping {mapping_id} deleted by user {current_user.username}")


@router.put("/mappings/{mapping_id}/playing-state")
async def set_mapping_playing_state(
    mapping_id: int,
    state_data: PlayingStateUpdate,
    current_user: AdminUser,
    db: DBSession,
):
    """
    Set the per-PC playing state for a screen mapping.

    Only admins and super admins can set playing state. Playing state is stored
    per (PC, mapping) pair, so multiple PCs sharing a layout each keep their own
    state independently.

    Args:
        mapping_id: Mapping ID
        state_data: PC ID and desired playing state
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        The mapping the state was set for

    Raises:
        HTTPException: If mapping or PC not found
    """
    mapping = db.query(ScreenMapping).filter(ScreenMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen mapping with ID {mapping_id} not found",
        )

    pc = db.query(PC).filter(PC.id == state_data.pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{state_data.pc_id}' not found",
        )

    # Atomic upsert of the per-(PC, mapping) state row. ON CONFLICT keeps this
    # race-safe: two concurrent writers for the same (pc_id, mapping_id) can't
    # both insert and trip uq_pc_screen_mapping_state (which would 500 the loser).
    stmt = (
        pg_insert(PcScreenMappingState)
        .values(
            pc_id=state_data.pc_id,
            mapping_id=mapping_id,
            playing_state=state_data.playing_state,
        )
        .on_conflict_do_update(
            constraint="uq_pc_screen_mapping_state",
            set_={"playing_state": state_data.playing_state},
        )
    )
    db.execute(stmt)
    db.commit()

    logger.info(
        f"Playing state for mapping {mapping_id} on PC '{state_data.pc_id}' "
        f"set to {state_data.playing_state} by user {current_user.username}"
    )
    # Echo the value that was set so the caller can confirm the write.
    return {
        "pc_id": state_data.pc_id,
        "mapping_id": mapping_id,
        "playing_state": state_data.playing_state,
    }


# ==================== Additional View and Mapping Endpoints ====================


@router.get("/views/{view_id}/slot/{row}/{col}", response_model=CameraMappingInfo)
async def get_camera_at_slot(
    view_id: str,
    row: int,
    col: int,
    current_user: CurrentUser,
    db: DBSession,
    pc_id: Optional[str] = Query(
        None, description="Resolve per-PC playing state for this PC"
    ),
):
    """
    Get camera assigned to a specific slot in a view.

    All authenticated users can view mappings.

    Args:
        view_id: View ID
        row: Slot row (1-indexed)
        col: Slot column (1-indexed)
        current_user: Current authenticated user
        db: DBSession: Database session
        pc_id: Optional PC ID to resolve per-PC playing state; when omitted,
            playing_state is emitted as False

    Returns:
        Camera mapping info for the slot, or empty slot if no camera assigned

    Raises:
        HTTPException: If view not found or slot out of bounds
    """
    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    # Validate slot position
    if row > view.layout_rows or row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid row {row}. Must be between 1 and {view.layout_rows}",
        )

    if col > view.layout_columns or col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid col {col}. Must be between 1 and {view.layout_columns}",
        )

    # Get mapping for this slot
    mapping = (
        db.query(ScreenMapping)
        .filter(
            ScreenMapping.view_id == view_id,
            ScreenMapping.slot_row == row,
            ScreenMapping.slot_col == col,
        )
        .first()
    )

    if not mapping:
        # Return empty slot info
        return CameraMappingInfo(
            slot_row=row,
            slot_col=col,
            device_id=None,
            device_name=None,
            camera_id=None,
            camera_name=None,
            playing_state=False,
        )

    # Return mapping info with device and camera names
    state_map = _resolve_playing_states(db, pc_id, [mapping.id])
    mapping_info = CameraMappingInfo(
        slot_row=mapping.slot_row,
        slot_col=mapping.slot_col,
        device_id=mapping.device_id,
        camera_id=mapping.camera_id,
        playing_state=state_map.get(mapping.id, False),
    )

    if mapping.device:
        mapping_info.device_name = mapping.device.name
    if mapping.camera:
        mapping_info.camera_name = mapping.camera.name

    return mapping_info


@router.patch("/views/{view_id}/rename")
async def rename_view(
    view_id: str,
    new_name: str = Query(
        ..., min_length=1, max_length=50, description="New view name"
    ),
    current_user: AdminUser = None,
    db: DBSession = None,
):
    """
    Rename a view (convenience endpoint).

    Only admins and super admins can rename views.

    Args:
        view_id: View ID
        new_name: New name for the view
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated view

    Raises:
        HTTPException: If view not found
    """
    actor = principal_to_actor(current_user)

    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    before = snapshot(view)

    old_name = view.name
    view.name = new_name

    stamp_updated(view, actor)
    audit_service.record_update(
        db, resource_type=ResourceType.VIEW, resource_id=view.id, actor=actor,
        before=before, after=snapshot(view),
    )
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
    )
    touch_layout(db, layout_id, actor)

    db.commit()
    db.refresh(view)

    logger.info(
        f"View '{view_id}' renamed from '{old_name}' to '{new_name}' by user {current_user.username}"
    )
    return ViewResponse.model_validate(view)


@router.get("/pc/{pc_id}/all-views", response_model=List[ScreenCompositeResponse])
async def get_all_views_for_pc(pc_id: str, current_user: CurrentUser, db: DBSession):
    """
    Get all views with mappings for all screens of a PC.

    All authenticated users can view this information.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of screen composites with views and camera mappings

    Raises:
        HTTPException: If PC not found
    """
    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    # Re-root through the PC's assigned layout; no layout means no screens.
    if not pc.screen_layout_id:
        return []

    # Get all screens for this PC's layout
    screens = (
        db.query(Screen)
        .filter(Screen.screen_layout_id == pc.screen_layout_id)
        .order_by(Screen.name)
        .all()
    )

    results = []
    for screen in screens:
        # Get views with mappings for each screen
        views = (
            db.query(View)
            .filter(View.screen_id == screen.id)
            .order_by(View.view_number)
            .all()
        )

        views_with_mappings = []
        for view in views:
            # Get mappings for this view
            mappings = (
                db.query(ScreenMapping).filter(ScreenMapping.view_id == view.id).all()
            )
            state_map = _resolve_playing_states(db, pc_id, [m.id for m in mappings])

            mapping_infos = []
            for mapping in mappings:
                mapping_info = CameraMappingInfo(
                    slot_row=mapping.slot_row,
                    slot_col=mapping.slot_col,
                    device_id=mapping.device_id,
                    camera_id=mapping.camera_id,
                    playing_state=state_map.get(mapping.id, False),
                )

                # Add device and camera names
                if mapping.device:
                    mapping_info.device_name = mapping.device.name
                if mapping.camera:
                    mapping_info.camera_name = mapping.camera.name

                mapping_infos.append(mapping_info)

            view_with_mappings = ViewWithMappings.model_validate(view)
            view_with_mappings.mappings = mapping_infos
            views_with_mappings.append(view_with_mappings)

        attach_actor_stamps_list(db, views_with_mappings, views)

        # Build screen composite response
        screen_composite = ScreenCompositeResponse.model_validate(screen)
        screen_composite.views = views_with_mappings
        screen_composite.view_count = len(views_with_mappings)

        results.append(screen_composite)

    attach_actor_stamps_list(db, results, screens)

    return results


@router.delete(
    "/views/{view_id}/slot/{row}/{col}", status_code=status.HTTP_204_NO_CONTENT
)
async def clear_slot(
    view_id: str, row: int, col: int, current_user: AdminUser, db: DBSession
):
    """
    Remove camera from a specific slot (clear the slot).

    Only admins and super admins can clear slots.

    Args:
        view_id: View ID
        row: Slot row (1-indexed)
        col: Slot column (1-indexed)
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If view or mapping not found
    """
    actor = principal_to_actor(current_user)

    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    # Get mapping for this slot
    mapping = (
        db.query(ScreenMapping)
        .filter(
            ScreenMapping.view_id == view_id,
            ScreenMapping.slot_row == row,
            ScreenMapping.slot_col == col,
        )
        .first()
    )

    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No camera assigned to slot ({row}, {col}) in view '{view_id}'",
        )

    snap = snapshot(mapping)
    layout_id = (
        db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
    )

    db.delete(mapping)

    audit_service.record_delete(
        db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(mapping.id), actor=actor,
        snapshot=snap,
    )
    touch_layout(db, layout_id, actor)

    db.commit()

    logger.info(
        f"Slot ({row}, {col}) cleared in view '{view_id}' by user {current_user.username}"
    )


@router.put("/views/{view_id}/slot/{row}/{col}")
async def assign_camera_to_slot(
    view_id: str,
    row: int,
    col: int,
    camera_id: str = Query(..., description="Camera ID to assign"),
    device_id: str = Query(None, description="Device ID (optional)"),
    current_user: AdminUser = None,
    db: DBSession = None,
):
    """
    Assign or update camera in a specific slot.

    Only admins and super admins can assign cameras.

    Args:
        view_id: View ID
        row: Slot row (1-indexed)
        col: Slot column (1-indexed)
        camera_id: Camera ID to assign
        device_id: Optional device ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Screen mapping

    Raises:
        HTTPException: If view or camera not found, or slot out of bounds
    """
    actor = principal_to_actor(current_user)

    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found",
        )

    # Validate slot position
    if row > view.layout_rows or row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid row {row}. Must be between 1 and {view.layout_rows}",
        )

    if col > view.layout_columns or col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid col {col}. Must be between 1 and {view.layout_columns}",
        )

    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found",
        )

    # Verify device if provided
    if device_id:
        device = db.query(Device).filter(Device.id == device_id).first()
        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device with ID '{device_id}' not found",
            )

    # Check if mapping already exists for this slot
    existing_mapping = (
        db.query(ScreenMapping)
        .filter(
            ScreenMapping.view_id == view_id,
            ScreenMapping.slot_row == row,
            ScreenMapping.slot_col == col,
        )
        .first()
    )

    if existing_mapping:
        # Update existing mapping
        before = snapshot(existing_mapping)
        existing_mapping.camera_id = camera_id
        existing_mapping.device_id = device_id

        stamp_updated(existing_mapping, actor)
        audit_service.record_update(
            db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(existing_mapping.id),
            actor=actor, before=before, after=snapshot(existing_mapping),
        )
        layout_id = (
            db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
        )
        touch_layout(db, layout_id, actor)

        db.commit()
        db.refresh(existing_mapping)
        logger.info(
            f"Camera '{camera_id}' assigned to slot ({row}, {col}) in view '{view_id}' by user {current_user.username}"
        )
        return ScreenMappingResponse.model_validate(existing_mapping)
    else:
        # Create new mapping
        new_mapping = ScreenMapping(
            screen_id=view.screen_id,
            view_id=view_id,
            slot_row=row,
            slot_col=col,
            device_id=device_id,
            camera_id=camera_id,
        )
        db.add(new_mapping)
        db.flush()  # Get the autoincrement mapping id before recording the audit entry

        stamp_created(new_mapping, actor)
        audit_service.record_create(
            db, resource_type=ResourceType.SCREEN_MAPPING, resource_id=str(new_mapping.id), actor=actor
        )
        layout_id = (
            db.query(Screen.screen_layout_id).filter(Screen.id == view.screen_id).scalar()
        )
        touch_layout(db, layout_id, actor)

        db.commit()
        db.refresh(new_mapping)
        logger.info(
            f"Camera '{camera_id}' assigned to slot ({row}, {col}) in view '{view_id}' by user {current_user.username}"
        )
        return ScreenMappingResponse.model_validate(new_mapping)
