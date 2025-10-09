"""
SQLAlchemy models for site camera layout configuration and layout.
"""

from sqlalchemy import (
    Column, String, Integer, ForeignKey, ForeignKeyConstraint, CheckConstraint, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship, foreign
from app.models.base import BaseModel


class SiteCamerasLayoutConfig(BaseModel):
    """
    Configuration for site camera layout grid (rows x columns).
    Defines the grid dimensions for displaying cameras for a specific site.
    """
    __tablename__ = "site_cameras_layout_config"

    site_id = Column(
        String(255),
        ForeignKey('sites.id', ondelete='CASCADE'),
        primary_key=True,
        comment="Unique identifier for the site (references sites.id)"
    )
    site_name = Column(
        String(255),
        nullable=False,
        comment="Name of the site"
    )
    n_rows = Column(
        Integer,
        nullable=False,
        comment="Number of rows in the layout grid (1-4)"
    )
    n_cols = Column(
        Integer,
        nullable=False,
        comment="Number of columns in the layout grid (1-4)"
    )

    # Table args with constraints
    __table_args__ = (
        CheckConstraint(
            'n_rows BETWEEN 1 AND 4',
            name='check_n_rows_valid'
        ),
        CheckConstraint(
            'n_cols BETWEEN 1 AND 4',
            name='check_n_cols_valid'
        ),
    )

    # Relationships
    site = relationship(
        "Site",
        back_populates="layout_config",
        lazy="select"
    )

    layout_slots = relationship(
        "SiteCamerasLayout",
        back_populates="config",
        primaryjoin="SiteCamerasLayoutConfig.site_id == foreign(SiteCamerasLayout.site_id)",
        cascade="all, delete-orphan",
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<SiteCamerasLayoutConfig("
            f"site_id='{self.site_id}', "
            f"site_name='{self.site_name}', "
            f"grid={self.n_rows}x{self.n_cols})>"
        )


class SiteCamerasLayout(BaseModel):
    """
    Individual camera slot assignments in a site's layout grid.
    Each record represents one camera in a specific grid position.
    """
    __tablename__ = "site_cameras_layout"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="Auto-incrementing primary key"
    )
    site_id = Column(
        String(255),
        nullable=False,
        comment="Unique identifier for the site (references sites.id)"
    )
    site_name = Column(
        String(255),
        nullable=False,
        comment="Name of the site"
    )
    slot_row = Column(
        Integer,
        nullable=False,
        comment="Row position in the grid (1-indexed)"
    )
    slot_col = Column(
        Integer,
        nullable=False,
        comment="Column position in the grid (1-indexed)"
    )
    camera_id = Column(
        String(255),
        nullable=False,
        comment="Unique identifier for the camera (references cameras.id)"
    )

    # Constraints and indexes
    __table_args__ = (
        # Foreign key constraints
        ForeignKeyConstraint(
            ['site_id'],
            ['sites.id'],
            name='fk_site_cameras_layout_site',
            ondelete='CASCADE'
        ),
        ForeignKeyConstraint(
            ['camera_id'],
            ['cameras.id'],
            name='fk_site_cameras_layout_camera',
            ondelete='CASCADE'
        ),
        # Unique constraint: one camera per grid position per site
        UniqueConstraint(
            'site_id', 'slot_row', 'slot_col',
            name='uq_site_cameras_layout_slot'
        ),
        # Check constraints for valid positions
        CheckConstraint(
            'slot_row >= 1',
            name='check_slot_row_positive'
        ),
        CheckConstraint(
            'slot_col >= 1',
            name='check_slot_col_positive'
        ),
        # Indexes for efficient lookups
        Index('idx_site_cameras_layout_site', 'site_id'),
        Index('idx_site_cameras_layout_camera', 'camera_id'),
    )

    # Relationships
    # Note: Assumes 'Site' and 'Camera' models exist
    site = relationship(
        "Site",
        back_populates="layout_slots",
        lazy="select"
    )

    camera = relationship(
        "Camera",
        back_populates="layout_slots",
        lazy="select"
    )

    config = relationship(
        "SiteCamerasLayoutConfig",
        back_populates="layout_slots",
        foreign_keys=[site_id],
        primaryjoin="SiteCamerasLayout.site_id == SiteCamerasLayoutConfig.site_id",
        lazy="select"
    )

    def __repr__(self):
        return (
            f"<SiteCamerasLayout("
            f"id={self.id}, "
            f"site_id='{self.site_id}', "
            f"slot=({self.slot_row},{self.slot_col}), "
            f"camera_id='{self.camera_id}')>"
        )
