"""
Device model for managing NVR/DVR recorders.

This is the entity that was called ``Site`` (table ``sites``) prior to migration
008. It keeps the NVR credentials and flags; all location data moved up to the
new parent :class:`app.models.site.Site`.
"""

from sqlalchemy import (
    Column, String, Boolean, Text, ForeignKey, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Device(BaseModel):
    """
    Device model representing a single NVR/DVR recorder at a Site.

    Attributes:
        id: Primary key (custom string ID, e.g. ``DEV_<hex>``)
        name: Device name
        site_id: Parent Site this device belongs to (references sites.id)
        nvr_username: Username for NVR access
        nvr_password: Encrypted password for NVR access
        use_tcp: Device-wide default for RTSP TCP transport
        new: Whether this is a newly added device
    """
    __tablename__ = "devices"

    id = Column(
        String(255),
        primary_key=True,
        comment="Unique device identifier"
    )
    name = Column(
        String(255),
        nullable=False,
        index=True,
        comment="Device name"
    )
    site_id = Column(
        String(255),
        ForeignKey('sites.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        comment="Site this device belongs to (references sites.id)"
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
    new = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether this is a newly added device"
    )
    use_tcp = Column(
        Boolean,
        default=False,
        nullable=False,
        server_default='false',
        comment="Device-wide default for RTSP TCP transport (overridable per camera)"
    )

    # Relationships
    site = relationship(
        "Site",
        back_populates="devices"
    )
    cameras = relationship(
        "Camera",
        back_populates="device",
        cascade="all, delete-orphan"
    )
    screen_mappings = relationship(
        "ScreenMapping",
        back_populates="device",
        foreign_keys="ScreenMapping.device_id"
    )

    # Table constraints
    __table_args__ = (
        Index("idx_devices_name", "name"),
        Index("idx_devices_site_id", "site_id"),
        Index("idx_devices_created_at", "created_at", postgresql_ops={"created_at": "DESC"}),
    )

    def __repr__(self):
        return (
            f"<Device(id='{self.id}', name='{self.name}', "
            f"site_id='{self.site_id}', "
            f"new={self.new})>"
        )
