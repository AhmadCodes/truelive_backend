"""
SQLAlchemy model for Camera table.
"""

from sqlalchemy import Column, String, Text, Boolean, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Camera(BaseModel):
    """
    Camera model representing individual cameras associated with devices.
    Each camera has RTSP streaming URLs and can be marked as new.
    """

    __tablename__ = "cameras"

    id = Column(
        String(255), primary_key=True, comment="Unique identifier for the camera"
    )
    device_id = Column(
        String(255),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Device this camera belongs to (references devices.id)",
    )
    name = Column(
        String(255), nullable=False, index=True, comment="Display name of the camera"
    )
    rtsp_url = Column(
        Text, nullable=False, comment="RTSP URL for camera streaming (can be long)"
    )
    main_stream_url = Column(
        Text, nullable=True, comment="Main stream URL for camera (optional)"
    )
    new = Column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Flag indicating if this is a newly added camera",
    )
    use_tcp = Column(
        Boolean,
        nullable=True,
        default=None,
        comment="Per-camera RTSP TCP override: NULL inherits device.use_tcp, true/false overrides",
    )

    # Table arguments for indexes
    __table_args__ = (
        Index("idx_cameras_device_id", "device_id"),
        Index("idx_cameras_name", "name"),
        Index(
            "idx_cameras_created_at",
            "created_at",
            postgresql_using="btree",
            postgresql_ops={"created_at": "DESC"},
        ),
    )

    # Relationships
    # Note: Assumes 'Device' model exists with 'id' as primary key
    device = relationship("Device", back_populates="cameras", lazy="select")

    # Relationship to snapshot (one-to-one). snapshots.camera_id is BOTH the PK
    # and the FK (ON DELETE CASCADE), so the snapshot is removed with the camera
    # at the DB level. passive_deletes=True lets the DB do that cascade — without
    # it the ORM tries to NULL snapshots.camera_id on delete, which fails because
    # that column is the primary key (AssertionError, 500 on any camera delete).
    snapshot = relationship(
        "Snapshot",
        back_populates="camera",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="select",
    )

    # Relationship to layout slots
    layout_slots = relationship(
        "SiteCamerasLayout",
        back_populates="camera",
        cascade="all, delete-orphan",
        lazy="select",
    )

    # Relationship to screen mappings
    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="camera",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self):
        return (
            f"<Camera("
            f"id='{self.id}', "
            f"device_id='{self.device_id}', "
            f"name='{self.name}', "
            f"new={self.new})>"
        )
