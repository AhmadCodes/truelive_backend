"""Alerting feature tables (alert_addresses, raw_messages, alerts, alert_media,
webhook_consumers, webhook_deliveries, service_accounts, service_account_tokens).

Revision ID: 007_alerting_feature_tables
Revises: 006_add_use_tcp_to_cameras
Create Date: 2026-05-14

High-volume tables (raw_messages, alerts, alert_media, webhook_deliveries) use
Postgres native range partitioning by month. The migration creates the parent table
plus six months of partitions starting from the deploy month. A periodic celery
beat task (`rollover_alerting_partitions`) extends the range forward.

Downgrade drops everything; the parent DROP TABLE cascades to partitions.
"""

from datetime import date, timedelta
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '007_alerting_feature_tables'
down_revision: Union[str, None] = '006_add_use_tcp_to_cameras'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that are partitioned by month — (table_name, partition_column)
_PARTITIONED = (
    ("raw_messages", "received_at"),
    ("alerts", "received_at"),
    ("alert_media", "created_at"),
    ("webhook_deliveries", "attempted_at"),
)


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _month_partitions(base: date, count: int):
    """Yield (suffix, start_inclusive, end_exclusive) for `count` consecutive months."""
    cur = _month_start(base)
    for _ in range(count):
        nxt = _next_month(cur)
        yield f"{cur.year:04d}_{cur.month:02d}", cur, nxt
        cur = nxt


def _create_partitions(table: str, count: int = 6) -> None:
    """Create `count` monthly partitions starting one month before the migration date.

    Slight lookback so a backdated ingest (clock skew, retry storm replay) lands in
    a real partition rather than failing on insert.
    """
    base = _month_start(date.today()) - timedelta(days=1)  # last day of previous month
    for suffix, start, end in _month_partitions(_month_start(base), count):
        op.execute(
            f"CREATE TABLE {table}_p{suffix} PARTITION OF {table} "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # alert_addresses (NOT partitioned — small, hot-path lookup)
    # ------------------------------------------------------------------
    op.create_table(
        "alert_addresses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "camera_id", sa.String(length=255),
            sa.ForeignKey("cameras.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("local_part", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_quarantined", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alert_addresses_camera_id", "alert_addresses", ["camera_id"])
    op.create_index(
        "ux_alert_addresses_local_domain",
        "alert_addresses", ["local_part", "domain"],
        unique=True,
    )
    op.execute(
        "CREATE INDEX ix_alert_addresses_active_lookup ON alert_addresses (local_part) "
        "WHERE is_active = true AND is_quarantined = false"
    )

    # ------------------------------------------------------------------
    # raw_messages (PARTITIONED BY received_at)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE raw_messages (
            id VARCHAR(36) NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            envelope_from TEXT,
            envelope_to TEXT,
            camera_id VARCHAR(255) REFERENCES cameras(id) ON DELETE SET NULL,
            alert_address_id VARCHAR(36) REFERENCES alert_addresses(id) ON DELETE SET NULL,
            size_bytes INTEGER NOT NULL,
            storage_uri TEXT NOT NULL,
            sender_ip INET,
            helo VARCHAR(255),
            spf_result VARCHAR(32),
            dkim_result VARCHAR(32),
            dmarc_result VARCHAR(32),
            status VARCHAR(32) NOT NULL DEFAULT 'received',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_raw_messages PRIMARY KEY (id, received_at),
            CONSTRAINT ck_raw_messages_status CHECK (
                status IN ('received','parsed','parse_failed','forwarded','forward_failed','quarantined')
            )
        ) PARTITION BY RANGE (received_at)
    """)
    op.execute("CREATE INDEX ix_raw_messages_camera_received ON raw_messages (camera_id, received_at)")
    op.execute(
        "CREATE INDEX ix_raw_messages_reconcile ON raw_messages (status) "
        "WHERE status IN ('received','parse_failed')"
    )
    _create_partitions("raw_messages")

    # ------------------------------------------------------------------
    # alerts (PARTITIONED BY received_at)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alerts (
            id VARCHAR(36) NOT NULL,
            raw_message_id VARCHAR(36) NOT NULL,
            camera_id VARCHAR(255) NOT NULL REFERENCES cameras(id) ON DELETE CASCADE,
            received_at TIMESTAMPTZ NOT NULL,
            detected_at TIMESTAMPTZ,
            event_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
            event_subtype VARCHAR(64),
            confidence DOUBLE PRECISION,
            parser_id VARCHAR(64),
            parser_version INTEGER,
            parser_confidence VARCHAR(16) NOT NULL DEFAULT 'unparsed',
            subject TEXT,
            body_text TEXT,
            extra JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_alerts PRIMARY KEY (id, received_at),
            CONSTRAINT ck_alerts_parser_confidence CHECK (
                parser_confidence IN ('exact','heuristic','llm_generated','unparsed')
            )
        ) PARTITION BY RANGE (received_at)
    """)
    op.execute("CREATE INDEX ix_alerts_camera_received ON alerts (camera_id, received_at)")
    op.execute("CREATE INDEX ix_alerts_received_at ON alerts (received_at)")
    op.execute("CREATE INDEX ix_alerts_raw_message ON alerts (raw_message_id)")
    _create_partitions("alerts")

    # ------------------------------------------------------------------
    # alert_media (PARTITIONED BY created_at)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE alert_media (
            id VARCHAR(36) NOT NULL,
            alert_id VARCHAR(36) NOT NULL,
            kind VARCHAR(32) NOT NULL DEFAULT 'attachment_other',
            content_type VARCHAR(128),
            size_bytes BIGINT NOT NULL,
            storage_uri TEXT NOT NULL,
            original_filename TEXT,
            sha256 VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_alert_media PRIMARY KEY (id, created_at),
            CONSTRAINT ck_alert_media_kind CHECK (
                kind IN ('snapshot','video_clip','attachment_other')
            )
        ) PARTITION BY RANGE (created_at)
    """)
    op.execute("CREATE INDEX ix_alert_media_alert ON alert_media (alert_id)")
    op.execute("CREATE INDEX ix_alert_media_sha256 ON alert_media (sha256)")
    _create_partitions("alert_media")

    # ------------------------------------------------------------------
    # webhook_consumers (NOT partitioned)
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_consumers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_webhook_consumers_active", "webhook_consumers", ["is_active"])

    # ------------------------------------------------------------------
    # webhook_deliveries (PARTITIONED BY attempted_at)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE webhook_deliveries (
            id VARCHAR(36) NOT NULL,
            alert_id VARCHAR(36) NOT NULL,
            consumer_id VARCHAR(36) NOT NULL REFERENCES webhook_consumers(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL DEFAULT 1,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            http_status INTEGER,
            response_excerpt TEXT,
            error TEXT,
            attempted_at TIMESTAMPTZ NOT NULL,
            next_retry_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_webhook_deliveries PRIMARY KEY (id, attempted_at),
            CONSTRAINT ck_webhook_deliveries_status CHECK (
                status IN ('pending','success','failed','giving_up')
            )
        ) PARTITION BY RANGE (attempted_at)
    """)
    op.execute("CREATE INDEX ix_webhook_deliveries_alert ON webhook_deliveries (alert_id)")
    op.execute("CREATE INDEX ix_webhook_deliveries_consumer ON webhook_deliveries (consumer_id, attempted_at)")
    _create_partitions("webhook_deliveries")

    # ------------------------------------------------------------------
    # service_accounts + service_account_tokens
    # ------------------------------------------------------------------
    op.create_table(
        "service_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scopes", sa.dialects.postgresql.ARRAY(sa.String(length=64)), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_by", sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_service_accounts_active", "service_accounts", ["is_active"])

    op.create_table(
        "service_account_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "service_account_id", sa.String(length=36),
            sa.ForeignKey("service_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sa_tokens_lookup", "service_account_tokens", ["revoked_at", "expires_at"])
    op.create_index("ix_sa_tokens_service_account", "service_account_tokens", ["service_account_id"])


def downgrade() -> None:
    # Drop in reverse FK order. Partitioned parents cascade to partitions on DROP.
    op.drop_table("service_account_tokens")
    op.drop_table("service_accounts")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.drop_table("webhook_consumers")
    op.execute("DROP TABLE IF EXISTS alert_media")
    op.execute("DROP TABLE IF EXISTS alerts")
    op.execute("DROP TABLE IF EXISTS raw_messages")
    op.drop_table("alert_addresses")
