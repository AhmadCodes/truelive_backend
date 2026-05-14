"""
Webhook delivery models for the alerting feature.

`webhook_consumers` stores the downstream destination (GuardDesk in v1) with an
encrypted HMAC secret. The schema supports N consumers; v1 only ever has one active.

`webhook_deliveries` is one row per POST attempt — partitioned monthly by
attempted_at, kept ~30 days. Source of truth for retry / give-up state.
"""

from sqlalchemy import (
    Column, String, Text, Boolean, ForeignKey, Index, Integer,
    DateTime, CheckConstraint, PrimaryKeyConstraint, func,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.base import TimestampMixin


DELIVERY_STATUSES = ("pending", "success", "failed", "giving_up")


class WebhookConsumer(Base, TimestampMixin):
    """
    A downstream consumer of normalized alerts. GuardDesk registers itself by
    POSTing to /alerting/webhook-consumers with its URL and a secret of its choice.
    """
    __tablename__ = "webhook_consumers"

    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    url = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)  # encrypted at rest
    is_active = Column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        Index("ix_webhook_consumers_active", "is_active"),
    )


class WebhookDelivery(Base):
    """
    One row per POST attempt. Partitioned by attempted_at monthly with shorter
    retention than alerts themselves (30d).

    consumer_id has a real FK; alert_id is loose (alerts is partitioned).
    """
    __tablename__ = "webhook_deliveries"

    id = Column(String(36), nullable=False)
    alert_id = Column(String(36), nullable=False)
    consumer_id = Column(
        String(36),
        ForeignKey("webhook_consumers.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt = Column(Integer, nullable=False, server_default="1")
    status = Column(String(16), nullable=False, server_default="pending")
    http_status = Column(Integer, nullable=True)
    response_excerpt = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    consumer = relationship("WebhookConsumer", lazy="select")

    __table_args__ = (
        PrimaryKeyConstraint("id", "attempted_at", name="pk_webhook_deliveries"),
        CheckConstraint(
            f"status IN {DELIVERY_STATUSES!r}",
            name="ck_webhook_deliveries_status",
        ),
        Index("ix_webhook_deliveries_alert", "alert_id"),
        Index("ix_webhook_deliveries_consumer", "consumer_id", "attempted_at"),
        {"postgresql_partition_by": "RANGE (attempted_at)"},
    )
