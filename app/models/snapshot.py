"""
SQLAlchemy model for Snapshot table.
"""

from sqlalchemy import (
    Column, String, Integer, BigInteger, LargeBinary, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Snapshot(BaseModel):
    """
    Snapshot model representing camera snapshots with image data.
    One-to-one relationship with Camera (camera_id is both PK and FK).
    Stores the latest snapshot image for each camera.
    """
    __tablename__ = "snapshots"

    camera_id = Column(
        String(255),
        ForeignKey('cameras.id', ondelete='CASCADE'),
        primary_key=True,
        comment="Camera ID this snapshot belongs to (references cameras.id)"
    )
    image = Column(
        LargeBinary,
        nullable=False,
        comment="Binary image data (BYTEA in PostgreSQL)"
    )
    width = Column(
        Integer,
        nullable=False,
        comment="Image width in pixels"
    )
    height = Column(
        Integer,
        nullable=False,
        comment="Image height in pixels"
    )
    capture_time = Column(
        BigInteger,
        nullable=False,
        index=True,
        comment="Unix timestamp when snapshot was captured"
    )

    # Table arguments for indexes
    __table_args__ = (
        Index('idx_snapshots_capture_time', 'capture_time', postgresql_using='btree', postgresql_ops={'capture_time': 'DESC'}),
    )

    # Relationships
    # Note: Assumes 'Camera' model exists with 'id' as primary key
    camera = relationship(
        "Camera",
        back_populates="snapshot",
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<Snapshot("
            f"camera_id='{self.camera_id}', "
            f"dimensions={self.width}x{self.height}, "
            f"capture_time={self.capture_time})>"
        )
