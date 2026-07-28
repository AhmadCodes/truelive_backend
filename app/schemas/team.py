"""
Pydantic schemas for Team operations.

A **Team** is the top-level organizational grouping. It owns Sites (many-to-many),
PCs (one team each) and Screen Layouts (one team each), and constrains which
cameras may appear on a team's layouts and which PCs its layouts may be assigned
to. Teams do not scope row visibility — they are an organizational + validation
grouping only.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.schemas.actor import ActorStampsMixin


class TeamCreate(BaseModel):
    """Schema for creating a new team."""

    name: str = Field(..., min_length=1, max_length=255, description="Unique team name")


class TeamUpdate(BaseModel):
    """Schema for renaming a team. Only the name may change."""

    name: str = Field(
        ..., min_length=1, max_length=255, description="New unique team name"
    )


class TeamResponse(ActorStampsMixin):
    """Team response schema."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SiteTeamAssignRequest(BaseModel):
    """Request body for assigning one or more sites to a team."""

    site_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Site IDs to assign to this team (a site may belong to many teams)",
    )


class CameraLibraryItem(BaseModel):
    """One camera in a team's filtered camera library."""

    id: str = Field(..., description="Camera ID")
    name: str = Field(..., description="Camera display name")
    device_id: str = Field(..., description="Owning device ID")
    device_name: Optional[str] = Field(None, description="Owning device name")
    site_id: str = Field(..., description="Owning site ID (a member of the team)")
    site_name: Optional[str] = Field(None, description="Owning site name")


class CameraLibraryResponse(BaseModel):
    """A team's camera library — only cameras whose site is a member of the team."""

    team_id: str = Field(..., description="Team the library was resolved for")
    cameras: List[CameraLibraryItem] = Field(
        default_factory=list, description="Cameras available to this team's layouts"
    )
