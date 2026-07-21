"""
Pydantic schemas for SyncJob API requests and responses.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum


class SyncJobStatus(str, Enum):
    """Enum for sync job statuses."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncJobResponse(BaseModel):
    """Response schema for sync job details."""
    id: str = Field(..., description="Unique job identifier")
    status: SyncJobStatus = Field(..., description="Current job status")
    progress: int = Field(..., ge=0, le=100, description="Progress percentage (0-100)")
    progress_message: Optional[str] = Field(None, description="Current step or progress description")
    started_at: Optional[datetime] = Field(None, description="When sync job started")
    completed_at: Optional[datetime] = Field(None, description="When sync job completed")
    created_at: datetime = Field(..., description="When sync job was created")
    result: Optional[Dict[str, Any]] = Field(None, description="Sync results if completed")
    error_message: Optional[str] = Field(None, description="Error message if failed")
    triggered_by: Optional[str] = Field(None, description="User ID who triggered the sync")

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "status": "completed",
                "progress": 100,
                "progress_message": "Sync completed successfully",
                "started_at": "2025-10-22T07:12:52Z",
                "completed_at": "2025-10-22T07:15:32Z",
                "created_at": "2025-10-22T07:12:50Z",
                "result": {
                    "devices_updated": 57,
                    "cameras_updated": 430,
                    "devices_removed": 0,
                    "cameras_removed": 0,
                    "errors": 0
                },
                "error_message": None,
                "triggered_by": "516c0a7f-3747-448c-99cd-eabe07c9b601"
            }
        }


class SyncJobStartResponse(BaseModel):
    """Response schema when starting an async sync job."""
    job_id: str = Field(..., description="Unique job identifier for tracking")
    status: SyncJobStatus = Field(..., description="Initial job status (pending)")
    message: str = Field(..., description="Human-readable message")

    class Config:
        json_schema_extra = {
            "example": {
                "job_id": "123e4567-e89b-12d3-a456-426614174000",
                "status": "pending",
                "message": "Sync job started successfully. Use job_id to check status."
            }
        }
