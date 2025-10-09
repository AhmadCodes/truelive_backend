"""
SQLAlchemy model for Camera table.
"""

from sqlalchemy import (
    Column, String, Text, Boolean, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Camera(BaseModel):
    """
    Camera model representing individual cameras associated with sites.
    Each camera has RTSP streaming URLs and can be marked as SureView camera or new.
    """
    __tablename__ = "cameras"

    id = Column(
        String(255),
        primary_key=True,
        comment="Unique identifier for the camera"
    )
    site_id = Column(
        String(255),
        ForeignKey('sites.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        comment="Site this camera belongs to (references sites.id)"
    )
    name = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Display name of the camera"
    )
    rtsp_url = Column(
        Text,
        nullable=False,
        comment="RTSP URL for camera streaming (can be long)"
    )
    main_stream_url = Column(
        Text,
        nullable=True,
        comment="Main stream URL for camera (optional)"
    )
    sureview_camera = Column(
        Boolean,
        nullable=False,
        server_default='false',
        comment="Flag indicating if this is a SureView integrated camera"
    )
    new = Column(
        Boolean,
        nullable=False,
        server_default='true',
        comment="Flag indicating if this is a newly added camera"
    )

    # Table arguments for indexes
    __table_args__ = (
        Index('idx_cameras_site_id', 'site_id'),
        Index('idx_cameras_name', 'name'),
        Index('idx_cameras_created_at', 'created_at', postgresql_using='btree', postgresql_ops={'created_at': 'DESC'}),
    )

    # Relationships
    # Note: Assumes 'Site' model exists with 'id' as primary key
    site = relationship(
        "Site",
        back_populates="cameras",
        lazy="select"
    )

    # Relationship to snapshot (one-to-one)
    snapshot = relationship(
        "Snapshot",
        back_populates="camera",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="select"
    )

    # Relationship to layout slots
    layout_slots = relationship(
        "SiteCamerasLayout",
        back_populates="camera",
        cascade="all, delete-orphan",
        lazy="select"
    )

    # Relationship to screen mappings
    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="camera",
        cascade="all, delete-orphan",
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<Camera("
            f"id='{self.id}', "
            f"site_id='{self.site_id}', "
            f"name='{self.name}', "
            f"sureview={self.sureview_camera}, "
            f"new={self.new})>"
        )
