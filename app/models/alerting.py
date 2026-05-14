"""
SQLAlchemy models for the alerting feature.

Receives Calipsa SMTP alerts via Postfix -> truelive-smtp-ingest, persists raw mail and
normalized alerts to Postgres + MinIO, and forwards them to a downstream consumer
(one downstream platform in v1). See experiments/alerting_feature/feature_description.md.

The high-volume tables (raw_messages, alerts, alert_media) use Postgres native range
partitioning by month. Partition management lives in alembic + a celery beat job.

Cross-table foreign keys that would cross partition boundaries (alert_media.alert_id
-> alerts.id) are intentionally not declared at the DB level — Postgres can't enforce
an FK to a partitioned table without including the partition key, which is impractical
here. Integrity is maintained in application code.
"""

from sqlalchemy import (
    Column, String, Text, Boolean, ForeignKey, Index, Integer,
    DateTime, CheckConstraint, BigInteger, Float, PrimaryKeyConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.base import TimestampMixin


# Status values used in raw_messages.status. String column with CHECK so we can extend
# without an enum-rename migration dance.
RAW_MESSAGE_STATUSES = (
    "received", "parsed", "parse_failed", "forwarded", "forward_failed", "quarantined",
)

# parser.confidence values
PARSER_CONFIDENCES = ("exact", "heuristic", "llm_generated", "unparsed")

ALERT_EVENT_TYPES = ("motion", "person", "vehicle", "intrusion", "unknown")

ALERT_MEDIA_KINDS = ("snapshot", "video_clip", "attachment_other")


class AlertAddress(Base, TimestampMixin):
    """
    Per-camera inbound email address. 1:N from camera so addresses can be rotated.

    The opaque local part (e.g. `cam-Xb3p9Hf2NkLqW8aZ`) is pasted into Calipsa as the
    alert destination for that camera. Hot path: LMTP RCPT TO lookup uses the partial
    index on `local_part WHERE is_active AND NOT is_quarantined`.
    """
    __tablename__ = "alert_addresses"

    id = Column(String(36), primary_key=True)
    camera_id = Column(
        String(255),
        ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    local_part = Column(String(64), nullable=False)
    domain = Column(String(255), nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    is_quarantined = Column(Boolean, nullable=False, server_default="false")
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    camera = relationship("Camera", lazy="select")

    __table_args__ = (
        Index("ux_alert_addresses_local_domain", "local_part", "domain", unique=True),
        Index(
            "ix_alert_addresses_active_lookup",
            "local_part",
            postgresql_where=(Column("is_active") == True) & (Column("is_quarantined") == False),  # noqa: E712
        ),
    )


class RawMessage(Base):
    """
    Durable inbound store. One row per envelope+data accepted at LMTP. Partitioned
    monthly by received_at.

    A row is inserted BEFORE the LMTP 250 ACK so we never lose a message that the
    sender thinks is delivered. Status transitions: received -> parsed | parse_failed,
    then -> forwarded | forward_failed | quarantined.
    """
    __tablename__ = "raw_messages"

    id = Column(String(36), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    envelope_from = Column(Text, nullable=True)
    envelope_to = Column(Text, nullable=True)
    camera_id = Column(
        String(255),
        ForeignKey("cameras.id", ondelete="SET NULL"),
        nullable=True,
    )
    alert_address_id = Column(
        String(36),
        ForeignKey("alert_addresses.id", ondelete="SET NULL"),
        nullable=True,
    )
    size_bytes = Column(Integer, nullable=False)
    storage_uri = Column(Text, nullable=False)
    sender_ip = Column(INET, nullable=True)
    helo = Column(String(255), nullable=True)
    spf_result = Column(String(32), nullable=True)
    dkim_result = Column(String(32), nullable=True)
    dmarc_result = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False, server_default="received")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", "received_at", name="pk_raw_messages"),
        CheckConstraint(
            f"status IN {RAW_MESSAGE_STATUSES!r}",
            name="ck_raw_messages_status",
        ),
        Index("ix_raw_messages_camera_received", "camera_id", "received_at"),
        Index(
            "ix_raw_messages_reconcile",
            "status",
            postgresql_where=Column("status").in_(("received", "parse_failed")),
        ),
        {"postgresql_partition_by": "RANGE (received_at)"},
    )


class Alert(Base):
    """
    Normalized alert. 1:1 with RawMessage. Partitioned monthly by received_at to
    align with raw_messages retention.
    """
    __tablename__ = "alerts"

    id = Column(String(36), nullable=False)
    raw_message_id = Column(String(36), nullable=False)
    camera_id = Column(
        String(255),
        ForeignKey("cameras.id", ondelete="CASCADE"),
        nullable=False,
    )
    received_at = Column(DateTime(timezone=True), nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=True)
    event_type = Column(String(32), nullable=False, server_default="unknown")
    event_subtype = Column(String(64), nullable=True)
    confidence = Column(Float, nullable=True)
    parser_id = Column(String(64), nullable=True)
    parser_version = Column(Integer, nullable=True)
    parser_confidence = Column(String(16), nullable=False, server_default="unparsed")
    subject = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    extra = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", "received_at", name="pk_alerts"),
        CheckConstraint(
            f"parser_confidence IN {PARSER_CONFIDENCES!r}",
            name="ck_alerts_parser_confidence",
        ),
        Index("ix_alerts_camera_received", "camera_id", "received_at"),
        Index("ix_alerts_received_at", "received_at"),
        Index("ix_alerts_raw_message", "raw_message_id"),
        {"postgresql_partition_by": "RANGE (received_at)"},
    )


class AlertMedia(Base):
    """
    Extracted attachments stored in MinIO. Partitioned monthly by created_at.

    alert_id is intentionally NOT a foreign key — alerts is partitioned and a cross-
    partition FK is impractical. App code maintains integrity.
    """
    __tablename__ = "alert_media"

    id = Column(String(36), nullable=False)
    alert_id = Column(String(36), nullable=False)
    kind = Column(String(32), nullable=False, server_default="attachment_other")
    content_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, nullable=False)
    storage_uri = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=True)
    sha256 = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_alert_media"),
        CheckConstraint(
            f"kind IN {ALERT_MEDIA_KINDS!r}",
            name="ck_alert_media_kind",
        ),
        Index("ix_alert_media_alert", "alert_id"),
        Index("ix_alert_media_sha256", "sha256"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )
