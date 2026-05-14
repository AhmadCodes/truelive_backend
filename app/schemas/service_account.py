"""
Pydantic schemas for service-account auth.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ServiceAccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)


class ServiceAccountUpdate(BaseModel):
    description: Optional[str] = None
    scopes: Optional[list[str]] = None
    is_active: Optional[bool] = None


class ServiceAccountResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ServiceAccountTokenCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Human-friendly label, e.g. 'prod-key-2026-05'")
    expires_at: Optional[datetime] = None


class ServiceAccountTokenResponse(BaseModel):
    """Returned WITHOUT the raw token after creation. Use TokenWithSecret on POST."""
    id: str
    name: str
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ServiceAccountTokenWithSecret(ServiceAccountTokenResponse):
    """Returned ONCE on token creation. The secret is never re-displayed."""
    secret: str = Field(..., description="The raw bearer token `tlsa_<...>` — shown once, store securely.")
