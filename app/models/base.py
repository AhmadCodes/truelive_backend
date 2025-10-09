"""
Base model classes and mixins for SQLAlchemy models.
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, func
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
