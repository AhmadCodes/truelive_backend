"""
Pydantic schemas for authentication endpoints.
"""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional
from datetime import datetime
import uuid


class Token(BaseModel):
    """Token response schema."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenRefresh(BaseModel):
    """Token refresh request schema."""
    refresh_token: str


class Login(BaseModel):
    """Login request schema."""
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)
    remember_me: bool = False


class UserBase(BaseModel):
    """Base user schema with common fields."""
    username: str = Field(..., min_length=3, max_length=255)
    email: EmailStr
    role: str = Field(..., pattern="^(user|admin|super_admin)$")


class UserCreate(UserBase):
    """Schema for creating a new user."""
    password: str = Field(..., min_length=8)
    is_active: bool = True

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v):
        """Validate password meets security requirements."""
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one number")
        return v


class UserUpdate(BaseModel):
    """Schema for updating user details."""
    email: Optional[EmailStr] = None
    role: Optional[str] = Field(None, pattern="^(user|admin|super_admin)$")
    is_active: Optional[bool] = None


class PasswordChange(BaseModel):
    """Schema for changing password."""
    current_password: str
    new_password: str = Field(..., min_length=8)
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        """Validate passwords match."""
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("Passwords do not match")
        return v


class PasswordReset(BaseModel):
    """Schema for resetting password (admin only)."""
    new_password: str = Field(..., min_length=8)


class EmailUpdate(BaseModel):
    """Schema for updating email."""
    email: EmailStr


class UserResponse(UserBase):
    """User response schema."""
    user_id: uuid.UUID
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserDetailResponse(UserResponse):
    """Detailed user response with additional fields."""
    created_by: Optional[uuid.UUID] = None
    updated_at: datetime

    class Config:
        from_attributes = True


class InvitationCreate(BaseModel):
    """Schema for creating invitation token."""
    email: Optional[EmailStr] = None


class InvitationResponse(BaseModel):
    """Invitation token response."""
    invitation_url: str
    token: str
    expires_at: datetime

    class Config:
        from_attributes = True
