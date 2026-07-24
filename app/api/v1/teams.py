"""
Team management API endpoints.

A **Team** is the top-level organizational grouping over Sites (many-to-many),
PCs (one team each) and Screen Layouts (one team each). Human management is
reserved to super_admins; machine integrations may manage teams with the
``teams:manage`` scope. Reads are open to any authenticated user or a
``teams:read``/``teams:manage`` service account.

Teams do not scope row visibility — they are an organizational + validation
grouping. The validation they enforce (which cameras may go on a team's layouts,
which PCs its layouts may be assigned to) lives in
``app.services.team_enforcement`` and at the relevant assignment endpoints.
"""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import DBSession, user_or_scope, super_admin_or_scope
from app.models.team import Team, site_team
from app.models.site import Site
from app.models.device import Device
from app.models.camera import Camera
from app.models.pc import PC
from app.models.screen_layout import ScreenLayout
from app.schemas.team import (
    TeamCreate,
    TeamUpdate,
    TeamResponse,
    SiteTeamAssignRequest,
    CameraLibraryItem,
    CameraLibraryResponse,
)
from app.services.team_enforcement import (
    layouts_blocking_site_unassign,
    MSG_SITE_UNASSIGN_IN_USE,
)

router = APIRouter()


def _generate_team_id() -> str:
    """Mint a new team identifier (mirrors the SITE_<hex> convention)."""
    return f"TEAM_{uuid.uuid4().hex[:12].upper()}"


def _get_team_or_404(db, team_id: str) -> Team:
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team '{team_id}' not found",
        )
    return team


@router.get("", response_model=List[TeamResponse])
async def list_teams(
    db: DBSession,
    _auth=Depends(user_or_scope("teams:read", "teams:manage")),
):
    """List all teams, ordered by name."""
    return db.query(Team).order_by(Team.name).all()


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    team_data: TeamCreate,
    db: DBSession,
    _auth=Depends(super_admin_or_scope("teams:manage")),
):
    """
    Create a new team.

    Requires super_admin (human) or a service account with ``teams:manage``.
    """
    existing = db.query(Team).filter(Team.name == team_data.name).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A team named '{team_data.name}' already exists",
        )

    new_team = Team(id=_generate_team_id(), name=team_data.name)
    db.add(new_team)
    db.commit()
    db.refresh(new_team)
    return new_team


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    team_id: str,
    db: DBSession,
    _auth=Depends(user_or_scope("teams:read", "teams:manage")),
):
    """Get a single team by id."""
    return _get_team_or_404(db, team_id)


@router.api_route("/{team_id}", methods=["PUT", "PATCH"], response_model=TeamResponse)
async def rename_team(
    team_id: str,
    team_data: TeamUpdate,
    db: DBSession,
    _auth=Depends(super_admin_or_scope("teams:manage")),
):
    """
    Rename a team.

    Requires super_admin (human) or a service account with ``teams:manage``.
    """
    team = _get_team_or_404(db, team_id)

    clash = (
        db.query(Team).filter(Team.name == team_data.name, Team.id != team_id).first()
    )
    if clash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A team named '{team_data.name}' already exists",
        )

    team.name = team_data.name
    db.commit()
    db.refresh(team)
    return team


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: str,
    db: DBSession,
    _auth=Depends(super_admin_or_scope("teams:manage")),
):
    """
    Delete a team.

    Blocked while the team still has any sites, PCs, or screen layouts — move
    or reassign those first. Requires super_admin or ``teams:manage``.
    """
    team = _get_team_or_404(db, team_id)

    site_count = db.execute(
        select(func.count())
        .select_from(site_team)
        .where(site_team.c.team_id == team_id)
    ).scalar_one()
    pc_count = db.query(PC).filter(PC.team_id == team_id).count()
    layout_count = (
        db.query(ScreenLayout).filter(ScreenLayout.team_id == team_id).count()
    )

    if site_count or pc_count or layout_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This team can't be deleted while it still has locations, "
                "devices, or layouts assigned to it. Move or reassign those "
                "first."
            ),
        )

    db.delete(team)
    db.commit()
    return None


@router.post("/{team_id}/sites", response_model=TeamResponse)
async def assign_sites_to_team(
    team_id: str,
    body: SiteTeamAssignRequest,
    db: DBSession,
    _auth=Depends(super_admin_or_scope("teams:manage")),
):
    """
    Assign one or more sites to a team.

    A site may belong to multiple teams simultaneously. Already-assigned sites
    are left unchanged (idempotent). Requires super_admin or ``teams:manage``.
    """
    team = _get_team_or_404(db, team_id)

    # Validate every requested site exists before inserting any membership.
    requested = list(dict.fromkeys(body.site_ids))
    found = {s.id for s in db.query(Site.id).filter(Site.id.in_(requested)).all()}
    missing = [sid for sid in requested if sid not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown location(s): {', '.join(missing)}",
        )

    for site_id in requested:
        db.execute(
            pg_insert(site_team)
            .values(site_id=site_id, team_id=team_id)
            .on_conflict_do_nothing(constraint="pk_site_team")
        )
    db.commit()
    db.refresh(team)
    return team


@router.delete("/{team_id}/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unassign_site_from_team(
    team_id: str,
    site_id: str,
    db: DBSession,
    _auth=Depends(super_admin_or_scope("teams:manage")),
):
    """
    Remove a site from a team.

    Blocked while any of the team's layouts still use a camera from that site —
    remove those cameras from the layouts first. Requires super_admin or
    ``teams:manage``.
    """
    _get_team_or_404(db, team_id)

    membership = db.execute(
        select(site_team.c.site_id)
        .where(site_team.c.site_id == site_id)
        .where(site_team.c.team_id == team_id)
    ).first()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This location is not assigned to this team",
        )

    blocking = layouts_blocking_site_unassign(db, site_id, team_id)
    if blocking:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=MSG_SITE_UNASSIGN_IN_USE,
        )

    db.execute(
        site_team.delete()
        .where(site_team.c.site_id == site_id)
        .where(site_team.c.team_id == team_id)
    )
    db.commit()
    return None


@router.get("/{team_id}/camera-library", response_model=CameraLibraryResponse)
async def get_team_camera_library(
    team_id: str,
    db: DBSession,
    _auth=Depends(user_or_scope("teams:read", "teams:manage")),
):
    """
    The team's camera library: every camera whose site is a member of the team.

    This is the set of cameras that may be placed on the team's layouts, and is
    intended to feed the layout-builder camera picker.
    """
    _get_team_or_404(db, team_id)

    rows = db.execute(
        select(
            Camera.id,
            Camera.name,
            Camera.device_id,
            Device.name,
            Device.site_id,
            Site.name,
        )
        .select_from(Camera)
        .join(Device, Device.id == Camera.device_id)
        .join(Site, Site.id == Device.site_id)
        .join(site_team, site_team.c.site_id == Device.site_id)
        .where(site_team.c.team_id == team_id)
        .order_by(Site.name, Camera.name)
    ).all()

    cameras = [
        CameraLibraryItem(
            id=camera_id,
            name=camera_name,
            device_id=device_id,
            device_name=device_name,
            site_id=site_id,
            site_name=site_name,
        )
        for camera_id, camera_name, device_id, device_name, site_id, site_name in rows
    ]
    return CameraLibraryResponse(team_id=team_id, cameras=cameras)
