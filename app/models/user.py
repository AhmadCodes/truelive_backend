"""
User model for authentication and user management.
"""

from sqlalchemy import (
    Column, String, Boolean, DateTime, UUID,
    CheckConstraint, Index, ForeignKey, text
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class User(BaseModel):
    """
    User model for authentication and authorization.

    Attributes:
        user_id: Primary key (UUID)
        username: Unique username
        email: Unique email address with format validation
        password_hash: Hashed password (bcrypt or Argon2)
        role: User role (user, admin, super_admin)
        is_active: Whether the user account is active
        created_by: Reference to user who created this account
        last_login: Timestamp of last successful login
    """
    __tablename__ = "users"

    user_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="Unique user identifier"
    )
    username = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique username for login"
    )
    email = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="User email address"
    )
    password_hash = Column(
        String(255),
        nullable=False,
        comment="Hashed password (bcrypt or Argon2)"
    )
    role = Column(
        String(50),
        nullable=False,
        index=True,
        comment="User role: user, admin, or super_admin"
    )
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether the user account is active"
    )
    created_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        comment="User who created this account"
    )
    last_login = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Timestamp of last successful login"
    )

    # Relationships
    creator = relationship(
        "User",
        remote_side=[user_id],
        back_populates="created_users",
        foreign_keys=[created_by]
    )
    created_users = relationship(
        "User",
        back_populates="creator",
        foreign_keys=[created_by]
    )
    invitation_tokens = relationship(
        "InvitationToken",
        back_populates="user",
        cascade="all, delete-orphan"
    )
    audit_logs = relationship(
        "AuditLog",
        back_populates="user",
        foreign_keys="AuditLog.user_id"
    )

    # Table constraints
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'admin', 'super_admin')",
            name="valid_role"
        ),
        CheckConstraint(
            "email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}$'",
            name="email_format"
        ),
        Index("idx_users_email", "email"),
        Index("idx_users_username", "username"),
        Index("idx_users_role", "role"),
        Index("idx_users_is_active", "is_active"),
        Index("idx_users_last_login", "last_login", postgresql_ops={"last_login": "DESC"}),
    )

    def __repr__(self):
        return (
            f"<User(user_id={self.user_id}, username='{self.username}', "
            f"email='{self.email}', role='{self.role}', is_active={self.is_active})>"
        )


class InvitationToken(BaseModel):
    """
    Invitation token model for user registration.

    Attributes:
        token_id: Primary key (UUID)
        token: Unique invitation token string
        user_id: Reference to the invited user
        expires_at: Token expiration timestamp
        is_used: Whether the token has been used
        used_at: Timestamp when token was used
        used_from_ip: IP address from which token was used
    """
    __tablename__ = "invitation_tokens"

    token_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="Unique token identifier"
    )
    token = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique invitation token string"
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who received the invitation"
    )
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="Token expiration timestamp"
    )
    is_used = Column(
        Boolean,
        nullable=False,
        default=False,
        index=True,
        comment="Whether the token has been used"
    )
    used_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when token was used"
    )
    used_from_ip = Column(
        String(45),  # IPv6 compatible (INET type)
        nullable=True,
        comment="IP address from which token was used"
    )

    # Relationships
    user = relationship(
        "User",
        back_populates="invitation_tokens"
    )

    # Table constraints
    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at",
            name="valid_expiration"
        ),
        Index("idx_invitation_tokens_token", "token"),
        Index("idx_invitation_tokens_user_id", "user_id"),
        Index("idx_invitation_tokens_expires_at", "expires_at"),
        Index("idx_invitation_tokens_is_used", "is_used"),
    )

    def __repr__(self):
        return (
            f"<InvitationToken(token_id={self.token_id}, user_id={self.user_id}, "
            f"is_used={self.is_used}, expires_at={self.expires_at})>"
        )


class AuditLog(BaseModel):
    """
    Audit log model for tracking user actions and system events.

    Attributes:
        id: Primary key (UUID)
        user_id: Reference to user who performed the action
        action: Action identifier (e.g., 'site.created', 'user.updated')
        resource_type: Type of resource affected
        resource_id: ID of the affected resource
        changes: JSON object containing the changes made
        ip_address: IP address of the user
        user_agent: User agent string from the request
    """
    __tablename__ = "audit_logs"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        comment="Unique audit log identifier"
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="User who performed the action"
    )
    action = Column(
        String(100),
        nullable=False,
        index=True,
        comment="Action identifier (e.g., 'site.created', 'user.updated')"
    )
    resource_type = Column(
        String(50),
        nullable=False,
        index=True,
        comment="Type of resource affected"
    )
    resource_id = Column(
        String(255),
        nullable=True,
        comment="ID of the affected resource"
    )
    changes = Column(
        String,  # JSONB type - will store JSON as text
        nullable=True,
        comment="JSON object containing the changes made"
    )
    ip_address = Column(
        String(45),  # IPv6 compatible (INET type)
        nullable=True,
        comment="IP address of the user"
    )
    user_agent = Column(
        String,
        nullable=True,
        comment="User agent string from the request"
    )

    # Relationships
    user = relationship(
        "User",
        back_populates="audit_logs",
        foreign_keys=[user_id]
    )

    # Table constraints
    __table_args__ = (
        Index("idx_audit_logs_user_id", "user_id"),
        Index("idx_audit_logs_action", "action"),
        Index("idx_audit_logs_resource_type", "resource_type"),
        Index("idx_audit_logs_created_at", "created_at", postgresql_ops={"created_at": "DESC"}),
    )

    def __repr__(self):
        return (
            f"<AuditLog(id={self.id}, user_id={self.user_id}, action='{self.action}', "
            f"resource_type='{self.resource_type}', resource_id='{self.resource_id}')>"
        )
