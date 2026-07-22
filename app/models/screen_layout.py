"""
ScreenLayout model — the owner of screens, decoupled from PCs.

A ScreenLayout groups screens together. PCs point at a layout (nullable,
SET NULL) so multiple PCs can share a layout; screens belong to exactly one
layout (CASCADE).
"""

from sqlalchemy import Column, String, Index
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class ScreenLayout(BaseModel):
    """
    ScreenLayout model representing a named collection of screens.

    Attributes:
        id: Unique identifier for the screen layout
        name: Display name of the screen layout
    """

    __tablename__ = "screen_layouts"

    # Primary key
    id = Column(
        String(100), primary_key=True, comment="Unique identifier for the screen layout"
    )

    # Basic information
    name = Column(
        String(255), nullable=False, comment="Display name of the screen layout"
    )

    # Constraints
    __table_args__ = (Index("idx_screen_layouts_name", "name"),)

    # Relationships
    screens = relationship(
        "Screen",
        back_populates="screen_layout",
        cascade="all, delete-orphan",
        doc="Screens belonging to this layout",
    )

    pcs = relationship(
        "PC",
        back_populates="screen_layout",
        foreign_keys="PC.screen_layout_id",
        doc="PCs assigned to this layout",
    )

    def __repr__(self):
        """String representation of ScreenLayout."""
        return f"<ScreenLayout(id='{self.id}', name='{self.name}')>"

    def to_dict(self):
        """
        Convert ScreenLayout instance to dictionary.

        Returns:
            Dictionary representation of the screen layout
        """
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
