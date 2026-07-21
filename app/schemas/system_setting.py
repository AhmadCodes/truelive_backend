"""
Pydantic schemas for SystemSetting API requests and responses.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID


class SystemSettingBase(BaseModel):
    """Base schema for system settings."""
    key: str = Field(..., description="Unique setting key (e.g., 'smtp.host')")
    value: Optional[str] = Field(None, description="Setting value")
    category: str = Field(..., description="Setting category")
    description: Optional[str] = Field(None, description="Human-readable description")
    data_type: str = Field(default="string", description="Data type: string, integer, boolean")


class SystemSettingResponse(SystemSettingBase):
    """Response schema for system setting details."""
    id: str = Field(..., description="Setting UUID")
    is_encrypted: bool = Field(..., description="Whether value is encrypted")
    updated_by: Optional[UUID] = Field(None, description="User ID who last updated")
    updated_at: datetime = Field(..., description="When setting was last updated")
    created_at: datetime = Field(..., description="When setting was created")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "key": "smtp.host",
                "value": "mail.usvg.ai",
                "category": "smtp",
                "description": "SMTP server hostname",
                "is_encrypted": False,
                "data_type": "string",
                "updated_by": "516c0a7f-3747-448c-99cd-eabe07c9b601",
                "updated_at": "2025-10-22T14:30:00Z",
                "created_at": "2025-10-22T14:00:00Z"
            }
        }


class SystemSettingUpdate(BaseModel):
    """Request schema for updating a single setting."""
    value: str = Field(..., description="New value for the setting")

    class Config:
        json_schema_extra = {
            "example": {
                "value": "new.smtp.host.com"
            }
        }


class SystemSettingsBulkUpdate(BaseModel):
    """Request schema for updating multiple settings at once."""
    settings: Dict[str, str] = Field(..., description="Dictionary of key-value pairs to update")

    @field_validator('settings')
    @classmethod
    def validate_settings_not_empty(cls, v):
        if not v:
            raise ValueError("Settings dictionary cannot be empty")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "settings": {
                    "smtp.host": "mail.usvg.ai",
                    "smtp.port": "587",
                    "smtp.from_email": "noreply@usvg.ai"
                }
            }
        }


class SystemSettingsByCategoryResponse(BaseModel):
    """Response schema for settings grouped by category."""
    category: str = Field(..., description="Category name")
    settings: List[SystemSettingResponse] = Field(..., description="Settings in this category")

    class Config:
        json_schema_extra = {
            "example": {
                "category": "smtp",
                "settings": [
                    {
                        "id": "123e4567-e89b-12d3-a456-426614174000",
                        "key": "smtp.host",
                        "value": "mail.usvg.ai",
                        "category": "smtp",
                        "description": "SMTP server hostname",
                        "is_encrypted": False,
                        "data_type": "string",
                        "updated_by": None,
                        "updated_at": "2025-10-22T14:00:00Z",
                        "created_at": "2025-10-22T14:00:00Z"
                    }
                ]
            }
        }


class SMTPTestRequest(BaseModel):
    """Request schema for testing SMTP connection."""
    test_email: str = Field(..., description="Email address to send test message to")

    @field_validator('test_email')
    @classmethod
    def validate_email(cls, v):
        if '@' not in v or '.' not in v:
            raise ValueError("Invalid email address format")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "test_email": "admin@example.com"
            }
        }


class SMTPTestResponse(BaseModel):
    """Response schema for SMTP connection test."""
    success: bool = Field(..., description="Whether test was successful")
    message: str = Field(..., description="Result message")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional details or error info")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Test email sent successfully to admin@example.com",
                "details": {
                    "smtp_host": "mail.usvg.ai",
                    "smtp_port": 587,
                    "from_email": "info@usvg.ai"
                }
            }
        }
