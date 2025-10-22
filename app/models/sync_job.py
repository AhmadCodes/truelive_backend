"""
SQLAlchemy model for SyncJob table.

Tracks asynchronous SureView sync operations for status monitoring and history.
"""

from sqlalchemy import (
    Column, String, Text, Enum, Integer, DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
import enum


class SyncJobStatus(str, enum.Enum):
    """Enum for sync job statuses."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncJob(BaseModel):
    """
    SyncJob model representing asynchronous SureView sync operations.

    Tracks the status, progress, and results of sync jobs for monitoring
    and historical purposes.
    """
    __tablename__ = "sync_jobs"

    id = Column(
        String(255),
        primary_key=True,
        comment="Unique identifier for the sync job (UUID)"
    )

    status = Column(
        Enum(SyncJobStatus),
        nullable=False,
        default=SyncJobStatus.PENDING,
        comment="Current status of the sync job"
    )

    progress = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Progress percentage (0-100)"
    )

    progress_message = Column(
        String(500),
        nullable=True,
        comment="Current step or progress description"
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when sync job actually started processing"
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when sync job completed or failed"
    )

    result = Column(
        JSON,
        nullable=True,
        comment="Sync results (sites_updated, cameras_updated, etc.)"
    )

    error_message = Column(
        Text,
        nullable=True,
        comment="Error message if sync failed"
    )

    triggered_by = Column(
        String(255),
        ForeignKey('users.user_id', ondelete='SET NULL'),
        nullable=True,
        comment="User who triggered the sync"
    )

    # Relationships
    user = relationship(
        "User",
        foreign_keys=[triggered_by],
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<SyncJob("
            f"id='{self.id}', "
            f"status='{self.status}', "
            f"progress={self.progress}%, "
            f"started_at='{self.started_at}')>"
        )

    def to_dict(self):
        """Convert sync job to dictionary for API responses."""
        return {
            "id": self.id,
            "status": self.status.value if isinstance(self.status, SyncJobStatus) else self.status,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "result": self.result,
            "error_message": self.error_message,
            "triggered_by": self.triggered_by
        }
