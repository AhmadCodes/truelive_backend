"""
PcScreenMappingState model — portal-only per-(PC, mapping) play state.

Holds the per-PC playing_state that used to live on ScreenMapping, now that
mappings are shared across PCs via layouts.
"""

from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from app.models.base import BaseModel


class PcScreenMappingState(BaseModel):
    """
    PcScreenMappingState model tracking per-PC play state for a screen mapping.

    Attributes:
        id: Auto-incrementing primary key
        pc_id: Reference to the PC
        mapping_id: Reference to the screen mapping
        playing_state: Whether this camera is currently playing for this PC
    """

    __tablename__ = "pc_screen_mapping_state"

    # Primary key
    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="Auto-incrementing primary key",
    )

    # Foreign keys
    pc_id = Column(
        String(50),
        ForeignKey("pcs.id", ondelete="CASCADE", name="fk_pcsms_pc"),
        nullable=False,
        comment="ID of the PC",
    )

    mapping_id = Column(
        Integer,
        ForeignKey("screen_mappings.id", ondelete="CASCADE", name="fk_pcsms_mapping"),
        nullable=False,
        comment="ID of the screen mapping",
    )

    # State
    playing_state = Column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="Whether this camera is currently playing for this PC",
    )

    # Constraints and indexes
    __table_args__ = (
        UniqueConstraint("pc_id", "mapping_id", name="uq_pc_screen_mapping_state"),
        Index("idx_pcsms_pc", "pc_id"),
        Index("idx_pcsms_mapping", "mapping_id"),
    )

    def __repr__(self):
        """String representation of PcScreenMappingState."""
        return (
            f"<PcScreenMappingState(id={self.id}, pc_id='{self.pc_id}', "
            f"mapping_id={self.mapping_id}, playing_state={self.playing_state})>"
        )

    def to_dict(self):
        """
        Convert PcScreenMappingState instance to dictionary.

        Returns:
            Dictionary representation of the state row
        """
        return {
            "id": self.id,
            "pc_id": self.pc_id,
            "mapping_id": self.mapping_id,
            "playing_state": self.playing_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
