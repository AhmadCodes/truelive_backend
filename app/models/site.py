"""
Site model for managing surveillance sites.
"""

from sqlalchemy import (
    Column, String, Boolean, Text, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Site(BaseModel):
    """
    Site model representing a surveillance location.

    Attributes:
        id: Primary key (custom string ID)
        name: Site name
        nvr_username: Username for NVR access
        nvr_password: Encrypted password for NVR access
        sureview_site: Whether this is a SureView-managed site
        new: Whether this is a newly added site
    """
    __tablename__ = "sites"

    id = Column(
        String(255),
        primary_key=True,
        comment="Unique site identifier"
    )
    name = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Site name"
    )
    nvr_username = Column(
        String(255),
        nullable=False,
        comment="Username for NVR access"
    )
    nvr_password = Column(
        Text,
        nullable=False,
        comment="Encrypted password for NVR access"
    )
    sureview_site = Column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="Whether this is a SureView-managed site"
    )
    new = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether this is a newly added site"
    )
    use_tcp = Column(
        Boolean,
        default=False,
        nullable=False,
        server_default='false',
        comment="Site-wide default for RTSP TCP transport (overridable per camera)"
    )

    # SureView additional fields
    customer_id = Column(
        String(50),
        nullable=True,
        index=True,
        comment="Customer ID from SureView (referenceId)"
    )
    address = Column(
        String(500),
        nullable=True,
        comment="Physical address of the site"
    )
    telephone = Column(
        String(255),
        nullable=True,
        comment="Primary contact telephone"
    )
    telephone2 = Column(
        String(255),
        nullable=True,
        comment="Secondary contact telephone"
    )
    telephone_police = Column(
        String(100),
        nullable=True,
        comment="Police contact telephone"
    )
    telephone_fire = Column(
        String(100),
        nullable=True,
        comment="Fire department contact telephone"
    )
    notes = Column(
        Text,
        nullable=True,
        comment="Site notes and instructions"
    )
    lat_long = Column(
        String(100),
        nullable=True,
        comment="Latitude and longitude coordinates"
    )

    # Relationships
    cameras = relationship(
        "Camera",
        back_populates="site",
        cascade="all, delete-orphan"
    )
    category_mappings = relationship(
        "SiteCategoryMapping",
        back_populates="site",
        cascade="all, delete-orphan"
    )
    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="site",
        foreign_keys="ScreenMapping.site_id"
    )
    layout_config = relationship(
        "SiteCamerasLayoutConfig",
        back_populates="site",
        uselist=False,  # One-to-one relationship
        cascade="all, delete-orphan"
    )
    layout_slots = relationship(
        "SiteCamerasLayout",
        back_populates="site",
        cascade="all, delete-orphan"
    )

    # Table constraints
    __table_args__ = (
        Index("idx_sites_name", "name"),
        Index("idx_sites_sureview", "sureview_site"),
        Index("idx_sites_customer_id", "customer_id"),
        Index("idx_sites_created_at", "created_at", postgresql_ops={"created_at": "DESC"}),
    )

    def __repr__(self):
        return (
            f"<Site(id='{self.id}', name='{self.name}', "
            f"sureview_site={self.sureview_site}, new={self.new})>"
        )
