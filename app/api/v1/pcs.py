"""
PC management API endpoints.
Provides CRUD operations for PCs and their associations with screens.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy import func, or_
from typing import List, Optional

from app.api.deps import AdminUser, DBSession, CurrentUser
from app.models.pc import PC
from app.models.screen import Screen
from app.schemas.pc import (
    PCCreate,
    PCUpdate,
    PCResponse,
    PCDetailResponse,
    PCWithScreenCount,
    PCWithManager,
    PCWithControlled,
    PCWithScreens,
    ScreenSummary
)
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=PCResponse, status_code=status.HTTP_201_CREATED)
async def create_pc(
    pc_data: PCCreate,
    current_user: AdminUser,
    db: DBSession
):
    """
    Create a new PC.

    Only admins and super admins can create PCs.

    Args:
        pc_data: PC creation data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Created PC

    Raises:
        HTTPException: If PC ID already exists or manager PC not found
    """
    # Check if PC with this ID already exists
    existing_pc = db.query(PC).filter(PC.id == pc_data.id).first()
    if existing_pc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PC with ID '{pc_data.id}' already exists"
        )

    # If manager_id is provided, verify it exists
    if pc_data.manager_id:
        manager = db.query(PC).filter(PC.id == pc_data.manager_id).first()
        if not manager:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manager PC with ID '{pc_data.manager_id}' not found"
            )
        if manager.role != 'manager':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PC '{pc_data.manager_id}' is not a manager PC"
            )

    # Create new PC
    new_pc = PC(
        id=pc_data.id,
        name=pc_data.name,
        ip_address=pc_data.ip_address,
        gpu_type=pc_data.gpu_type,
        role=pc_data.role,
        manager_id=pc_data.manager_id
    )

    db.add(new_pc)
    db.commit()
    db.refresh(new_pc)

    logger.info(f"PC '{new_pc.id}' created by user {current_user.username}")
    return new_pc


@router.get("", response_model=List[PCWithScreenCount])
async def list_pcs(
    current_user: CurrentUser,
    db: DBSession,
    role: Optional[str] = Query(None, description="Filter by role (controller or manager)"),
    manager_id: Optional[str] = Query(None, description="Filter by manager PC ID"),
    search: Optional[str] = Query(None, description="Search by name or IP address")
):
    """
    List all PCs with optional filters.

    All authenticated users can view PCs.

    Args:
        current_user: Current authenticated user
        db: Database session
        role: Optional role filter
        manager_id: Optional manager ID filter
        search: Optional search term for name or IP

    Returns:
        List of PCs with screen counts
    """
    query = db.query(PC)

    # Apply filters
    if role:
        query = query.filter(PC.role == role)

    if manager_id:
        query = query.filter(PC.manager_id == manager_id)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                PC.name.ilike(search_pattern),
                PC.ip_address.ilike(search_pattern)
            )
        )

    pcs = query.order_by(PC.name).all()

    # Add screen count to each PC
    result = []
    for pc in pcs:
        screen_count = db.query(func.count(Screen.id)).filter(Screen.pc_id == pc.id).scalar() or 0
        pc_data = PCWithScreenCount.model_validate(pc)
        pc_data.screen_count = screen_count
        result.append(pc_data)

    return result


@router.get("/count")
async def get_pc_count(
    current_user: CurrentUser,
    db: DBSession,
    role: Optional[str] = Query(None, description="Filter by role (controller or manager)")
):
    """
    Get count of PCs.

    All authenticated users can view PC counts.

    Args:
        current_user: Current authenticated user
        db: Database session
        role: Optional role filter

    Returns:
        PC count statistics
    """
    query = db.query(func.count(PC.id))

    if role:
        query = query.filter(PC.role == role)

    total_count = query.scalar() or 0

    # Get counts by role
    controller_count = db.query(func.count(PC.id)).filter(PC.role == 'controller').scalar() or 0
    manager_count = db.query(func.count(PC.id)).filter(PC.role == 'manager').scalar() or 0

    return {
        "total": total_count,
        "controllers": controller_count,
        "managers": manager_count
    }


@router.get("/{pc_id}", response_model=PCDetailResponse)
async def get_pc(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get a specific PC by ID.

    All authenticated users can view PC details.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        PC details

    Raises:
        HTTPException: If PC not found
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    return pc


@router.get("/{pc_id}/with-manager", response_model=PCWithManager)
async def get_pc_with_manager(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get a PC with its manager information.

    All authenticated users can view PC details.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        PC with manager information

    Raises:
        HTTPException: If PC not found
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    return PCWithManager.model_validate(pc)


@router.get("/{pc_id}/controlled", response_model=PCWithControlled)
async def get_pc_with_controlled(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get a manager PC with its controlled PCs.

    All authenticated users can view PC details.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Manager PC with controlled PCs

    Raises:
        HTTPException: If PC not found or not a manager
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    if pc.role != 'manager':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PC '{pc_id}' is not a manager PC"
        )

    # Get screen count
    screen_count = db.query(func.count(Screen.id)).filter(Screen.pc_id == pc.id).scalar() or 0

    result = PCWithControlled.model_validate(pc)
    result.screen_count = screen_count

    return result


@router.get("/{pc_id}/screens", response_model=PCWithScreens)
async def get_pc_screens(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession
):
    """
    Get a PC with all its screens.

    All authenticated users can view PC screens.

    Args:
        pc_id: PC ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        PC with screen details

    Raises:
        HTTPException: If PC not found
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    # Get screens
    screens = db.query(Screen).filter(Screen.pc_id == pc_id).order_by(Screen.name).all()

    # Convert to response format
    screen_summaries = [ScreenSummary.model_validate(screen) for screen in screens]

    result = PCWithScreens.model_validate(pc)
    result.screen_count = len(screen_summaries)
    result.screens = screen_summaries

    return result


@router.put("/{pc_id}", response_model=PCResponse)
async def update_pc(
    pc_id: str,
    pc_data: PCUpdate,
    current_user: AdminUser,
    db: DBSession
):
    """
    Update a PC.

    Only admins and super admins can update PCs.

    Args:
        pc_id: PC ID
        pc_data: PC update data
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Updated PC

    Raises:
        HTTPException: If PC not found or validation fails
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    # Update fields
    update_data = pc_data.model_dump(exclude_unset=True)

    # If updating manager_id, verify it exists
    if 'manager_id' in update_data and update_data['manager_id']:
        manager = db.query(PC).filter(PC.id == update_data['manager_id']).first()
        if not manager:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manager PC with ID '{update_data['manager_id']}' not found"
            )
        if manager.role != 'manager':
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PC '{update_data['manager_id']}' is not a manager PC"
            )

    # Validate role change
    if 'role' in update_data:
        new_role = update_data['role']
        # If changing to manager, clear manager_id
        if new_role == 'manager' and pc.manager_id:
            pc.manager_id = None
        # If changing to controller and no manager_id in update, it's ok (can be null)

    for field, value in update_data.items():
        setattr(pc, field, value)

    db.commit()
    db.refresh(pc)

    logger.info(f"PC '{pc_id}' updated by user {current_user.username}")
    return pc


@router.delete("/{pc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pc(
    pc_id: str,
    current_user: AdminUser,
    db: DBSession
):
    """
    Delete a PC.

    Only admins and super admins can delete PCs.
    All associated screens will be deleted due to cascade.

    Args:
        pc_id: PC ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Raises:
        HTTPException: If PC not found
    """
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found"
        )

    # Check if this is a manager PC with controlled PCs
    controlled_count = db.query(func.count(PC.id)).filter(PC.manager_id == pc_id).scalar() or 0
    if controlled_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete manager PC '{pc_id}' with {controlled_count} controlled PCs. "
                   f"Reassign or delete controlled PCs first."
        )

    db.delete(pc)
    db.commit()

    logger.info(f"PC '{pc_id}' deleted by user {current_user.username}")
