"""
Pydantic schemas for PC operations.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Literal
from datetime import datetime


class PCBase(BaseModel):
    """Base schema with common PC fields."""

    name: str = Field(..., min_length=1, max_length=255, description="PC name")
    ip_address: str = Field(..., min_length=1, max_length=50, description="IP address of the PC")
    gpu_type: Optional[str] = Field(None, max_length=100, description="GPU type/model")
    role: Literal['controller', 'manager'] = Field('controller', description="PC role (controller or manager)")
    manager_id: Optional[str] = Field(None, max_length=50, description="Manager PC ID (for controller PCs)")


class PCCreate(PCBase):
    """Schema for creating a new PC."""

    id: str = Field(..., min_length=1, max_length=50, description="Unique PC identifier")

    @field_validator('manager_id')
    @classmethod
    def validate_manager_id(cls, v: Optional[str], info) -> Optional[str]:
        """Validate that manager_id is only set for controller PCs."""
        if v is not None:
            role = info.data.get('role')
            if role == 'manager':
                raise ValueError('Manager PCs cannot have a manager_id')
        return v


class PCUpdate(BaseModel):
    """Schema for updating an existing PC. All fields are optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="PC name")
    ip_address: Optional[str] = Field(None, min_length=1, max_length=50, description="IP address of the PC")
    gpu_type: Optional[str] = Field(None, max_length=100, description="GPU type/model")
    role: Optional[Literal['controller', 'manager']] = Field(None, description="PC role (controller or manager)")
    manager_id: Optional[str] = Field(None, max_length=50, description="Manager PC ID (for controller PCs)")


class PCResponse(BaseModel):
    """Basic PC response schema."""

    id: str
    name: str
    ip_address: str
    gpu_type: Optional[str] = None
    role: str
    manager_id: Optional[str] = None
    last_connected: Optional[datetime] = None
    last_applied: Optional[datetime] = None

    class Config:
        from_attributes = True


class PCDetailResponse(PCResponse):
    """Detailed PC response schema with timestamps."""

    auth_token: Optional[str] = None
    token_expiry: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PCWithScreenCount(PCResponse):
    """PC response with count of screens connected to this PC."""

    screen_count: int = 0

    class Config:
        from_attributes = True


class PCWithManager(PCResponse):
    """PC response with manager PC information."""

    manager: Optional[PCResponse] = None

    class Config:
        from_attributes = True


class PCWithControlled(PCResponse):
    """PC response with controlled PCs (for manager PCs)."""

    controlled_pcs: List[PCResponse] = []
    screen_count: int = 0

    class Config:
        from_attributes = True


class ScreenSummary(BaseModel):
    """Summary information for a screen."""

    id: str
    name: str
    rows: int
    columns: int
    total_slots: int
    switching_interval: Optional[int] = None

    class Config:
        from_attributes = True


class PCWithScreens(PCWithScreenCount):
    """PC response with detailed screen information."""

    screens: List[ScreenSummary] = []

    class Config:
        from_attributes = True
