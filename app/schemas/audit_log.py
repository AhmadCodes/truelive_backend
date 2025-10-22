"""
Pydantic schemas for audit log operations.
"""

from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime
from uuid import UUID


class AuditLogBase(BaseModel):
    """Base audit log schema."""

    action: str = Field(..., description="Action identifier (e.g., 'user.created', 'site.updated')")
    resource_type: str = Field(..., description="Type of resource affected")
    resource_id: Optional[str] = Field(None, description="ID of the affected resource")
    changes: Optional[dict[str, Any]] = Field(None, description="Changes made (before/after values)")


class AuditLogCreate(AuditLogBase):
    """Schema for creating an audit log entry."""

    user_id: Optional[UUID] = Field(None, description="User who performed the action")
    ip_address: Optional[str] = Field(None, max_length=45, description="IP address of the user")
    user_agent: Optional[str] = Field(None, description="User agent string from the request")


class AuditLogResponse(AuditLogBase):
    """Response schema for audit log."""

    id: UUID
    user_id: Optional[UUID] = None
    username: Optional[str] = Field(None, description="Username of the user who performed the action")
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AuditLogListResponse(BaseModel):
    """Response schema for paginated audit log list."""

    total: int = Field(..., description="Total number of audit logs")
    page: int = Field(..., description="Current page number")
    per_page: int = Field(..., description="Items per page")
    total_pages: int = Field(..., description="Total number of pages")
    logs: list[AuditLogResponse] = Field(..., description="List of audit logs")


class AuditLogStatsResponse(BaseModel):
    """Response schema for audit log statistics."""

    total_logs: int = Field(..., description="Total number of audit logs")
    logs_by_action: dict[str, int] = Field(..., description="Count of logs grouped by action")
    logs_by_resource_type: dict[str, int] = Field(..., description="Count of logs grouped by resource type")
    logs_by_user: dict[str, int] = Field(..., description="Count of logs grouped by user (top 10)")
    recent_activity: list[AuditLogResponse] = Field(..., description="Most recent 10 audit logs")


class AuditLogFilters(BaseModel):
    """Filters for audit log queries."""

    user_id: Optional[UUID] = Field(None, description="Filter by user ID")
    action: Optional[str] = Field(None, description="Filter by action (supports partial match)")
    resource_type: Optional[str] = Field(None, description="Filter by resource type")
    resource_id: Optional[str] = Field(None, description="Filter by resource ID")
    date_from: Optional[datetime] = Field(None, description="Filter logs from this date")
    date_to: Optional[datetime] = Field(None, description="Filter logs until this date")
    page: int = Field(1, ge=1, description="Page number")
    per_page: int = Field(50, ge=1, le=1000, description="Items per page")
