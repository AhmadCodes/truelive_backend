"""
ScreenMapping model for camera-to-screen slot assignments.
"""

from sqlalchemy import Column, String, Integer, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, ActorStampMixin


class ScreenMapping(BaseModel, ActorStampMixin):
    """
    ScreenMapping model representing the assignment of cameras to specific grid positions.

    This is a junction table that maps cameras to specific positions (slots) on screens/views.
    Each record represents one camera displayed at a specific position in a specific view.

    Attributes:
        id: Auto-incrementing primary key
        screen_id: Reference to the screen
        view_id: Reference to the view
        slot_row: Row position in the grid (1-indexed)
        slot_col: Column position in the grid (1-indexed)
        device_id: Reference to the device
        camera_id: Reference to the camera
    """

    __tablename__ = "screen_mappings"

    # Primary key
    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="Auto-incrementing primary key",
    )

    # Foreign keys
    screen_id = Column(
        String(100),
        ForeignKey("screens.id", ondelete="CASCADE"),
        nullable=False,
        comment="ID of the screen",
    )

    view_id = Column(
        String(255),
        ForeignKey("views.id", ondelete="CASCADE"),
        nullable=False,
        comment="ID of the view",
    )

    # Grid position
    slot_row = Column(
        Integer, nullable=False, comment="Row position in the grid (1-indexed)"
    )

    slot_col = Column(
        Integer, nullable=False, comment="Column position in the grid (1-indexed)"
    )

    # Camera assignment
    device_id = Column(
        String(255),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=True,
        comment="ID of the device",
    )

    camera_id = Column(
        String(255),
        ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=True,
        comment="ID of the camera",
    )

    # Constraints and indexes
    __table_args__ = (
        # Unique constraint: one camera per grid position per view
        UniqueConstraint(
            "view_id", "slot_row", "slot_col", name="uq_screen_mapping_slot"
        ),
        # Indexes for efficient lookups
        Index("idx_screen_mappings_screen", "screen_id"),
        Index("idx_screen_mappings_view", "view_id"),
        Index("idx_screen_mappings_device", "device_id"),
        Index("idx_screen_mappings_camera", "camera_id"),
    )

    # Relationships
    screen = relationship(
        "Screen", back_populates="screen_mappings", doc="Screen this mapping belongs to"
    )

    view = relationship(
        "View", back_populates="screen_mappings", doc="View this mapping belongs to"
    )

    device = relationship(
        "Device",
        back_populates="screen_mappings",
        foreign_keys=[device_id],
        doc="Device of the camera",
    )

    camera = relationship(
        "Camera", back_populates="screen_mappings", doc="Camera being displayed"
    )

    def __repr__(self):
        """String representation of ScreenMapping."""
        return (
            f"<ScreenMapping(id={self.id}, view_id='{self.view_id}', "
            f"slot=({self.slot_row},{self.slot_col}), camera_id='{self.camera_id}')>"
        )

    def to_dict(self):
        """
        Convert ScreenMapping instance to dictionary.

        Returns:
            Dictionary representation of the screen mapping
        """
        return {
            "id": self.id,
            "screen_id": self.screen_id,
            "view_id": self.view_id,
            "slot_row": self.slot_row,
            "slot_col": self.slot_col,
            "device_id": self.device_id,
            "camera_id": self.camera_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
