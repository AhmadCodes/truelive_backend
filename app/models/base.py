"""
Base model classes and mixins for SQLAlchemy models.
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, String, func
from app.database import Base


class TimestampMixin:
    """
    Mixin that adds created_at and updated_at timestamp fields.
    Uses PostgreSQL TIMESTAMP WITH TIME ZONE for timezone-aware timestamps.
    """
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Timestamp when the record was created"
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="Timestamp when the record was last updated"
    )


class ActorStampMixin:
    """
    Mixin that records WHO created and last modified a row, where the actor may
    be a human user, a service account, or the system.

    Stored as a (type, id, label) triple for both creator and last modifier:
      - *_by_type:  'user' | 'service_account' | 'system'
      - *_by_id:    the user UUID (as text) or service-account id, or NULL for system
      - *_by_label: cached display name captured at write time (fallback shown when
                    the actor is later deleted; while the actor still exists, read
                    paths live-resolve the current name instead)

    Intentionally NO ForeignKey on *_id: the stamp must survive deletion of the
    referenced user/service account.
    """
    created_by_type = Column(
        String(20), nullable=False, server_default="system",
        comment="Actor kind that created the row: user|service_account|system"
    )
    created_by_id = Column(
        String(36), nullable=True,
        comment="User UUID (as text) or service-account id of the creator; NULL for system"
    )
    created_by_label = Column(
        String(255), nullable=False, server_default="system",
        comment="Cached display name of the creator at creation time"
    )
    updated_by_type = Column(
        String(20), nullable=False, server_default="system",
        comment="Actor kind that last modified the row: user|service_account|system"
    )
    updated_by_id = Column(
        String(36), nullable=True,
        comment="User UUID (as text) or service-account id of the last modifier; NULL for system"
    )
    updated_by_label = Column(
        String(255), nullable=False, server_default="system",
        comment="Cached display name of the last modifier at modification time"
    )

    @property
    def created_by(self):
        """Cached creator stamp as a dict (for response serialization via
        from_attributes). Read paths may live-resolve the label to the actor's
        current name; this property returns the cached fallback."""
        if not self.created_by_type:
            return None
        return {
            "type": self.created_by_type,
            "id": self.created_by_id,
            "label": self.created_by_label,
        }

    @property
    def updated_by(self):
        """Cached last-modifier stamp as a dict (see created_by)."""
        if not self.updated_by_type:
            return None
        return {
            "type": self.updated_by_type,
            "id": self.updated_by_id,
            "label": self.updated_by_label,
        }


class BaseModel(Base, TimestampMixin):
    """
    Abstract base model that includes timestamp fields.
    All models should inherit from this class.
    """
    __abstract__ = True

    def to_dict(self):
        """
        Convert model instance to dictionary.

        Returns:
            Dictionary representation of the model
        """
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns
        }

    def __repr__(self):
        """
        String representation of the model.

        Returns:
            String representation
        """
        columns = ", ".join(
            f"{column.name}={repr(getattr(self, column.name))}"
            for column in self.__table__.columns
        )
        return f"<{self.__class__.__name__}({columns})>"
