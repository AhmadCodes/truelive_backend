"""
PC management API endpoints.
Provides CRUD operations for PCs and their associations with screens.
"""

from fastapi import APIRouter, HTTPException, status, Query, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from typing import List, Optional
import httpx
import time

from app.api.deps import AdminUser, DBSession, CurrentUser, admin_or_scope
from app.models.pc import PC
from app.models.team import Team
from app.models.screen import Screen
from app.services.team_enforcement import (
    MSG_PC_LAYOUT_DIFFERENT_TEAM,
    MSG_PC_MOVE_HOLDS_LAYOUT,
)
from app.schemas.pc import (
    PCCreate,
    PCUpdate,
    PCResponse,
    PCDetailResponse,
    PCWithScreenCount,
    PCWithManager,
    PCWithControlled,
    PCWithScreens,
    ScreenSummary,
    ConfigurePCScreensRequest,
    ConfigurePCScreensResponse,
    PCTokenResponse,
    PCConnectionStatus,
    AllPCsConnectionStatus,
    ImportConfigRequest,
    ImportConfigResponse,
    CopyLayoutResponse,
)
from app.schemas.deploy import PCDeployResult
from app.schemas.pc_status import DeploymentHistory, DeploymentSummary, PCHealthDetail
from app.services.config_importer import import_config_for_pc, copy_layout_from_pc
from app.services.actor import (
    principal_to_actor,
    stamp_created,
    stamp_updated,
    snapshot,
    attach_actor_stamps,
    attach_actor_stamps_list,
)
from app.services import audit_service, pc_status, pc_tokens
from app.services.audit_service import ResourceType
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

# Roles that may read a PC's relay token (the AdminUser roles, app/api/deps.py).
TOKEN_VISIBLE_ROLES = ("admin", "super_admin")


def _hide_token_unless_admin(resp, user) -> None:
    """NEW-40 (D12): a PC token lets its holder register as that PC on the
    relay, so only admins see it. Everyone else gets ``auth_token = None``;
    ``token_expiry`` stays visible."""
    if getattr(user, "role", None) not in TOKEN_VISIBLE_ROLES:
        resp.auth_token = None


@router.post("", response_model=PCResponse, status_code=status.HTTP_201_CREATED)
async def create_pc(
    pc_data: PCCreate,
    db: DBSession,
    principal=Depends(admin_or_scope("teams:manage")),
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
    actor = principal_to_actor(principal)

    # Check if PC with this ID already exists
    existing_pc = db.query(PC).filter(PC.id == pc_data.id).first()
    if existing_pc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PC with ID '{pc_data.id}' already exists",
        )

    # If manager_id is provided, verify it exists
    if pc_data.manager_id:
        manager = db.query(PC).filter(PC.id == pc_data.manager_id).first()
        if not manager:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manager PC with ID '{pc_data.manager_id}' not found",
            )
        if manager.role != "manager":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PC '{pc_data.manager_id}' is not a manager PC",
            )

    # Validate the team exists (every PC belongs to exactly one team).
    if not db.query(Team).filter(Team.id == pc_data.team_id).first():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team '{pc_data.team_id}' not found",
        )

    # Create new PC
    new_pc = PC(
        id=pc_data.id,
        name=pc_data.name,
        ip_address=pc_data.ip_address,
        gpu_type=pc_data.gpu_type,
        role=pc_data.role,
        manager_id=pc_data.manager_id,
        team_id=pc_data.team_id,
    )

    db.add(new_pc)
    db.flush()

    stamp_created(new_pc, actor)
    audit_service.record_create(
        db, resource_type=ResourceType.PC, resource_id=new_pc.id, actor=actor
    )

    db.commit()
    db.refresh(new_pc)

    logger.info(f"PC '{new_pc.id}' created")
    return new_pc


@router.get("", response_model=List[PCWithScreenCount])
async def list_pcs(
    current_user: CurrentUser,
    db: DBSession,
    role: Optional[str] = Query(
        None, description="Filter by role (controller or manager)"
    ),
    manager_id: Optional[str] = Query(None, description="Filter by manager PC ID"),
    search: Optional[str] = Query(None, description="Search by name or IP address"),
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
    query = db.query(PC).options(joinedload(PC.team))

    # Apply filters
    if role:
        query = query.filter(PC.role == role)

    if manager_id:
        query = query.filter(PC.manager_id == manager_id)

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(PC.name.ilike(search_pattern), PC.ip_address.ilike(search_pattern))
        )

    pcs = query.order_by(PC.name).all()

    # Add screen count to each PC
    result = []
    for pc in pcs:
        screen_count = 0
        if pc.screen_layout_id:
            screen_count = (
                db.query(func.count(Screen.id))
                .filter(Screen.screen_layout_id == pc.screen_layout_id)
                .scalar()
                or 0
            )
        pc_data = PCWithScreenCount.model_validate(pc)
        pc_data.screen_count = screen_count
        _hide_token_unless_admin(pc_data, current_user)
        result.append(pc_data)

    attach_actor_stamps_list(db, result, pcs)

    return result


@router.get("/count")
async def get_pc_count(
    current_user: CurrentUser,
    db: DBSession,
    role: Optional[str] = Query(
        None, description="Filter by role (controller or manager)"
    ),
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
    controller_count = (
        db.query(func.count(PC.id)).filter(PC.role == "controller").scalar() or 0
    )
    manager_count = (
        db.query(func.count(PC.id)).filter(PC.role == "manager").scalar() or 0
    )

    return {
        "total": total_count,
        "controllers": controller_count,
        "managers": manager_count,
    }


def _connection_status(pc: PC, health, deploy, ws_info: Optional[dict], now: float) -> PCConnectionStatus:
    """One PC's status (D16): presence from the relay and pcs, the latest
    station status and the latest deploy. Timestamps are aware UTC (NEW-51)."""
    is_connected = ws_info is not None
    return PCConnectionStatus(
        pc_id=pc.id,
        pc_name=pc.name,
        is_connected=is_connected,
        last_connected=pc_status.utc_from_epoch(pc.last_connected),
        last_seen=pc_status.utc_from_epoch(pc.last_seen),
        last_applied=pc_status.utc_from_epoch(pc.last_applied),
        websocket_connected_at=pc_status.normalize_iso(ws_info.get("connected_at")) if is_connected else None,
        health=pc_status.health_summary(health, now),
        last_deploy=pc_status.deployment_summary(deploy),
    )


@router.get("/status", response_model=AllPCsConnectionStatus)
async def get_all_pcs_connection_status(current_user: CurrentUser, db: DBSession):
    """
    Get connection status for all PCs.

    Returns:
    - Total number of PCs in database
    - Number of currently connected PCs
    - Number of disconnected PCs
    - List of all PCs with their connection status, their latest station
      status (``health``, from pc_health) and their latest deploy
      (``last_deploy``), each null when there is none (D16)

    The connection status is determined by querying the WebSocket server
    for real-time connection information and combining it with database
    information (last_connected, last_applied timestamps). PCs, health and
    latest deploys come from one query. Timestamps are UTC with an offset.
    """
    rows = pc_status.status_rows(db)

    # Get currently connected PCs from WebSocket server
    ws_data = await get_websocket_connected_pcs()
    connected_pc_ids = {pc["pc_id"]: pc for pc in ws_data.get("connected_pcs", [])}

    now = time.time()
    pc_statuses = [_connection_status(pc, health, deploy, connected_pc_ids.get(pc.id), now)
                   for pc, health, deploy in rows]

    # Calculate counts
    connected_count = sum(1 for pc in pc_statuses if pc.is_connected)
    disconnected_count = len(pc_statuses) - connected_count

    return AllPCsConnectionStatus(
        total_pcs=len(pc_statuses),
        connected_count=connected_count,
        disconnected_count=disconnected_count,
        pcs=pc_statuses,
    )


# Declared before every /{pc_id}/... route, so that a config_id such as
# "status" or "health" never resolves to a PC route (D16).
@router.get("/deployments/{config_id}", response_model=DeploymentSummary)
def get_deployment(config_id: str, current_user: CurrentUser, db: DBSession):
    """One deploy by its config_id (D16). No layout body, token or URL."""
    from app.models.pc_tracking import PcDeployment

    row = db.query(PcDeployment).filter(PcDeployment.id == config_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return pc_status.deployment_summary(row)


@router.get("/{pc_id}", response_model=PCDetailResponse)
async def get_pc(pc_id: str, current_user: CurrentUser, db: DBSession):
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
    pc = db.query(PC).options(joinedload(PC.team)).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    resp = PCDetailResponse.model_validate(pc)
    _hide_token_unless_admin(resp, current_user)
    attach_actor_stamps(db, resp, pc)
    return resp


@router.get("/{pc_id}/with-manager", response_model=PCWithManager)
async def get_pc_with_manager(pc_id: str, current_user: CurrentUser, db: DBSession):
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
            detail=f"PC with ID '{pc_id}' not found",
        )

    resp = PCWithManager.model_validate(pc)
    attach_actor_stamps(db, resp, pc)
    return resp


@router.get("/{pc_id}/controlled", response_model=PCWithControlled)
async def get_pc_with_controlled(pc_id: str, current_user: CurrentUser, db: DBSession):
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
            detail=f"PC with ID '{pc_id}' not found",
        )

    if pc.role != "manager":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PC '{pc_id}' is not a manager PC",
        )

    # Get screen count via the PC's assigned screen layout
    screen_count = 0
    if pc.screen_layout_id:
        screen_count = (
            db.query(func.count(Screen.id))
            .filter(Screen.screen_layout_id == pc.screen_layout_id)
            .scalar()
            or 0
        )

    result = PCWithControlled.model_validate(pc)
    result.screen_count = screen_count
    attach_actor_stamps(db, result, pc)

    return result


@router.get("/{pc_id}/screens", response_model=PCWithScreens)
async def get_pc_screens(pc_id: str, current_user: CurrentUser, db: DBSession):
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
            detail=f"PC with ID '{pc_id}' not found",
        )

    # Get screens via the PC's assigned screen layout
    screens = []
    if pc.screen_layout_id:
        screens = (
            db.query(Screen)
            .filter(Screen.screen_layout_id == pc.screen_layout_id)
            .order_by(Screen.name)
            .all()
        )

    # Convert to response format
    screen_summaries = [ScreenSummary.model_validate(screen) for screen in screens]

    result = PCWithScreens.model_validate(pc)
    result.screen_count = len(screen_summaries)
    result.screens = screen_summaries
    _hide_token_unless_admin(result, current_user)
    attach_actor_stamps(db, result, pc)

    return result


@router.put("/{pc_id}", response_model=PCResponse)
async def update_pc(
    pc_id: str,
    pc_data: PCUpdate,
    db: DBSession,
    principal=Depends(admin_or_scope("teams:manage")),
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
    actor = principal_to_actor(principal)

    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    before = snapshot(pc)

    # Update fields
    update_data = pc_data.model_dump(exclude_unset=True)

    # If updating manager_id, verify it exists
    if "manager_id" in update_data and update_data["manager_id"]:
        manager = db.query(PC).filter(PC.id == update_data["manager_id"]).first()
        if not manager:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Manager PC with ID '{update_data['manager_id']}' not found",
            )
        if manager.role != "manager":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"PC '{update_data['manager_id']}' is not a manager PC",
            )

    # Validate a team change (every PC belongs to exactly one team).
    if "team_id" in update_data and update_data["team_id"] != pc.team_id:
        if not db.query(Team).filter(Team.id == update_data["team_id"]).first():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Team '{update_data['team_id']}' not found",
            )

    # Resolve the PC's post-update team and layout, then enforce that the two
    # stay in the same team. This single check covers both assigning a layout to
    # a PC (AC-14) and moving a PC that still holds a layout (AC-17).
    effective_team = update_data.get("team_id", pc.team_id)
    if "screen_layout_id" in update_data:
        effective_layout_id = update_data["screen_layout_id"]
        layout_is_new = bool(effective_layout_id)
    else:
        effective_layout_id = pc.screen_layout_id
        layout_is_new = False

    if effective_layout_id:
        from app.models.screen_layout import ScreenLayout

        layout = (
            db.query(ScreenLayout)
            .filter(ScreenLayout.id == effective_layout_id)
            .first()
        )
        if not layout:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Screen layout with ID '{effective_layout_id}' not found",
            )
        if layout.team_id != effective_team:
            # A newly-provided layout => an assignment; otherwise the PC is being
            # moved to a team that doesn't match the layout it still holds.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    MSG_PC_LAYOUT_DIFFERENT_TEAM
                    if layout_is_new
                    else MSG_PC_MOVE_HOLDS_LAYOUT
                ),
            )

    # Validate role change
    if "role" in update_data:
        new_role = update_data["role"]
        # If changing to manager, clear manager_id
        if new_role == "manager" and pc.manager_id:
            pc.manager_id = None
        # If changing to controller and no manager_id in update, it's ok (can be null)

    for field, value in update_data.items():
        setattr(pc, field, value)

    stamp_updated(pc, actor)
    audit_service.record_update(
        db,
        resource_type=ResourceType.PC,
        resource_id=pc.id,
        actor=actor,
        before=before,
        after=snapshot(pc),
    )

    db.commit()
    db.refresh(pc)

    logger.info(f"PC '{pc_id}' updated")
    return pc


@router.delete("/{pc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pc(pc_id: str, current_user: AdminUser, db: DBSession):
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
            detail=f"PC with ID '{pc_id}' not found",
        )

    # Check if this is a manager PC with controlled PCs
    controlled_count = (
        db.query(func.count(PC.id)).filter(PC.manager_id == pc_id).scalar() or 0
    )
    if controlled_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete manager PC '{pc_id}' with {controlled_count} controlled PCs. "
            f"Reassign or delete controlled PCs first.",
        )

    actor = principal_to_actor(current_user)
    snap = snapshot(pc)
    audit_service.record_delete(
        db, resource_type=ResourceType.PC, resource_id=pc.id, actor=actor, snapshot=snap
    )

    db.delete(pc)
    db.commit()

    logger.info(f"PC '{pc_id}' deleted by user {current_user.username}")


@router.get("/{pc_id}/config/preview")
async def preview_pc_config(pc_id: str, current_user: AdminUser, db: DBSession):
    """
    Preview the device configuration for a PC without deploying it.

    Generates and returns the device configuration JSON that would be sent to the PC.

    Args:
        pc_id: PC ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Generated device configuration

    Raises:
        HTTPException: If PC not found
    """
    from app.services.config_loader import load_pc_config
    from app.services.config_generator import generate_config

    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    try:
        # Load PC configuration from database
        logger.info(f"Loading configuration for PC {pc_id}")
        site_config = load_pc_config(pc_id, db)

        # Generate device JSON config
        logger.info(f"Generating device config for PC {pc_id}")
        device_config = generate_config(site_config, db)

        return device_config

    except Exception as e:
        logger.error(f"Error generating config for PC {pc_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate the display configuration.",
        )


@router.post("/{pc_id}/deploy", response_model=PCDeployResult)
def deploy_config(pc_id: str, current_user: AdminUser, db: DBSession):
    """
    Deploy configuration to a PC via the display client service.

    Generates the device configuration, records the deploy (``pc_deployments``,
    committed before the send) and hands it to the relay (plan 06 D14). A
    plain ``def``: the relay call blocks, so FastAPI runs this route in its
    threadpool and the event loop keeps serving other requests.

    Args:
        pc_id: PC ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        ``{pc_id, status, delivery, config_id, confirmable, message, screens,
        ack_timeout_sec}``
        (never the generated configuration).

    Raises:
        HTTPException: 404 if the PC is not found, 409 if it has no layout.

    A layout that was not delivered (offline PC, relay unreachable or no
    answer) answers 502 with ``{detail: <message>, result: <the same per-PC
    result>}`` (D18).
    """
    from app.services.deploy_service import TRIGGER_PC_DEPLOY, deploy_config_to_pcs

    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    if pc.screen_layout_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No screen layout is assigned to PC '{pc_id}'; assign a layout before deploying",
        )

    summary = deploy_config_to_pcs(
        [pc], db, trigger=TRIGGER_PC_DEPLOY, actor=principal_to_actor(current_user)
    )
    result = summary["results"][0]

    if result["status"] == "failed":
        # 502 with the message as a string ``detail`` (what older portals show),
        # plus the per-PC ``result`` (plan 06 D18): the portal tracks the deploy
        # row of a failed deploy too (config_id, delivery offline or error).
        # Built from PCDeployResult, so no other key can leak (NEW-45).
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": result["message"], "result": PCDeployResult(**result).model_dump(mode="json")},
        )

    return result


@router.post(
    "/{pc_id}/configure-screens",
    response_model=ConfigurePCScreensResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "PC or camera IDs not found"}},
)
async def configure_pc_screens(
    pc_id: str,
    request: ConfigurePCScreensRequest,
    db: DBSession,
    current_user: AdminUser,
):
    """
    Configure screens, views, and camera mappings for a PC.

    Creates or updates screens based on screen name matching, creates views
    for each screen, and distributes cameras across views sequentially.

    **Validation:**
    - PC must exist
    - ALL camera IDs must exist in database
    - Returns 404 error with list of invalid camera IDs if any don't exist

    **Screen Handling:**
    - If screen with same name exists for this PC: UPDATE (delete old views/mappings)
    - If screen name is new: CREATE new screen

    **Camera Distribution:**
    - Cameras distributed sequentially across screens and views
    - Fill View 1 completely, then View 2, etc.
    - Fill Screen 1 completely, then Screen 2, etc.
    - Empty slots are NOT created (no mappings without cameras)

    **Screen Dimensions:**
    - Screen rows/columns: capped at 4x4 (physical display limit)
    - View layout: can be up to 10x10 (virtual grid)

    Only admins and super admins can configure PC screens.

    Args:
        pc_id: PC identifier
        request: Screen configuration request with camera list
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Configuration statistics (screens created/updated, views, mappings, cameras used)

    Raises:
        HTTPException 404: PC not found or invalid camera IDs
        HTTPException 500: Configuration error
    """
    from app.services.pc_screen_configurator import (
        validate_camera_ids,
        configure_pc_screens as configure_screens,
    )

    actor = principal_to_actor(current_user)

    try:
        # 1. Validate PC exists
        pc = db.query(PC).filter(PC.id == pc_id).first()
        if not pc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"PC with ID '{pc_id}' not found",
            )

        # 2. Validate ALL camera IDs exist - FAIL FAST
        invalid_ids = validate_camera_ids(request.camera_ids, db)
        if invalid_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "detail": "Some camera IDs not found in database",
                    "invalid_camera_ids": invalid_ids,
                },
            )

        # 3. Only proceed if all validations pass
        result = configure_screens(pc_id, request, db, actor=actor)

        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Error configuring screens for PC {pc_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not configure the display screens.",
        )


@router.post("/{pc_id}/generate-token", response_model=PCTokenResponse)
async def generate_pc_token(pc_id: str, current_user: AdminUser, db: DBSession):
    """
    Generate a new authentication token for a PC.

    Creates a long-lived JWT token (8760 hours = 1 year by default) for PC client
    authentication with the WebSocket server and API endpoints.

    The token is:
    - Stored in the database (auth_token field)
    - Associated with an expiration timestamp (token_expiry field)
    - Used by the PC client to authenticate WebSocket connections

    Only admins and super admins can generate PC tokens.

    Args:
        pc_id: PC identifier
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        PC token details including the JWT token and expiration info

    Raises:
        HTTPException 404: PC not found
        HTTPException 500: Token generation error
    """
    from app.core.security import create_pc_auth_token
    from app.core.config import settings

    # Verify PC exists
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    try:
        actor = principal_to_actor(current_user)
        before = snapshot(pc)

        # Generate JWT token for PC
        token, expiry_timestamp = create_pc_auth_token(pc_id, pc.name)

        # Save token to database
        pc.auth_token = token
        pc.token_expiry = expiry_timestamp
        # D30: the token an open relay renewal replaced no longer counts as
        # current once an admin issues a new one.
        pc_tokens.cancel_open_renewals(db, pc_id, actor)

        stamp_updated(pc, actor)
        audit_service.record_update(
            db,
            resource_type=ResourceType.PC,
            resource_id=pc.id,
            actor=actor,
            before=before,
            after=snapshot(pc),
        )

        db.commit()

        logger.info(f"Token generated for PC '{pc_id}' by user {current_user.username}")

        return PCTokenResponse(
            pc_id=pc_id,
            pc_name=pc.name,
            auth_token=token,
            token_expiry=expiry_timestamp,
            expires_in_hours=settings.JWT_PC_TOKEN_EXPIRE_HOURS,
            message=f"Token generated successfully for PC '{pc.name}'",
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Error generating token for PC {pc_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not generate the authentication token.",
        )


# Helper function to query WebSocket server
async def get_websocket_connected_pcs() -> dict:
    """
    Query the WebSocket server for currently connected PCs.

    Returns:
        Dictionary with connected_pcs list and total_connected count
    """
    from app.core.config import settings
    import os

    # Use Docker service name if running in container, otherwise use localhost
    websocket_host = os.getenv("WEBSOCKET_INTERNAL_HOST", "websocket")

    websocket_url = (
        f"http://{websocket_host}:{settings.WEBSOCKET_PORT}/api/connected-pcs"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(websocket_url)
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.warning(f"Failed to connect to WebSocket server at {websocket_url}: {e}")
        return {"connected_pcs": [], "total_connected": 0}
    except httpx.HTTPStatusError as e:
        logger.error(f"WebSocket server returned error: {e}")
        return {"connected_pcs": [], "total_connected": 0}
    except Exception as e:
        logger.error(f"Unexpected error querying WebSocket server: {e}")
        return {"connected_pcs": [], "total_connected": 0}


@router.get("/{pc_id}/status", response_model=PCConnectionStatus)
async def get_pc_connection_status(
    pc_id: str, current_user: CurrentUser, db: DBSession
):
    """
    Get connection status for a specific PC.

    Returns:
    - PC ID and name
    - Whether PC is currently connected to WebSocket server
    - Last connection timestamp from database
    - Last configuration applied timestamp
    - Current WebSocket connection start time (if connected)
    - The latest station status (``health``) and deploy (``last_deploy``),
      or null (D16)

    The connection status is determined by querying the WebSocket server
    for real-time connection information and combining it with database
    information.

    Returns 404 if PC not found in database.
    """
    rows = pc_status.status_rows(db, pc_id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )
    pc, health, deploy = rows[0]

    # Get currently connected PCs from WebSocket server
    ws_data = await get_websocket_connected_pcs()
    connected_pc_ids = {pc["pc_id"]: pc for pc in ws_data.get("connected_pcs", [])}
    return _connection_status(pc, health, deploy, connected_pc_ids.get(pc_id), time.time())


def _require_pc(db, pc_id: str) -> None:
    if db.query(PC.id).filter(PC.id == pc_id).first() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )


@router.get("/{pc_id}/deployments", response_model=DeploymentHistory)
def get_pc_deployments(
    pc_id: str,
    current_user: CurrentUser,
    db: DBSession,
    limit: int = Query(pc_status.HISTORY_DEFAULT_LIMIT, ge=1, le=pc_status.HISTORY_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """A PC's deploys, newest first (created_at desc, id desc), paginated
    (D16). Retention keeps the newest 50 per PC, none older than 90 days."""
    _require_pc(db, pc_id)
    total, rows = pc_status.deployment_history(db, pc_id, limit=limit, offset=offset)
    return DeploymentHistory(pc_id=pc_id, total=total, limit=limit, offset=offset,
                             items=[pc_status.deployment_summary(row) for row in rows])


@router.get("/{pc_id}/health", response_model=Optional[PCHealthDetail])
def get_pc_health(pc_id: str, current_user: CurrentUser, db: DBSession):
    """Everything the relay keeps about the PC's latest station status (D16).
    ``null`` when the PC never reported; 404 when there is no such PC."""
    from app.models.pc_tracking import PcHealth

    _require_pc(db, pc_id)
    row = db.query(PcHealth).filter(PcHealth.pc_id == pc_id).first()
    return pc_status.health_detail(row)


@router.post("/{pc_id}/import-config", response_model=ImportConfigResponse)
async def import_pc_config(
    pc_id: str, request: ImportConfigRequest, db: DBSession, current_user: AdminUser
):
    """
    Import configuration for a PC from device JSON.

    This endpoint accepts a device configuration JSON (same format as deploy config)
    and creates/updates the screens, views, and mappings for the PC.

    The configuration is imported into the database but NOT deployed to the PC.
    Use the deploy endpoint to send the configuration to the PC after import.

    Cameras and devices that don't exist in the database will be skipped.

    Args:
        pc_id: ID of the PC to import config for
        request: Request containing the device configuration JSON
        db: Database session
        current_user: Current authenticated admin user

    Returns:
        Import result with statistics

    Raises:
        404: PC not found
        400: Invalid configuration format
    """
    # Import the configuration
    actor = principal_to_actor(current_user)
    result = import_config_for_pc(db=db, pc_id=pc_id, config=request.config, actor=actor)

    if not result.success:
        # Check if it's a 404 (PC not found)
        if "not found" in result.message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=result.message
            )
        # Otherwise it's a bad request
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=result.message
        )

    return ImportConfigResponse(
        success=result.success,
        pc_id=result.pc_id,
        screens_created=result.screens_created,
        views_created=result.views_created,
        mappings_created=result.mappings_created,
        cameras_skipped=result.cameras_skipped,
        devices_skipped=result.devices_skipped,
        message=result.message,
    )


@router.post(
    "/{pc_id}/copy-layout-from/{source_pc_id}", response_model=CopyLayoutResponse
)
async def copy_layout_from_another_pc(
    pc_id: str, source_pc_id: str, db: DBSession, current_user: AdminUser
):
    """
    Copy the entire screen layout from another PC.

    This endpoint copies all screens, views, and mappings from the source PC
    to the target PC. The target PC's existing layout is cleared before copying.

    New IDs are generated for the copied screens and views, but the camera
    and device assignments are preserved.

    Args:
        pc_id: ID of the target PC to copy layout TO
        source_pc_id: ID of the source PC to copy layout FROM
        db: Database session
        current_user: Current authenticated admin user

    Returns:
        Copy result with statistics

    Raises:
        404: Source or target PC not found
        400: Cannot copy to same PC or source has no screens
    """
    actor = principal_to_actor(current_user)
    result = copy_layout_from_pc(
        db=db, target_pc_id=pc_id, source_pc_id=source_pc_id, actor=actor
    )

    if not result.success:
        # Check if it's a 404 (PC not found)
        if "not found" in result.message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=result.message
            )
        # Otherwise it's a bad request
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=result.message
        )

    return CopyLayoutResponse(
        success=result.success,
        source_pc_id=result.source_pc_id,
        target_pc_id=result.target_pc_id,
        screens_copied=result.screens_copied,
        views_copied=result.views_copied,
        mappings_copied=result.mappings_copied,
        message=result.message,
    )
