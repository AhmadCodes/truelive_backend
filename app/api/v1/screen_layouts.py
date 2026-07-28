"""
Screen Layout management API endpoints.

A ScreenLayout is the owner that sits between PCs and Screens. Screens belong to
a layout; PCs point at a single layout (nullable) to resolve their screen
configuration. Multiple PCs can share one layout.

Provides CRUD for layouts, PC assignment/unassignment, and deployment of a
layout's configuration to its assigned PCs.
"""

from fastapi import APIRouter, HTTPException, status, Query, Depends
from sqlalchemy import or_
from sqlalchemy.orm import joinedload
from typing import List, Optional
from pydantic import BaseModel, Field
import logging

from app.api.deps import AdminUser, DBSession, CurrentUser, admin_or_scope
from app.models.screen_layout import ScreenLayout
from app.models.pc import PC
from app.models.team import Team
from app.schemas.screen_layout import (
    ScreenLayoutCreate,
    ScreenLayoutUpdate,
    ScreenLayoutResponse,
    AssignedPCsResponse,
    DeployRequest,
)
from app.services.team_enforcement import (
    cameras_blocking_layout_move,
    MSG_PC_LAYOUT_DIFFERENT_TEAM,
    MSG_LAYOUT_MOVE_CAMERAS,
)
from app.api.v1.pcs import _deploy_config_to_pcs
from app.services.actor import (
    principal_to_actor,
    stamp_created,
    stamp_updated,
    snapshot,
    attach_actor_stamps,
    attach_actor_stamps_list,
)
from app.services import audit_service
from app.services.audit_service import ResourceType

logger = logging.getLogger(__name__)
router = APIRouter()


class PCAssignmentRequest(BaseModel):
    """Request body for assigning/unassigning PCs to a screen layout."""

    pc_ids: List[str] = Field(..., description="PC IDs to assign or unassign")


@router.post(
    "", response_model=ScreenLayoutResponse, status_code=status.HTTP_201_CREATED
)
async def create_screen_layout(
    layout_data: ScreenLayoutCreate,
    db: DBSession,
    principal=Depends(admin_or_scope("teams:manage")),
):
    """
    Create a new screen layout.

    Only admins and super admins can create screen layouts.

    Raises:
        HTTPException 409: If a layout with this ID already exists
    """
    actor = principal_to_actor(principal)

    existing = db.query(ScreenLayout).filter(ScreenLayout.id == layout_data.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Screen layout with ID '{layout_data.id}' already exists",
        )

    if not db.query(Team).filter(Team.id == layout_data.team_id).first():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team '{layout_data.team_id}' not found",
        )

    layout = ScreenLayout(
        id=layout_data.id, name=layout_data.name, team_id=layout_data.team_id
    )
    db.add(layout)

    stamp_created(layout, actor)
    audit_service.record_create(
        db, resource_type=ResourceType.LAYOUT, resource_id=layout.id, actor=actor
    )

    db.commit()
    db.refresh(layout)

    logger.info(f"Screen layout '{layout.id}' created")
    return layout


@router.get("", response_model=List[ScreenLayoutResponse])
async def list_screen_layouts(
    current_user: CurrentUser,
    db: DBSession,
    search: Optional[str] = Query(None, description="Search by layout name"),
):
    """
    List all screen layouts with an optional name search.

    All authenticated users can view screen layouts.
    """
    query = db.query(ScreenLayout).options(joinedload(ScreenLayout.team))

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(or_(ScreenLayout.name.ilike(search_pattern)))

    layouts = query.order_by(ScreenLayout.name).all()
    responses = [ScreenLayoutResponse.model_validate(l) for l in layouts]
    attach_actor_stamps_list(db, responses, layouts)
    return responses


@router.get("/{layout_id}", response_model=ScreenLayoutResponse)
async def get_screen_layout(layout_id: str, current_user: CurrentUser, db: DBSession):
    """
    Get a specific screen layout by ID.

    All authenticated users can view screen layout details.

    Raises:
        HTTPException 404: If the layout is not found
    """
    layout = (
        db.query(ScreenLayout)
        .options(joinedload(ScreenLayout.team))
        .filter(ScreenLayout.id == layout_id)
        .first()
    )
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    resp = ScreenLayoutResponse.model_validate(layout)
    attach_actor_stamps(db, resp, layout)
    return resp


@router.put("/{layout_id}", response_model=ScreenLayoutResponse)
async def update_screen_layout(
    layout_id: str,
    layout_data: ScreenLayoutUpdate,
    db: DBSession,
    principal=Depends(admin_or_scope("teams:manage")),
):
    """
    Update a screen layout's name.

    Only admins and super admins can update screen layouts.

    Raises:
        HTTPException 404: If the layout is not found
    """
    actor = principal_to_actor(principal)

    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    before = snapshot(layout)

    update_data = layout_data.model_dump(exclude_unset=True)

    # Moving a layout to another team is blocked while it still contains cameras
    # from locations that aren't part of the target team (AC-16).
    new_team_id = update_data.get("team_id")
    if new_team_id is not None and new_team_id != layout.team_id:
        if not db.query(Team).filter(Team.id == new_team_id).first():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team '{new_team_id}' not found",
            )
        blocking = cameras_blocking_layout_move(db, layout_id, new_team_id)
        if blocking:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=MSG_LAYOUT_MOVE_CAMERAS,
            )

    for field, value in update_data.items():
        setattr(layout, field, value)

    stamp_updated(layout, actor)
    audit_service.record_update(
        db, resource_type=ResourceType.LAYOUT, resource_id=layout.id, actor=actor,
        before=before, after=snapshot(layout),
    )

    db.commit()
    db.refresh(layout)

    logger.info(f"Screen layout '{layout_id}' updated")
    return layout


@router.delete("/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_screen_layout(layout_id: str, current_user: AdminUser, db: DBSession):
    """
    Delete a screen layout.

    Owned screens, views, and mappings are removed by ORM cascade; PCs assigned
    to this layout have their pointer cleared (SET NULL) by the database.

    Only admins and super admins can delete screen layouts.

    Raises:
        HTTPException 404: If the layout is not found
    """
    actor = principal_to_actor(current_user)

    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    snap = snapshot(layout)

    db.delete(layout)

    audit_service.record_delete(
        db, resource_type=ResourceType.LAYOUT, resource_id=layout_id, actor=actor, snapshot=snap
    )

    db.commit()

    logger.info(f"Screen layout '{layout_id}' deleted by user {current_user.username}")


@router.get("/{layout_id}/assigned-pcs", response_model=AssignedPCsResponse)
async def get_assigned_pcs(layout_id: str, current_user: CurrentUser, db: DBSession):
    """
    List the PCs currently assigned to a screen layout.

    All authenticated users can view a layout's assigned PCs.

    Raises:
        HTTPException 404: If the layout is not found
    """
    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    pcs = (
        db.query(PC)
        .options(joinedload(PC.team))
        .filter(PC.screen_layout_id == layout_id)
        .order_by(PC.name)
        .all()
    )

    return AssignedPCsResponse(screen_layout_id=layout_id, pcs=pcs)


@router.post("/{layout_id}/assign")
async def assign_pcs(
    layout_id: str, request: PCAssignmentRequest, current_user: AdminUser, db: DBSession
):
    """
    Assign PCs to a screen layout (set their ``screen_layout_id``).

    Validates the layout and every PC id before mutating anything.

    Only admins and super admins can assign PCs.

    Raises:
        HTTPException 404: If the layout or any PC is not found
    """
    actor = principal_to_actor(current_user)

    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    # Validate all PCs exist and share the layout's team first, so the operation
    # is all-or-nothing.
    pcs = []
    for pc_id in request.pc_ids:
        pc = db.query(PC).filter(PC.id == pc_id).first()
        if not pc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PC with ID '{pc_id}' not found",
            )
        # Team boundary: a layout may only be assigned to PCs in its own team.
        if pc.team_id != layout.team_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=MSG_PC_LAYOUT_DIFFERENT_TEAM,
            )
        pcs.append(pc)

    for pc in pcs:
        pc.screen_layout_id = layout_id
        stamp_updated(pc, actor)

    if pcs:
        audit_service.record_update(
            db, resource_type=ResourceType.LAYOUT, resource_id=layout_id, actor=actor,
            before={}, after={}, extra={"pcs_assigned": [pc.id for pc in pcs]},
        )

    db.commit()

    logger.info(
        f"Assigned {len(pcs)} PC(s) to screen layout '{layout_id}' "
        f"by user {current_user.username}"
    )

    return {
        "screen_layout_id": layout_id,
        "assigned_pc_ids": [pc.id for pc in pcs],
        "message": f"Assigned {len(pcs)} PC(s) to screen layout '{layout_id}'",
    }


@router.post("/{layout_id}/unassign")
async def unassign_pcs(
    layout_id: str, request: PCAssignmentRequest, current_user: AdminUser, db: DBSession
):
    """
    Unassign PCs from a screen layout.

    Only PCs currently pointing at this layout are cleared (set to null); PCs
    assigned to a different layout are left untouched.

    Only admins and super admins can unassign PCs.

    Raises:
        HTTPException 404: If the layout or any PC is not found
    """
    actor = principal_to_actor(current_user)

    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    unassigned = []
    for pc_id in request.pc_ids:
        pc = db.query(PC).filter(PC.id == pc_id).first()
        if not pc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PC with ID '{pc_id}' not found",
            )
        if pc.screen_layout_id == layout_id:
            pc.screen_layout_id = None
            stamp_updated(pc, actor)
            unassigned.append(pc_id)

    if unassigned:
        audit_service.record_update(
            db, resource_type=ResourceType.LAYOUT, resource_id=layout_id, actor=actor,
            before={}, after={}, extra={"pcs_unassigned": unassigned},
        )

    db.commit()

    logger.info(
        f"Unassigned {len(unassigned)} PC(s) from screen layout '{layout_id}' "
        f"by user {current_user.username}"
    )

    return {
        "screen_layout_id": layout_id,
        "unassigned_pc_ids": unassigned,
        "message": f"Unassigned {len(unassigned)} PC(s) from screen layout '{layout_id}'",
    }


@router.post("/{layout_id}/deploy")
async def deploy_screen_layout(
    layout_id: str, request: DeployRequest, current_user: AdminUser, db: DBSession
):
    """
    Deploy a screen layout's configuration to its assigned PCs.

    Targets default to every PC assigned to the layout. When ``pc_ids`` is
    provided, every id is pre-flight validated to be currently assigned to THIS
    layout; if any is not, the request is rejected before anything is sent.

    Only admins and super admins can deploy screen layouts.

    Raises:
        HTTPException 404: If the layout is not found
        HTTPException 400: If a requested PC is not assigned to this layout
    """
    layout = db.query(ScreenLayout).filter(ScreenLayout.id == layout_id).first()
    if not layout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Screen layout with ID '{layout_id}' not found",
        )

    assigned = (
        db.query(PC).filter(PC.screen_layout_id == layout_id).order_by(PC.name).all()
    )
    assigned_ids = {pc.id for pc in assigned}

    if request.pc_ids is not None:
        invalid = [pid for pid in request.pc_ids if pid not in assigned_ids]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "detail": f"Some PCs are not assigned to screen layout '{layout_id}'",
                    "invalid_pc_ids": invalid,
                },
            )
        requested = set(request.pc_ids)
        targets = [pc for pc in assigned if pc.id in requested]
    else:
        targets = assigned

    result = _deploy_config_to_pcs(targets, db)
    result["screen_layout_id"] = layout_id
    if result["total"] == 0:
        result["message"] = "No PCs are assigned to this screen layout"

    logger.info(
        f"Deployed screen layout '{layout_id}' to {result['deployed']} PC(s) "
        f"by user {current_user.username}"
    )

    return result
