"""
SQLAlchemy model for SystemSetting table.

Stores system-wide configuration settings with encryption support for sensitive values.
"""

import uuid
from sqlalchemy import Column, String, Text, Boolean, ForeignKey, JSON, UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class SystemSetting(BaseModel):
    """
    SystemSetting model for storing runtime-configurable system settings.

    Supports encryption for sensitive values (passwords), audit trail for changes,
    and categorization for logical grouping of settings.
    """
    __tablename__ = "system_settings"

    id = Column(
        String(255),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key"
    )

    key = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique setting key (e.g., 'smtp.host')"
    )

    value = Column(
        Text,
        nullable=True,
        comment="Setting value (stored as string, may be encrypted)"
    )

    category = Column(
        String(50),
        nullable=False,
        index=True,
        comment="Setting category (smtp, tasks, snapshots, etc.)"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Human-readable description of what this setting does"
    )

    is_encrypted = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether the value is encrypted (for passwords)"
    )

    data_type = Column(
        String(20),
        nullable=False,
        default="string",
        comment="Data type: string, integer, boolean, json"
    )

    updated_by = Column(
        UUID(as_uuid=True),
        ForeignKey('users.user_id', ondelete='SET NULL'),
        nullable=True,
        comment="User ID who last updated this setting"
    )

    # Relationships
    user = relationship(
        "User",
        foreign_keys=[updated_by],
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<SystemSetting("
            f"key='{self.key}', "
            f"category='{self.category}', "
            f"is_encrypted={self.is_encrypted})>"
        )

    def to_dict(self, mask_sensitive=True):
        """
        Convert setting to dictionary for API responses.

        Args:
            mask_sensitive: If True, masks encrypted values with asterisks

        Returns:
            Dictionary representation of the setting
        """
        value = self.value
        if mask_sensitive and self.is_encrypted and value:
            value = "********"

        return {
            "id": self.id,
            "key": self.key,
            "value": value,
            "category": self.category,
            "description": self.description,
            "is_encrypted": self.is_encrypted,
            "data_type": self.data_type,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
