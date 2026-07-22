"""
PC (Personal Computer) model for managing controller and manager PCs.
"""

from sqlalchemy import (
    Column,
    String,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class PC(BaseModel):
    """
    PC model representing controller and manager PCs in the system.

    Attributes:
        id: Unique identifier for the PC
        name: Display name of the PC
        ip_address: IPv4 or IPv6 address
        gpu_type: GPU type/model installed
        role: PC role (controller or manager)
        manager_id: Reference to managing PC (self-referencing)
        auth_token: Authentication token for PC
        token_expiry: Unix timestamp when token expires
        last_connected: Unix timestamp of last connection
        last_applied: Unix timestamp of last configuration applied
    """

    __tablename__ = "pcs"

    # Primary key
    id = Column(String(50), primary_key=True, comment="Unique identifier for the PC")

    # Basic information
    name = Column(String(255), nullable=False, comment="Display name of the PC")

    ip_address = Column(
        String(45),  # IPv6 compatible
        nullable=True,
        comment="IPv4 or IPv6 address of the PC",
    )

    gpu_type = Column(
        String(100), nullable=True, comment="GPU type/model installed on the PC"
    )

    # Role and management
    role = Column(
        String(20),
        nullable=False,
        default="controller",
        comment="PC role (controller or manager)",
    )

    manager_id = Column(
        String(50),
        ForeignKey("pcs.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID of the managing PC (for controller PCs)",
    )

    # Assigned screen layout
    screen_layout_id = Column(
        String(100),
        ForeignKey(
            "screen_layouts.id", ondelete="SET NULL", name="fk_pcs_screen_layout"
        ),
        nullable=True,
        comment="ID of the screen layout assigned to this PC",
    )

    # Authentication
    auth_token = Column(Text, nullable=True, comment="Authentication token for PC")

    token_expiry = Column(
        BigInteger,
        nullable=True,
        comment="Unix timestamp when authentication token expires",
    )

    # Connection tracking
    last_connected = Column(
        BigInteger, nullable=True, comment="Unix timestamp of last connection"
    )

    last_seen = Column(
        BigInteger,
        nullable=True,
        comment=(
            "Unix timestamp the PC was last seen alive on the websocket "
            "(rolled by the heartbeat presence sweep)"
        ),
    )

    last_applied = Column(
        BigInteger,
        nullable=True,
        comment="Unix timestamp of last configuration applied",
    )

    # Constraints
    __table_args__ = (
        CheckConstraint("role IN ('controller', 'manager')", name="check_pc_role"),
        # Indexes
        Index("idx_pcs_role", "role"),
        Index("idx_pcs_manager_id", "manager_id"),
        Index(
            "idx_pcs_last_connected",
            "last_connected",
            postgresql_ops={"last_connected": "DESC"},
        ),
        Index(
            "idx_pcs_last_seen",
            "last_seen",
            postgresql_ops={"last_seen": "DESC"},
        ),
        Index("idx_pcs_name", "name"),
        Index("idx_pcs_screen_layout_id", "screen_layout_id"),
    )

    # Relationships
    # Self-referencing relationship for manager
    manager = relationship(
        "PC",
        remote_side=[id],
        foreign_keys=[manager_id],
        backref="controlled_pcs",
        doc="Manager PC for this controller PC",
    )

    # Relationship to assigned screen layout
    screen_layout = relationship(
        "ScreenLayout",
        back_populates="pcs",
        foreign_keys=[screen_layout_id],
        doc="Screen layout assigned to this PC",
    )

    def __repr__(self):
        """String representation of PC."""
        return f"<PC(id='{self.id}', name='{self.name}', role='{self.role}')>"

    def to_dict(self):
        """
        Convert PC instance to dictionary.

        Returns:
            Dictionary representation of the PC
        """
        return {
            "id": self.id,
            "name": self.name,
            "ip_address": self.ip_address,
            "gpu_type": self.gpu_type,
            "role": self.role,
            "manager_id": self.manager_id,
            "screen_layout_id": self.screen_layout_id,
            "auth_token": self.auth_token,
            "token_expiry": self.token_expiry,
            "last_connected": self.last_connected,
            "last_seen": self.last_seen,
            "last_applied": self.last_applied,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
