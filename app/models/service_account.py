"""
Service-account auth models.

Non-human principals (GuardDesk in v1) authenticate with scoped Bearer tokens
(prefix `tlsa_` for grep-ability). Layered on top of the existing JWT auth used
for human users — see `app/api/deps.py` ServiceAccount dependency.
"""

from sqlalchemy import (
    Column, String, Text, Boolean, ForeignKey, Index, DateTime,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.base import TimestampMixin


class ServiceAccount(Base, TimestampMixin):
    """A non-human principal with one or more scoped tokens."""
    __tablename__ = "service_accounts"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    scopes = Column(ARRAY(String(64)), nullable=False, server_default="{}")
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_by = Column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    tokens = relationship(
        "ServiceAccountToken",
        back_populates="service_account",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_service_accounts_active", "is_active"),
    )


class ServiceAccountToken(Base):
    """
    Individual bearer tokens. The raw token is returned to the caller exactly once
    on creation; only token_hash is stored (bcrypt or argon2 hash of `tlsa_<random>`).
    """
    __tablename__ = "service_account_tokens"

    id = Column(String(36), primary_key=True)
    service_account_id = Column(
        String(36),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(Text, nullable=False)
    name = Column(String(255), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=__import__("sqlalchemy").func.now(),
    )

    service_account = relationship("ServiceAccount", back_populates="tokens")

    __table_args__ = (
        Index("ix_sa_tokens_lookup", "revoked_at", "expires_at"),
    )
