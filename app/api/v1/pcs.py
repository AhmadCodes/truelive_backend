"""
PC management API endpoints.
Provides CRUD operations for PCs and their associations with screens.
"""

from fastapi import APIRouter, HTTPException, status, Query
from sqlalchemy import func, or_
from typing import List, Optional
import httpx
from datetime import datetime

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
from app.services.config_importer import import_config_for_pc, copy_layout_from_pc
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("", response_model=PCResponse, status_code=status.HTTP_201_CREATED)
async def create_pc(pc_data: PCCreate, current_user: AdminUser, db: DBSession):
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

    # Create new PC
    new_pc = PC(
        id=pc_data.id,
        name=pc_data.name,
        ip_address=pc_data.ip_address,
        gpu_type=pc_data.gpu_type,
        role=pc_data.role,
        manager_id=pc_data.manager_id,
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
    query = db.query(PC)

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
        result.append(pc_data)

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


@router.get("/status", response_model=AllPCsConnectionStatus)
async def get_all_pcs_connection_status(current_user: CurrentUser, db: DBSession):
    """
    Get connection status for all PCs.

    Returns:
    - Total number of PCs in database
    - Number of currently connected PCs
    - Number of disconnected PCs
    - List of all PCs with their connection status

    The connection status is determined by querying the WebSocket server
    for real-time connection information and combining it with database
    information (last_connected, last_applied timestamps).
    """
    # Get all PCs from database
    all_pcs = db.query(PC).all()

    # Get currently connected PCs from WebSocket server
    ws_data = await get_websocket_connected_pcs()
    connected_pc_ids = {pc["pc_id"]: pc for pc in ws_data.get("connected_pcs", [])}

    # Build status for each PC
    pc_statuses = []
    for pc in all_pcs:
        is_connected = pc.id in connected_pc_ids
        ws_info = connected_pc_ids.get(pc.id, {})

        # Convert Unix timestamp to datetime if exists
        last_connected = None
        if pc.last_connected:
            last_connected = datetime.fromtimestamp(pc.last_connected)

        last_applied = None
        if pc.last_applied:
            last_applied = datetime.fromtimestamp(pc.last_applied)

        pc_status = PCConnectionStatus(
            pc_id=pc.id,
            pc_name=pc.name,
            is_connected=is_connected,
            last_connected=last_connected,
            last_applied=last_applied,
            websocket_connected_at=ws_info.get("connected_at")
            if is_connected
            else None,
        )
        pc_statuses.append(pc_status)

    # Calculate counts
    connected_count = sum(1 for pc in pc_statuses if pc.is_connected)
    disconnected_count = len(pc_statuses) - connected_count

    return AllPCsConnectionStatus(
        total_pcs=len(pc_statuses),
        connected_count=connected_count,
        disconnected_count=disconnected_count,
        pcs=pc_statuses,
    )


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
    pc = db.query(PC).filter(PC.id == pc_id).first()
    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    return pc


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

    return PCWithManager.model_validate(pc)


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

    return result


@router.put("/{pc_id}", response_model=PCResponse)
async def update_pc(
    pc_id: str, pc_data: PCUpdate, current_user: AdminUser, db: DBSession
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
            detail=f"PC with ID '{pc_id}' not found",
        )

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

    # Reparent to a screen layout. A non-null value must reference an existing
    # layout; an explicit null unassigns the PC from its current layout.
    if update_data.get("screen_layout_id"):
        from app.models.screen_layout import ScreenLayout

        layout = (
            db.query(ScreenLayout)
            .filter(ScreenLayout.id == update_data["screen_layout_id"])
            .first()
        )
        if not layout:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Screen layout with ID '{update_data['screen_layout_id']}' not found",
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

    db.commit()
    db.refresh(pc)

    logger.info(f"PC '{pc_id}' updated by user {current_user.username}")
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
            detail=f"Failed to generate configuration: {str(e)}",
        )


def _deploy_config_to_pcs(pcs: List[PC], db) -> dict:
    """
    Deploy the resolved display configuration to a batch of PCs.

    Opens a SINGLE connection to the display client service, loops emitting one
    config message per PC (``targetId`` = the PC id), then disconnects once. A
    PC with no assigned screen layout generates an empty config and is reported
    as ``skipped`` rather than treated as an error. Config generation or delivery
    failures for one PC are recorded and the batch continues.

    Args:
        pcs: PCs to deploy to (already loaded from the session)
        db: Database session

    Returns:
        Aggregated result: counts plus a per-PC ``results`` list (input order).
        Each per-PC entry has ``pc_id``, ``status`` (``deployed`` / ``skipped``
        / ``failed``), ``message`` and ``screens``; deployed entries also carry
        the generated ``config``.
    """
    from app.services.config_loader import load_pc_config
    from app.services.config_generator import generate_config
    import socketio as sio_client
    from app.core.config import settings
    import time

    results: dict = {}
    configs: dict = {}

    # 1. Generate each PC's config up front; separate no-layout and failures.
    for pc in pcs:
        if pc.screen_layout_id is None:
            results[pc.id] = {
                "pc_id": pc.id,
                "status": "skipped",
                "message": "No screen layout is assigned to this deployment target",
                "screens": 0,
            }
            continue
        try:
            logger.info(f"Generating device config for PC {pc.id}")
            device_config = generate_config(load_pc_config(pc.id, db), db)
            configs[pc.id] = device_config
        except Exception as e:
            logger.error(f"Error generating config for PC {pc.id}: {e}")
            results[pc.id] = {
                "pc_id": pc.id,
                "status": "failed",
                "message": "Failed to generate the display configuration",
                "screens": 0,
            }

    # 2. Deliver over a single connection to the display client service.
    if configs:
        sio = sio_client.Client()
        websocket_host = (
            "websocket"
            if settings.WEBSOCKET_HOST == "0.0.0.0"
            else settings.WEBSOCKET_HOST
        )
        websocket_url = f"http://{websocket_host}:{settings.WEBSOCKET_PORT}"
        connected = False
        try:
            logger.info(f"Connecting to display client service at {websocket_url}")
            sio.connect(websocket_url)
            connected = True
        except Exception as e:
            logger.error(f"Could not reach display client service: {e}")

        if not connected:
            for pc in pcs:
                if pc.id in configs:
                    results[pc.id] = {
                        "pc_id": pc.id,
                        "status": "failed",
                        "message": "Could not reach the display client to deploy the configuration",
                        "screens": len(configs[pc.id].get("screens", [])),
                    }
        else:
            for pc in pcs:
                if pc.id not in configs:
                    continue
                device_config = configs[pc.id]
                try:
                    sio.emit(
                        "message",
                        {"type": "config", "targetId": pc.id, "content": device_config},
                    )
                    pc.last_applied = int(time.time())
                    results[pc.id] = {
                        "pc_id": pc.id,
                        "status": "deployed",
                        "message": f"Configuration deployed to PC {pc.id}",
                        "screens": len(device_config.get("screens", [])),
                        "config": device_config,
                    }
                except Exception as e:
                    logger.error(f"Error deploying config to PC {pc.id}: {e}")
                    results[pc.id] = {
                        "pc_id": pc.id,
                        "status": "failed",
                        "message": "Could not reach the display client to deploy the configuration",
                        "screens": len(device_config.get("screens", [])),
                    }

            # Single short settle before tearing the connection down.
            time.sleep(0.5)
            try:
                sio.disconnect()
            except Exception:
                pass
            db.commit()

    ordered = [results[pc.id] for pc in pcs]
    return {
        "total": len(pcs),
        "deployed": sum(1 for r in ordered if r["status"] == "deployed"),
        "skipped": sum(1 for r in ordered if r["status"] == "skipped"),
        "failed": sum(1 for r in ordered if r["status"] == "failed"),
        "results": ordered,
    }


@router.post("/{pc_id}/deploy")
async def deploy_config(pc_id: str, current_user: AdminUser, db: DBSession):
    """
    Deploy configuration to a PC via the display client service.

    Generates the device configuration and sends it to the target PC
    if it's currently connected.

    Args:
        pc_id: PC ID
        current_user: Current authenticated admin or super admin
        db: Database session

    Returns:
        Deployment status

    Raises:
        HTTPException: If PC not found, has no layout, or delivery fails
    """
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

    summary = _deploy_config_to_pcs([pc], db)
    result = summary["results"][0]

    if result["status"] == "failed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=result["message"]
        )

    return {
        "pc_id": pc_id,
        "status": result["status"],
        "message": result["message"],
        "screens": result["screens"],
        "config": result.get("config", {}),
    }


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
        result = configure_screens(pc_id, request, db)

        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.error(f"Error configuring screens for PC {pc_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to configure PC screens: {str(e)}",
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
        # Generate JWT token for PC
        token, expiry_timestamp = create_pc_auth_token(pc_id, pc.name)

        # Save token to database
        pc.auth_token = token
        pc.token_expiry = expiry_timestamp
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
            detail=f"Failed to generate token: {str(e)}",
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

    The connection status is determined by querying the WebSocket server
    for real-time connection information and combining it with database
    information.

    Returns 404 if PC not found in database.
    """
    # Get PC from database
    pc = db.query(PC).filter(PC.id == pc_id).first()

    if not pc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PC with ID '{pc_id}' not found",
        )

    # Get currently connected PCs from WebSocket server
    ws_data = await get_websocket_connected_pcs()
    connected_pc_ids = {pc["pc_id"]: pc for pc in ws_data.get("connected_pcs", [])}

    # Check if this PC is connected
    is_connected = pc_id in connected_pc_ids
    ws_info = connected_pc_ids.get(pc_id, {})

    # Convert Unix timestamp to datetime if exists
    last_connected = None
    if pc.last_connected:
        last_connected = datetime.fromtimestamp(pc.last_connected)

    last_applied = None
    if pc.last_applied:
        last_applied = datetime.fromtimestamp(pc.last_applied)

    return PCConnectionStatus(
        pc_id=pc.id,
        pc_name=pc.name,
        is_connected=is_connected,
        last_connected=last_connected,
        last_applied=last_applied,
        websocket_connected_at=ws_info.get("connected_at") if is_connected else None,
    )


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
    result = import_config_for_pc(db=db, pc_id=pc_id, config=request.config)

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
    result = copy_layout_from_pc(db=db, target_pc_id=pc_id, source_pc_id=source_pc_id)

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
