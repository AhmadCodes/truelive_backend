"""
Pydantic schemas for invitation operations.
"""

from pydantic import BaseModel, Field, EmailStr
from typing import Optional
from datetime import datetime
from uuid import UUID


class InvitationSendRequest(BaseModel):
    """Schema for sending an invitation."""

    email: EmailStr = Field(..., description="Email address to send invitation to")
    role: str = Field(default="user", description="Role for the invited user (user, admin, super_admin)")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "newuser@example.com",
                "role": "user"
            }
        }


class InvitationResponse(BaseModel):
    """Response schema for invitation."""

    id: UUID
    email: str
    role: str
    token: str
    created_at: datetime
    expires_at: datetime
    is_used: bool
    used_at: Optional[datetime] = None
    invited_by_id: UUID
    invited_by_username: Optional[str] = None

    class Config:
        from_attributes = True


class InvitationListResponse(BaseModel):
    """Response schema for listing invitations."""

    id: UUID
    email: str
    role: str
    created_at: datetime
    expires_at: datetime
    is_used: bool
    used_at: Optional[datetime] = None
    invited_by_username: Optional[str] = None
    is_expired: bool = False

    class Config:
        from_attributes = True


class RegisterWithInvitationRequest(BaseModel):
    """Schema for registering with an invitation token."""

    token: str = Field(..., min_length=1, description="Invitation token")
    username: str = Field(..., min_length=3, max_length=50, description="Desired username")
    full_name: str = Field(..., min_length=1, max_length=255, description="User's full name")
    password: str = Field(..., min_length=8, description="Account password")

    class Config:
        json_schema_extra = {
            "example": {
                "token": "123e4567-e89b-12d3-a456-426614174000",
                "username": "johndoe",
                "full_name": "John Doe",
                "password": "SecurePass123!"
            }
        }


class InvitationSendResponse(BaseModel):
    """Response after sending an invitation."""

    success: bool
    message: str
    invitation_id: Optional[UUID] = None
    email: Optional[str] = None
    expires_at: Optional[datetime] = None

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Invitation sent successfully to newuser@example.com",
                "invitation_id": "123e4567-e89b-12d3-a456-426614174000",
                "email": "newuser@example.com",
                "expires_at": "2024-10-25T12:00:00Z"
            }
        }
