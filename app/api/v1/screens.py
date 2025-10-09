"""
Screen management API endpoints.
Provides CRUD operations for Screens, Views, and Screen Mappings.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy import func, or_
from typing import List, Optional

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.pc import PC
from app.models.screen import Screen
from app.models.view import View
from app.models.screen_mapping import ScreenMapping
from app.models.camera import Camera
from app.models.site import Site
from app.schemas.screen import (
    ScreenCreate,
    ScreenUpdate,
    ScreenResponse,
    ScreenDetailResponse,
    ScreenWithPC,
    ScreenWithViews,
    ScreenLayoutResponse,
    ViewCreate,
    ViewUpdate,
    ViewResponse,
    ViewDetailResponse,
    ViewWithMappings,
    ScreenMappingCreate,
    ScreenMappingUpdate,
    ScreenMappingResponse,
    CameraMappingInfo,
    PCInfo
)
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Screen CRUD Endpoints ====================

@router.post("", response_model=ScreenResponse, status_code=status.HTTP_201_CREATED)
async def create_screen(
    screen_data: ScreenCreate,
    current_user: AdminUser,
    db: DBSession
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
        HTTPException: If screen ID already exists or PC not found
    """
    # Check if screen with this ID already exists
    existing_screen = db.query(Screen).filter(Screen.id == screen_data.id).first()
    if existing_screen:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Screen with ID '{screen_data.id}' already exists"
        )

    # Verify PC exists
    pc = db.query(PC).filter(PC.id == screen_data.pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{screen_data.pc_id}' not found"
        )

    # Create new screen
    new_screen = Screen(
        id=screen_data.id,
        name=screen_data.name,
        pc_id=screen_data.pc_id,
        rows=screen_data.rows,
        columns=screen_data.columns,
        switching_interval=screen_data.switching_interval
    )

    db.add(new_screen)
    db.commit()
    db.refresh(new_screen)

    logger.info(f"Screen '{new_screen.id}' created by user {current_user.username}")
    return new_screen


@router.get("", response_model=List[ScreenWithPC])
async def list_screens(
    current_user: CurrentUser,
    db: DBSession,
    pc_id: Optional[str] = Query(None, description="Filter by PC ID"),
    search: Optional[str] = Query(None, description="Search by name")
):
    """
    List all screens with optional filters.

    All authenticated users can view screens.

    Args:
        current_user: Current authenticated user
        db: Database session
        pc_id: Optional PC ID filter
        search: Optional search term for name

    Returns:
        List of screens with PC information
    """
    query = db.query(Screen)

    # Apply filters
    if pc_id:
        query = query.filter(Screen.pc_id == pc_id)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(Screen.name.ilike(search_pattern))

    screens = query.order_by(Screen.name).all()

    # Convert to response format with PC info
    result = []
    for screen in screens:
        screen_data = ScreenWithPC.model_validate(screen)
        if screen.pc:
            screen_data.pc = PCInfo.model_validate(screen.pc)
        result.append(screen_data)

    return result


@router.get("/count")
async def get_screen_count(
    current_user: CurrentUser,
    db: DBSession,
    pc_id: Optional[str] = Query(None, description="Filter by PC ID")
):
    """
    Get count of screens.

    All authenticated users can view screen counts.

    Args:
        current_user: Current authenticated user
        db: Database session
        pc_id: Optional PC ID filter

    Returns:
        Screen count statistics
    """
    query = db.query(func.count(Screen.id))

    if pc_id:
        query = query.filter(Screen.pc_id == pc_id)

    total_count = query.scalar() or 0

    return {
        "total": total_count
    }


@router.get("/{screen_id}", response_model=ScreenDetailResponse)
async def get_screen(
    screen_id: str,
    current_user: CurrentUser,
    db: DBSession
):
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
            detail=f"Screen with ID '{screen_id}' not found"
        )

    return screen


@router.get("/{screen_id}/with-views", response_model=ScreenWithViews)
async def get_screen_with_views(
    screen_id: str,
    current_user: CurrentUser,
    db: DBSession
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
            detail=f"Screen with ID '{screen_id}' not found"
        )

    # Get views
    views = db.query(View).filter(View.screen_id == screen_id).order_by(View.view_number).all()

    result = ScreenWithViews.model_validate(screen)
    if screen.pc:
        result.pc = PCInfo.model_validate(screen.pc)
    result.views = [ViewResponse.model_validate(v) for v in views]
    result.view_count = len(views)

    return result


@router.get("/{screen_id}/layout", response_model=ScreenLayoutResponse)
async def get_screen_layout(
    screen_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get complete screen layout with views and camera mappings.

    All authenticated users can view screen layouts.

    Args:
        screen_id: Screen ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Complete screen layout

    Raises:
        HTTPException: If screen not found
    """
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found"
        )

    # Get views with mappings
    views = db.query(View).filter(View.screen_id == screen_id).order_by(View.view_number).all()

    views_with_mappings = []
    for view in views:
        # Get mappings for this view
        mappings = db.query(ScreenMapping).filter(ScreenMapping.view_id == view.id).all()

        mapping_infos = []
        for mapping in mappings:
            mapping_info = CameraMappingInfo(
                slot_row=mapping.slot_row,
                slot_col=mapping.slot_col,
                site_id=mapping.site_id,
                camera_id=mapping.camera_id,
                playing_state=mapping.playing_state
            )

            # Add site and camera names
            if mapping.site:
                mapping_info.site_name = mapping.site.name
            if mapping.camera:
                mapping_info.camera_name = mapping.camera.name

            mapping_infos.append(mapping_info)

        view_with_mappings = ViewWithMappings.model_validate(view)
        view_with_mappings.mappings = mapping_infos
        views_with_mappings.append(view_with_mappings)

    result = ScreenLayoutResponse.model_validate(screen)
    if screen.pc:
        result.pc = PCInfo.model_validate(screen.pc)
    result.views = views_with_mappings
    result.view_count = len(views_with_mappings)

    return result


@router.put("/{screen_id}", response_model=ScreenResponse)
async def update_screen(
    screen_id: str,
    screen_data: ScreenUpdate,
    current_user: AdminUser,
    db: DBSession
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
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found"
        )

    # Update fields
    update_data = screen_data.model_dump(exclude_unset=True)

    # If updating pc_id, verify it exists
    if 'pc_id' in update_data:
        pc = db.query(PC).filter(PC.id == update_data['pc_id']).first()
        if not pc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PC with ID '{update_data['pc_id']}' not found"
            )

    for field, value in update_data.items():
        setattr(screen, field, value)

    db.commit()
    db.refresh(screen)

    logger.info(f"Screen '{screen_id}' updated by user {current_user.username}")
    return screen


@router.delete("/{screen_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_screen(
    screen_id: str,
    current_user: AdminUser,
    db: DBSession
):
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
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found"
        )

    db.delete(screen)
    db.commit()

    logger.info(f"Screen '{screen_id}' deleted by user {current_user.username}")


# ==================== View CRUD Endpoints ====================

@router.post("/{screen_id}/views", response_model=ViewResponse, status_code=status.HTTP_201_CREATED)
async def create_view(
    screen_id: str,
    view_data: ViewCreate,
    current_user: AdminUser,
    db: DBSession
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
    # Verify screen exists
    screen = db.query(Screen).filter(Screen.id == screen_id).first()
    if not screen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen with ID '{screen_id}' not found"
        )

    # Check if view with this ID already exists
    existing_view = db.query(View).filter(View.id == view_data.id).first()
    if existing_view:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"View with ID '{view_data.id}' already exists"
        )

    # Check for view_number conflict
    existing_view_number = db.query(View).filter(
        View.screen_id == screen_id,
        View.view_number == view_data.view_number
    ).first()
    if existing_view_number:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"View number {view_data.view_number} already exists for screen '{screen_id}'"
        )

    # Create new view
    new_view = View(
        id=view_data.id,
        screen_id=screen_id,
        name=view_data.name,
        layout_rows=view_data.layout_rows,
        layout_columns=view_data.layout_columns,
        view_number=view_data.view_number
    )

    db.add(new_view)
    db.commit()
    db.refresh(new_view)

    logger.info(f"View '{new_view.id}' created for screen '{screen_id}' by user {current_user.username}")
    return new_view


@router.get("/{screen_id}/views", response_model=List[ViewResponse])
async def list_views(
    screen_id: str,
    current_user: CurrentUser,
    db: DBSession
):
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
            detail=f"Screen with ID '{screen_id}' not found"
        )

    views = db.query(View).filter(View.screen_id == screen_id).order_by(View.view_number).all()

    return [ViewResponse.model_validate(v) for v in views]


@router.get("/views/{view_id}", response_model=ViewDetailResponse)
async def get_view(
    view_id: str,
    current_user: CurrentUser,
    db: DBSession
):
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
            detail=f"View with ID '{view_id}' not found"
        )

    return view


@router.get("/views/{view_id}/with-mappings", response_model=ViewWithMappings)
async def get_view_with_mappings(
    view_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get a view with all its camera mappings.

    All authenticated users can view view details.

    Args:
        view_id: View ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        View with camera mappings

    Raises:
        HTTPException: If view not found
    """
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    # Get mappings
    mappings = db.query(ScreenMapping).filter(ScreenMapping.view_id == view_id).all()

    mapping_infos = []
    for mapping in mappings:
        mapping_info = CameraMappingInfo(
            slot_row=mapping.slot_row,
            slot_col=mapping.slot_col,
            site_id=mapping.site_id,
            camera_id=mapping.camera_id,
            playing_state=mapping.playing_state
        )

        # Add site and camera names
        if mapping.site:
            mapping_info.site_name = mapping.site.name
        if mapping.camera:
            mapping_info.camera_name = mapping.camera.name

        mapping_infos.append(mapping_info)

    result = ViewWithMappings.model_validate(view)
    result.mappings = mapping_infos

    return result


@router.put("/views/{view_id}", response_model=ViewResponse)
async def update_view(
    view_id: str,
    view_data: ViewUpdate,
    current_user: AdminUser,
    db: DBSession
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
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    # Update fields
    update_data = view_data.model_dump(exclude_unset=True)

    # If updating view_number, check for conflicts
    if 'view_number' in update_data:
        existing = db.query(View).filter(
            View.screen_id == view.screen_id,
            View.view_number == update_data['view_number'],
            View.id != view_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"View number {update_data['view_number']} already exists for screen '{view.screen_id}'"
            )

    for field, value in update_data.items():
        setattr(view, field, value)

    db.commit()
    db.refresh(view)

    logger.info(f"View '{view_id}' updated by user {current_user.username}")
    return view


@router.delete("/views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_view(
    view_id: str,
    current_user: AdminUser,
    db: DBSession
):
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
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    db.delete(view)
    db.commit()

    logger.info(f"View '{view_id}' deleted by user {current_user.username}")


# ==================== Screen Mapping Endpoints ====================

@router.post("/views/{view_id}/mappings", response_model=ScreenMappingResponse, status_code=status.HTTP_201_CREATED)
async def create_screen_mapping(
    view_id: str,
    mapping_data: ScreenMappingCreate,
    current_user: AdminUser,
    db: DBSession
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
        HTTPException: If view not found, slot conflict, or camera/site not found
    """
    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    # Validate slot position
    if mapping_data.slot_row > view.layout_rows or mapping_data.slot_row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid slot_row {mapping_data.slot_row}. Must be between 1 and {view.layout_rows}"
        )

    if mapping_data.slot_col > view.layout_columns or mapping_data.slot_col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid slot_col {mapping_data.slot_col}. Must be between 1 and {view.layout_columns}"
        )

    # Check for existing mapping at this slot
    existing_mapping = db.query(ScreenMapping).filter(
        ScreenMapping.view_id == view_id,
        ScreenMapping.slot_row == mapping_data.slot_row,
        ScreenMapping.slot_col == mapping_data.slot_col
    ).first()

    if existing_mapping:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Slot ({mapping_data.slot_row}, {mapping_data.slot_col}) already has a mapping"
        )

    # Verify camera and site if provided
    if mapping_data.camera_id:
        camera = db.query(Camera).filter(Camera.id == mapping_data.camera_id).first()
        if not camera:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Camera with ID '{mapping_data.camera_id}' not found"
            )

    if mapping_data.site_id:
        site = db.query(Site).filter(Site.id == mapping_data.site_id).first()
        if not site:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Site with ID '{mapping_data.site_id}' not found"
            )

    # Create mapping
    new_mapping = ScreenMapping(
        pc_id=view.screen.pc_id,
        screen_id=view.screen_id,
        view_id=view_id,
        slot_row=mapping_data.slot_row,
        slot_col=mapping_data.slot_col,
        site_id=mapping_data.site_id,
        camera_id=mapping_data.camera_id,
        playing_state=mapping_data.playing_state
    )

    db.add(new_mapping)
    db.commit()
    db.refresh(new_mapping)

    logger.info(f"Screen mapping created for view '{view_id}' slot ({mapping_data.slot_row}, {mapping_data.slot_col}) by user {current_user.username}")
    return new_mapping


@router.get("/views/{view_id}/mappings", response_model=List[ScreenMappingResponse])
async def list_view_mappings(
    view_id: str,
    current_user: CurrentUser,
    db: DBSession
):
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
            detail=f"View with ID '{view_id}' not found"
        )

    mappings = db.query(ScreenMapping).filter(ScreenMapping.view_id == view_id).order_by(
        ScreenMapping.slot_row, ScreenMapping.slot_col
    ).all()

    return [ScreenMappingResponse.model_validate(m) for m in mappings]


@router.put("/mappings/{mapping_id}", response_model=ScreenMappingResponse)
async def update_screen_mapping(
    mapping_id: int,
    mapping_data: ScreenMappingUpdate,
    current_user: AdminUser,
    db: DBSession
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
    mapping = db.query(ScreenMapping).filter(ScreenMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen mapping with ID {mapping_id} not found"
        )

    # Update fields
    update_data = mapping_data.model_dump(exclude_unset=True)

    # Verify camera if updating
    if 'camera_id' in update_data and update_data['camera_id']:
        camera = db.query(Camera).filter(Camera.id == update_data['camera_id']).first()
        if not camera:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Camera with ID '{update_data['camera_id']}' not found"
            )

    # Verify site if updating
    if 'site_id' in update_data and update_data['site_id']:
        site = db.query(Site).filter(Site.id == update_data['site_id']).first()
        if not site:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Site with ID '{update_data['site_id']}' not found"
            )

    for field, value in update_data.items():
        setattr(mapping, field, value)

    db.commit()
    db.refresh(mapping)

    logger.info(f"Screen mapping {mapping_id} updated by user {current_user.username}")
    return mapping


@router.delete("/mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_screen_mapping(
    mapping_id: int,
    current_user: AdminUser,
    db: DBSession
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
    mapping = db.query(ScreenMapping).filter(ScreenMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen mapping with ID {mapping_id} not found"
        )

    db.delete(mapping)
    db.commit()

    logger.info(f"Screen mapping {mapping_id} deleted by user {current_user.username}")


# ==================== Additional View and Mapping Endpoints ====================

@router.get("/views/{view_id}/slot/{row}/{col}", response_model=CameraMappingInfo)
async def get_camera_at_slot(
    view_id: str,
    row: int,
    col: int,
    current_user: CurrentUser,
    db: DBSession
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
            detail=f"View with ID '{view_id}' not found"
        )

    # Validate slot position
    if row > view.layout_rows or row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid row {row}. Must be between 1 and {view.layout_rows}"
        )

    if col > view.layout_columns or col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid col {col}. Must be between 1 and {view.layout_columns}"
        )

    # Get mapping for this slot
    mapping = db.query(ScreenMapping).filter(
        ScreenMapping.view_id == view_id,
        ScreenMapping.slot_row == row,
        ScreenMapping.slot_col == col
    ).first()

    if not mapping:
        # Return empty slot info
        return CameraMappingInfo(
            slot_row=row,
            slot_col=col,
            site_id=None,
            site_name=None,
            camera_id=None,
            camera_name=None,
            playing_state=False
        )

    # Return mapping info with site and camera names
    mapping_info = CameraMappingInfo(
        slot_row=mapping.slot_row,
        slot_col=mapping.slot_col,
        site_id=mapping.site_id,
        camera_id=mapping.camera_id,
        playing_state=mapping.playing_state
    )

    if mapping.site:
        mapping_info.site_name = mapping.site.name
    if mapping.camera:
        mapping_info.camera_name = mapping.camera.name

    return mapping_info


@router.patch("/views/{view_id}/rename")
async def rename_view(
    view_id: str,
    new_name: str = Query(..., min_length=1, max_length=50, description="New view name"),
    current_user: AdminUser = None,
    db: DBSession = None
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
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    old_name = view.name
    view.name = new_name

    db.commit()
    db.refresh(view)

    logger.info(f"View '{view_id}' renamed from '{old_name}' to '{new_name}' by user {current_user.username}")
    return ViewResponse.model_validate(view)


@router.get("/pc/{pc_id}/all-views", response_model=List[ScreenLayoutResponse])
async def get_all_views_for_pc(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get all views with mappings for all screens of a PC.

    All authenticated users can view this information.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List of screen layouts with views and camera mappings

    Raises:
        HTTPException: If PC not found
    """
    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    # Get all screens for this PC
    screens = db.query(Screen).filter(Screen.pc_id == pc_id).order_by(Screen.name).all()

    results = []
    for screen in screens:
        # Get views with mappings for each screen
        views = db.query(View).filter(View.screen_id == screen.id).order_by(View.view_number).all()

        views_with_mappings = []
        for view in views:
            # Get mappings for this view
            mappings = db.query(ScreenMapping).filter(ScreenMapping.view_id == view.id).all()

            mapping_infos = []
            for mapping in mappings:
                mapping_info = CameraMappingInfo(
                    slot_row=mapping.slot_row,
                    slot_col=mapping.slot_col,
                    site_id=mapping.site_id,
                    camera_id=mapping.camera_id,
                    playing_state=mapping.playing_state
                )

                # Add site and camera names
                if mapping.site:
                    mapping_info.site_name = mapping.site.name
                if mapping.camera:
                    mapping_info.camera_name = mapping.camera.name

                mapping_infos.append(mapping_info)

            view_with_mappings = ViewWithMappings.model_validate(view)
            view_with_mappings.mappings = mapping_infos
            views_with_mappings.append(view_with_mappings)

        # Build screen layout response
        screen_layout = ScreenLayoutResponse.model_validate(screen)
        if screen.pc:
            screen_layout.pc = PCInfo.model_validate(screen.pc)
        screen_layout.views = views_with_mappings
        screen_layout.view_count = len(views_with_mappings)

        results.append(screen_layout)

    return results


@router.delete("/views/{view_id}/slot/{row}/{col}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_slot(
    view_id: str,
    row: int,
    col: int,
    current_user: AdminUser,
    db: DBSession
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
    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    # Get mapping for this slot
    mapping = db.query(ScreenMapping).filter(
        ScreenMapping.view_id == view_id,
        ScreenMapping.slot_row == row,
        ScreenMapping.slot_col == col
    ).first()

    if not mapping:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No camera assigned to slot ({row}, {col}) in view '{view_id}'"
        )

    db.delete(mapping)
    db.commit()

    logger.info(f"Slot ({row}, {col}) cleared in view '{view_id}' by user {current_user.username}")


@router.put("/views/{view_id}/slot/{row}/{col}")
async def assign_camera_to_slot(
    view_id: str,
    row: int,
    col: int,
    camera_id: str = Query(..., description="Camera ID to assign"),
    site_id: str = Query(None, description="Site ID (optional)"),
    playing_state: bool = Query(False, description="Playing state"),
    current_user: AdminUser = None,
    db: DBSession = None
):
    """
    Assign or update camera in a specific slot.

    Only admins and super admins can assign cameras.

    Args:
        view_id: View ID
        row: Slot row (1-indexed)
        col: Slot column (1-indexed)
        camera_id: Camera ID to assign
        site_id: Optional site ID
        playing_state: Playing state
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Screen mapping

    Raises:
        HTTPException: If view or camera not found, or slot out of bounds
    """
    # Verify view exists
    view = db.query(View).filter(View.id == view_id).first()
    if not view:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"View with ID '{view_id}' not found"
        )

    # Validate slot position
    if row > view.layout_rows or row < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid row {row}. Must be between 1 and {view.layout_rows}"
        )

    if col > view.layout_columns or col < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid col {col}. Must be between 1 and {view.layout_columns}"
        )

    # Verify camera exists
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if not camera:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera with ID '{camera_id}' not found"
        )

    # Verify site if provided
    if site_id:
        site = db.query(Site).filter(Site.id == site_id).first()
        if not site:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Site with ID '{site_id}' not found"
            )

    # Check if mapping already exists for this slot
    existing_mapping = db.query(ScreenMapping).filter(
        ScreenMapping.view_id == view_id,
        ScreenMapping.slot_row == row,
        ScreenMapping.slot_col == col
    ).first()

    if existing_mapping:
        # Update existing mapping
        existing_mapping.camera_id = camera_id
        existing_mapping.site_id = site_id
        existing_mapping.playing_state = playing_state
        db.commit()
        db.refresh(existing_mapping)
        logger.info(f"Camera '{camera_id}' assigned to slot ({row}, {col}) in view '{view_id}' by user {current_user.username}")
        return ScreenMappingResponse.model_validate(existing_mapping)
    else:
        # Create new mapping
        new_mapping = ScreenMapping(
            pc_id=view.screen.pc_id,
            screen_id=view.screen_id,
            view_id=view_id,
            slot_row=row,
            slot_col=col,
            site_id=site_id,
            camera_id=camera_id,
            playing_state=playing_state
        )
        db.add(new_mapping)
        db.commit()
        db.refresh(new_mapping)
        logger.info(f"Camera '{camera_id}' assigned to slot ({row}, {col}) in view '{view_id}' by user {current_user.username}")
        return ScreenMappingResponse.model_validate(new_mapping)
