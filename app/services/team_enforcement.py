"""
Team boundary enforcement — the single source of truth for cross-team checks.

Teams own three top-level entities: Sites (many-to-many), PCs (one team) and
Screen Layouts (one team). Two rules follow:

1. **Cameras on a layout** — a camera may only be placed on a layout if the
   camera's site is a member of that layout's team. A layout's link to a site is
   *implicit*: layout → screens → screen_mappings → camera → device → site. This
   module centralizes that walk so the four placement sites
   (create/update screen mapping + the two bulk paths) and the guarded moves all
   share one implementation and never drift.
2. **Layouts on a PC** — enforced at the call sites directly (both are a single
   ``pc.team_id == layout.team_id`` column comparison, see the API layer).

All user-facing messages here are deliberately neutral: they never name internal
technology or leak schema/table structure.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.camera import Camera
from app.models.device import Device
from app.models.screen import Screen
from app.models.screen_layout import ScreenLayout
from app.models.screen_mapping import ScreenMapping
from app.models.team import site_team


# --------------------------------------------------------------------------- #
# Customer-facing messages (no internal tech, no schema leakage).
# --------------------------------------------------------------------------- #
MSG_CAMERA_NOT_IN_TEAM = (
    "This camera can't be added here — it belongs to a location that isn't part "
    "of this team."
)
MSG_PC_LAYOUT_DIFFERENT_TEAM = (
    "This layout can't be assigned to this device — they belong to different teams."
)
MSG_SITE_UNASSIGN_IN_USE = (
    "This location can't be removed from the team while the team's layouts are "
    "still using its cameras. Remove those cameras from the layouts first."
)
MSG_LAYOUT_MOVE_CAMERAS = (
    "This layout can't be moved to the selected team — it still contains cameras "
    "from locations that aren't part of that team."
)
MSG_PC_MOVE_HOLDS_LAYOUT = (
    "This device can't be moved to the selected team while it still has a layout "
    "from its current team. Remove the layout first."
)


class CrossTeamError(ValueError):
    """Raised when an operation would cross a team boundary.

    Subclasses ``ValueError`` so service-layer callers that already translate
    ``ValueError`` to a 400 keep working. The message is customer-facing.
    """


# --------------------------------------------------------------------------- #
# Membership primitives
# --------------------------------------------------------------------------- #
def site_ids_for_team(db: Session, team_id: str) -> set[str]:
    """Return the set of site ids that are members of ``team_id``."""
    rows = db.execute(
        select(site_team.c.site_id).where(site_team.c.team_id == team_id)
    ).all()
    return {r[0] for r in rows}


def camera_ids_for_team(db: Session, team_id: str) -> set[str]:
    """Return the set of camera ids whose site is a member of ``team_id``.

    This is the team's camera library expressed as ids — used to filter which
    cameras an import/copy may place onto the team's layouts.
    """
    rows = db.execute(
        select(Camera.id)
        .select_from(Camera)
        .join(Device, Device.id == Camera.device_id)
        .join(site_team, site_team.c.site_id == Device.site_id)
        .where(site_team.c.team_id == team_id)
    ).all()
    return {r[0] for r in rows}


def is_site_in_team(db: Session, site_id: str, team_id: str) -> bool:
    """True if ``site_id`` is a member of ``team_id``."""
    row = db.execute(
        select(site_team.c.site_id)
        .where(site_team.c.site_id == site_id)
        .where(site_team.c.team_id == team_id)
        .limit(1)
    ).first()
    return row is not None


def site_id_for_camera(db: Session, camera_id: str) -> Optional[str]:
    """Resolve a camera to its owning site id (camera → device → site)."""
    row = db.execute(
        select(Device.site_id)
        .select_from(Camera)
        .join(Device, Device.id == Camera.device_id)
        .where(Camera.id == camera_id)
    ).first()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Layout / team resolution
# --------------------------------------------------------------------------- #
def team_id_for_layout(db: Session, layout_id: str) -> Optional[str]:
    """Return the team id that owns ``layout_id`` (a direct column read)."""
    row = db.execute(
        select(ScreenLayout.team_id).where(ScreenLayout.id == layout_id)
    ).first()
    return row[0] if row else None


def team_id_for_screen(db: Session, screen_id: str) -> Optional[str]:
    """Return the team id owning the layout that ``screen_id`` belongs to."""
    row = db.execute(
        select(ScreenLayout.team_id)
        .select_from(Screen)
        .join(ScreenLayout, ScreenLayout.id == Screen.screen_layout_id)
        .where(Screen.id == screen_id)
    ).first()
    return row[0] if row else None


def cameras_in_layout(db: Session, layout_id: str) -> List[str]:
    """Return distinct non-null camera ids currently placed in ``layout_id``."""
    rows = db.execute(
        select(ScreenMapping.camera_id)
        .select_from(ScreenMapping)
        .join(Screen, Screen.id == ScreenMapping.screen_id)
        .where(Screen.screen_layout_id == layout_id)
        .where(ScreenMapping.camera_id.isnot(None))
        .distinct()
    ).all()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Assertions used by the placement enforcement sites
# --------------------------------------------------------------------------- #
def assert_camera_in_screen_team(db: Session, camera_id: str, screen_id: str) -> None:
    """Reject placing ``camera_id`` onto a slot of ``screen_id`` when the camera's
    site is not a member of the screen's layout's team.

    A missing team (e.g. a screen with no resolvable layout) is treated as "no
    constraint could be resolved" and is not blocked here — existence of the
    screen/camera is validated by the caller.
    """
    team_id = team_id_for_screen(db, screen_id)
    if team_id is None:
        return
    _assert_camera_in_team(db, camera_id, team_id)


def assert_camera_in_layout_team(db: Session, camera_id: str, layout_id: str) -> None:
    """Reject placing ``camera_id`` onto ``layout_id`` when the camera's site is
    not a member of the layout's team."""
    team_id = team_id_for_layout(db, layout_id)
    if team_id is None:
        return
    _assert_camera_in_team(db, camera_id, team_id)


def assert_cameras_in_layout_team(
    db: Session, camera_ids: List[str], layout_id: str
) -> None:
    """Bulk variant — reject if any of ``camera_ids`` has a site outside the
    layout's team. One membership query, not N round-trips."""
    ids = [c for c in camera_ids if c]
    if not ids:
        return
    team_id = team_id_for_layout(db, layout_id)
    if team_id is None:
        return
    allowed_sites = site_ids_for_team(db, team_id)
    rows = db.execute(
        select(Camera.id, Device.site_id)
        .select_from(Camera)
        .join(Device, Device.id == Camera.device_id)
        .where(Camera.id.in_(ids))
    ).all()
    for _camera_id, site_id in rows:
        if site_id not in allowed_sites:
            raise CrossTeamError(MSG_CAMERA_NOT_IN_TEAM)


def _assert_camera_in_team(db: Session, camera_id: str, team_id: str) -> None:
    site_id = site_id_for_camera(db, camera_id)
    # If the camera doesn't resolve to a site, existence is the caller's concern;
    # we only enforce the boundary when a site is known.
    if site_id is None:
        return
    if not is_site_in_team(db, site_id, team_id):
        raise CrossTeamError(MSG_CAMERA_NOT_IN_TEAM)


# --------------------------------------------------------------------------- #
# Guarded-move checks — return the offending items; caller picks the message.
# --------------------------------------------------------------------------- #
def layouts_blocking_site_unassign(
    db: Session, site_id: str, team_id: str
) -> List[str]:
    """Return ids of the team's layouts that currently use a camera belonging to
    ``site_id``. Non-empty => un-assigning the site from the team is blocked."""
    rows = db.execute(
        select(ScreenLayout.id)
        .select_from(ScreenLayout)
        .join(Screen, Screen.screen_layout_id == ScreenLayout.id)
        .join(ScreenMapping, ScreenMapping.screen_id == Screen.id)
        .join(Camera, Camera.id == ScreenMapping.camera_id)
        .join(Device, Device.id == Camera.device_id)
        .where(ScreenLayout.team_id == team_id)
        .where(Device.site_id == site_id)
        .distinct()
    ).all()
    return [r[0] for r in rows]


def cameras_blocking_layout_move(
    db: Session, layout_id: str, target_team_id: str
) -> List[str]:
    """Return ids of cameras in ``layout_id`` whose site is not a member of
    ``target_team_id``. Non-empty => moving the layout to that team is blocked."""
    allowed_sites = site_ids_for_team(db, target_team_id)
    rows = db.execute(
        select(Camera.id, Device.site_id)
        .select_from(Screen)
        .join(ScreenMapping, ScreenMapping.screen_id == Screen.id)
        .join(Camera, Camera.id == ScreenMapping.camera_id)
        .join(Device, Device.id == Camera.device_id)
        .where(Screen.screen_layout_id == layout_id)
        .distinct()
    ).all()
    return [camera_id for camera_id, site_id in rows if site_id not in allowed_sites]


def layout_blocks_pc_move(db: Session, pc, target_team_id: str) -> bool:
    """True if ``pc`` still holds a layout that is not in ``target_team_id`` —
    in which case moving the PC to that team is blocked."""
    if not pc.screen_layout_id:
        return False
    layout_team = team_id_for_layout(db, pc.screen_layout_id)
    return layout_team is not None and layout_team != target_team_id
