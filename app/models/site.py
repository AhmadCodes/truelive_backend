"""
Site model — the parent "place" that owns one or more Devices (NVR/DVRs).

Note: the table name `sites` deliberately changes meaning as of migration 008.
Before 008 it modelled a single NVR/DVR; that entity is now `app.models.device.Device`
(table `devices`). This class models the physical location: address, contact
telephones, notes and coordinates.
"""

from sqlalchemy import (
    Column, String, Text, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Site(BaseModel):
    """
    Site model representing a physical surveillance location.

    A Site owns many Devices (NVR/DVRs). All location data lives here and only
    here — Devices carry no address or contact information.

    Attributes:
        id: Primary key (custom string ID, e.g. ``SITE_<hex>``)
        name: Site name
        customer_id: External customer reference ID
        address: Physical address of the site
        telephone: Primary contact telephone
        telephone2: Secondary contact telephone
        telephone_police: Police contact telephone
        telephone_fire: Fire department contact telephone
        notes: Site notes and instructions
        lat_long: Latitude and longitude coordinates
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

    # Location / contact details (moved up from the former sites table in 008)
    customer_id = Column(
        String(50),
        nullable=True,
        index=True,
        comment="External customer reference ID"
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
    devices = relationship(
        "Device",
        back_populates="site",
        cascade="all, delete-orphan"
    )
    category_mappings = relationship(
        "SiteCategoryMapping",
        back_populates="site",
        cascade="all, delete-orphan"
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
        Index("idx_sites_customer_id", "customer_id"),
        Index("idx_sites_created_at", "created_at", postgresql_ops={"created_at": "DESC"}),
    )

    def __repr__(self):
        return (
            f"<Site(id='{self.id}', name='{self.name}', "
            f"customer_id='{self.customer_id}')>"
        )
