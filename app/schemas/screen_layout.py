"""
Pydantic schemas for ScreenLayout operations.

A ScreenLayout is the new owner between PC and Screen: screens belong to a
layout, and PCs point at a single layout to resolve their screen configuration.
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.schemas.pc import PCResponse


class ScreenLayoutCreate(BaseModel):
    """Schema for creating a new screen layout."""

    id: str = Field(
        ..., min_length=1, max_length=100, description="Unique screen layout identifier"
    )
    name: str = Field(
        ..., min_length=1, max_length=255, description="Screen layout name"
    )


class ScreenLayoutUpdate(BaseModel):
    """Schema for updating an existing screen layout. All fields are optional."""

    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="Screen layout name"
    )


class ScreenLayoutResponse(BaseModel):
    """Screen layout response schema."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AssignedPCsResponse(BaseModel):
    """Response listing the PCs currently assigned to a screen layout."""

    screen_layout_id: str = Field(..., description="Screen layout identifier")
    pcs: List[PCResponse] = Field(
        default_factory=list, description="PCs assigned to this layout"
    )


class DeployRequest(BaseModel):
    """Request body for deploying a screen layout to its assigned PCs."""

    pc_ids: Optional[List[str]] = Field(
        None,
        description="Optional subset of assigned PC IDs to deploy to; deploys to all assigned PCs when omitted",
    )


class PlayingStateUpdate(BaseModel):
    """Request body for setting per-PC playing state on a screen mapping."""

    pc_id: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="PC ID the playing state applies to",
    )
    playing_state: bool = Field(
        ..., description="Active playback state for this PC and mapping"
    )
