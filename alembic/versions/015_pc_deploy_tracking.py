"""Deploy tracking and station health: pc_deployments, pc_health, pc_token_renewals.

Plan 06 D13 (OD-22), shipped with backend 1.1.0 (M2):

* ``pc_deployments``: one row per deploy of a layout to a PC; id = config_id.
  FK to ``pcs`` with ON DELETE CASCADE, to ``screen_layouts`` with SET NULL.
  Status is CHECK-constrained; each state has an epoch timestamp, and
  ``failed_at`` may follow ``applied_at``. Indexes (pc_id, created_at) and
  (status, created_at).
* ``pc_health``: one row per PC with the latest StationStatusV1 fields, the
  register-time versions and C23's config-status block (lists and dicts as
  ``sa.JSON``).
* ``pc_token_renewals`` (D30): pc_id, previous_token_sha256, issued_at,
  confirmed_at. No token values.

Purely additive: ``pcs`` and every other table are untouched, so a downgrade
loses only the deploy history and health rows. ``sa.JSON`` only (no JSONB,
ARRAY or UUID). Upgrade and downgrade are idempotent and inspector-guarded,
like 012 and 014.

VERIFY ON A SCRATCH DATABASE (template0 + alembic) OR A RESTORED COPY OF
PRODUCTION, never the live database.

Revision ID: 015_pc_deploy_tracking
Revises: 014_actor_audit_stamps
Create Date: 2026-11-17
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "015_pc_deploy_tracking"
down_revision: Union[str, None] = "014_actor_audit_stamps"
branch_labels = None
depends_on = None


# Frozen copies of the model constants (app/models/pc_tracking.py) as of 015.
DEPLOYMENT_STATUSES = (
    "pending",
    "delivered",
    "received",
    "accepted",
    "applied",
    "failed",
    "unconfirmed",
    "superseded",
    "offline",
    "relay_failed",
)
HEALTH_STATES = ("ok", "degraded", "unhealthy", "stopped", "unknown")

TABLES = ("pc_deployments", "pc_health", "pc_token_renewals")
INDEXES = {
    "pc_deployments": (
        ("ix_pc_deployments_pc_id_created_at", ["pc_id", "created_at"]),
        ("ix_pc_deployments_status_created_at", ["status", "created_at"]),
    ),
    "pc_token_renewals": (("ix_pc_token_renewals_pc_id", ["pc_id"]),),
}


def _in_list(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
                  comment="Timestamp when the record was created"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
                  comment="Timestamp when the record was last updated"),
    ]


def _actor_stamps():
    return [
        sa.Column("created_by_type", sa.String(20), nullable=False, server_default="system",
                  comment="Actor kind that created the row: user|service_account|system"),
        sa.Column("created_by_id", sa.String(36), nullable=True,
                  comment="User UUID (as text) or service-account id of the creator; NULL for system"),
        sa.Column("created_by_label", sa.String(255), nullable=False, server_default="system",
                  comment="Cached display name of the creator at creation time"),
        sa.Column("updated_by_type", sa.String(20), nullable=False, server_default="system",
                  comment="Actor kind that last modified the row: user|service_account|system"),
        sa.Column("updated_by_id", sa.String(36), nullable=True,
                  comment="User UUID (as text) or service-account id of the last modifier; NULL for system"),
        sa.Column("updated_by_label", sa.String(255), nullable=False, server_default="system",
                  comment="Cached display name of the last modifier at modification time"),
    ]


def _create_pc_deployments():
    op.create_table(
        "pc_deployments",
        sa.Column("id", sa.String(64), nullable=False, comment="config_id of the deploy (minted by the backend, D14)"),
        sa.Column("pc_id", sa.String(50), nullable=False, comment="Target PC"),
        sa.Column("layout_id", sa.String(100), nullable=True,
                  comment="Screen layout that was deployed (NULL once the layout is deleted)"),
        sa.Column("layout_name", sa.String(255), nullable=True, comment="Layout name at deploy time"),
        sa.Column("config_hash", sa.String(64), nullable=True, comment="sha256 hex of the deployed payload"),
        sa.Column("screens", sa.Integer(), nullable=True, comment="Number of screens in the payload"),
        sa.Column("trigger", sa.String(32), nullable=False,
                  comment="What started the deploy, e.g. pc_deploy, layout_deploy, catch_up"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending",
                  comment="Deploy state, see DEPLOYMENT_STATUSES"),
        sa.Column("status_detail", sa.String(500), nullable=True,
                  comment="Reason or detail of the current state (no config, no URLs)"),
        sa.Column("confirmable", sa.Boolean(), nullable=False, server_default=sa.false(),
                  comment="The station can confirm applied (Client >= 1.4.0); 1.1.x rows end at received"),
        sa.Column("mode", sa.String(32), nullable=True,
                  comment="Delivery mode reported by the Client, e.g. live, fallback_file"),
        sa.Column("used_fallback", sa.Boolean(), nullable=True, comment="The Client fell back to file + restart (D8)"),
        sa.Column("persisted", sa.Boolean(), nullable=True, comment="Core persisted the layout (D7)"),
        sa.Column("source", sa.String(32), nullable=True,
                  comment="What confirmed the state, e.g. stdout, channel, timeout"),
        sa.Column("status_changed_at", sa.BigInteger(), nullable=True, comment="Unix time of the last state change"),
        sa.Column("delivered_at", sa.BigInteger(), nullable=True, comment="Unix time the relay delivered it"),
        sa.Column("received_at", sa.BigInteger(), nullable=True, comment="Unix time the Client acknowledged receipt"),
        sa.Column("accepted_at", sa.BigInteger(), nullable=True, comment="Unix time Core accepted it"),
        sa.Column("applied_at", sa.BigInteger(), nullable=True, comment="Unix time Core applied it"),
        sa.Column("failed_at", sa.BigInteger(), nullable=True, comment="Unix time it failed (may follow applied_at)"),
        sa.Column("unconfirmed_at", sa.BigInteger(), nullable=True, comment="Unix time it was marked unconfirmed"),
        sa.Column("superseded_at", sa.BigInteger(), nullable=True, comment="Unix time a newer deploy replaced it"),
        sa.Column("client_version", sa.String(64), nullable=True, comment="Client version that handled the deploy"),
        sa.Column("core_version", sa.String(64), nullable=True, comment="Core version that handled the deploy"),
        *_timestamps(),
        *_actor_stamps(),
        sa.PrimaryKeyConstraint("id", name="pc_deployments_pkey"),
        sa.ForeignKeyConstraint(["pc_id"], ["pcs.id"], name="fk_pc_deployments_pc", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["layout_id"], ["screen_layouts.id"], name="fk_pc_deployments_layout",
                                ondelete="SET NULL"),
        sa.CheckConstraint(_in_list("status", DEPLOYMENT_STATUSES), name="ck_pc_deployments_status"),
        sa.CheckConstraint("screens IS NULL OR screens >= 0", name="ck_pc_deployments_screens"),
    )


def _create_pc_health():
    op.create_table(
        "pc_health",
        sa.Column("pc_id", sa.String(50), nullable=False, comment="PC this row describes"),
        sa.Column("received_at", sa.BigInteger(), nullable=True,
                  comment="Unix time the relay received the last station_status"),
        sa.Column("reported_at", sa.BigInteger(), nullable=True, comment="Station clock (ts) of the last station_status"),
        sa.Column("registered_at", sa.BigInteger(), nullable=True,
                  comment="Unix time of the register that wrote the versions"),
        sa.Column("last_seen", sa.BigInteger(), nullable=True,
                  comment="Presence timestamp (D15 may move the sweep here, NEW-52)"),
        sa.Column("last_message_id", sa.String(64), nullable=True, comment="message_id of the last station_status"),
        sa.Column("health", sa.String(16), nullable=True, comment="ok, degraded, unhealthy, stopped or unknown"),
        sa.Column("reasons", sa.JSON(), nullable=True, comment="Health reason tokens"),
        sa.Column("client_version", sa.String(64), nullable=True, comment="Client version"),
        sa.Column("client_build", sa.String(64), nullable=True, comment="Client build"),
        sa.Column("core_version", sa.String(64), nullable=True, comment="Core version reported at register"),
        sa.Column("core_installed_version", sa.String(64), nullable=True, comment="Installed Core version"),
        sa.Column("core_running_version", sa.String(64), nullable=True, comment="Running Core version"),
        sa.Column("core_version_source", sa.String(32), nullable=True, comment="Where the Core version came from"),
        sa.Column("core_exe_size", sa.BigInteger(), nullable=True, comment="Size of the Core executable in bytes"),
        sa.Column("capabilities", sa.JSON(), nullable=True, comment="Client capabilities from register"),
        sa.Column("delivery_policy", sa.String(32), nullable=True, comment="live_with_fallback or file_restart"),
        sa.Column("core_state", sa.String(64), nullable=True, comment="CoreState phase, e.g. RUNNING"),
        sa.Column("core_pid", sa.BigInteger(), nullable=True, comment="Core process id"),
        sa.Column("core_build_class", sa.String(16), nullable=True, comment="legacy, reports_v1, reports_v2 or unknown"),
        sa.Column("core_exe_sha256", sa.String(64), nullable=True, comment="sha256 of the Core executable"),
        sa.Column("core_uptime_sec", sa.BigInteger(), nullable=True, comment="Core uptime in seconds"),
        sa.Column("core_restarts_24h", sa.Integer(), nullable=True, comment="Core restarts in the last 24 h"),
        sa.Column("core_last_exit", sa.String(10), nullable=True, comment="Last Core exit code as hex, e.g. 0x544C0002"),
        sa.Column("telemetry_mode", sa.String(32), nullable=True, comment="How the Client hears Core"),
        sa.Column("tap_state", sa.String(8), nullable=True, comment="Stdout tap state: ok, lossy or dead"),
        sa.Column("telemetry_channels", sa.JSON(), nullable=True, comment="Core report channels as the Client sees them"),
        sa.Column("running_config_id", sa.String(64), nullable=True, comment="config_id the wall runs"),
        sa.Column("deploying", sa.Boolean(), nullable=True, comment="A deploy is in progress on the station"),
        sa.Column("screens", sa.Integer(), nullable=True, comment="Screens on the wall"),
        sa.Column("streams_total", sa.Integer(), nullable=True, comment="Streams in the layout"),
        sa.Column("streams_playing", sa.Integer(), nullable=True, comment="Streams playing"),
        sa.Column("streams_connecting", sa.Integer(), nullable=True, comment="Streams connecting"),
        sa.Column("streams_failed", sa.JSON(), nullable=True, comment="Failed source ids (at most 50)"),
        sa.Column("streams_failed_count", sa.Integer(), nullable=True, comment="True number of failed streams"),
        sa.Column("flags", sa.JSON(), nullable=True, comment="Client flags {token: bool}"),
        sa.Column("readiness", sa.JSON(), nullable=True, comment="Station readiness issues [{code, detail}]"),
        sa.Column("relay_flags", sa.JSON(), nullable=True, comment="Relay-observed flags, e.g. token_outdated (D12)"),
        sa.Column("core_config_mode", sa.String(32), nullable=True, comment="C23 core_config_mode"),
        sa.Column("mode_set_by", sa.String(32), nullable=True, comment="C23 mode_set_by"),
        sa.Column("core_revision", sa.String(64), nullable=True, comment="C23 core_revision"),
        sa.Column("config_core_version", sa.String(64), nullable=True, comment="C23 core_version"),
        sa.Column("core_caps", sa.JSON(), nullable=True, comment="C23 core_caps"),
        sa.Column("migration_status", sa.String(32), nullable=True, comment="C23 migration_status"),
        sa.Column("drift_key_names", sa.JSON(), nullable=True, comment="C23 drift_key_names"),
        sa.Column("legacy_available", sa.Boolean(), nullable=True, comment="C23 legacy_available"),
        sa.Column("last_config_error", sa.String(200), nullable=True, comment="C23 last_config_error"),
        sa.Column("data_dir_acl_warning", sa.Boolean(), nullable=True, comment="C23 data_dir_acl_warning"),
        sa.Column("fleet_errors_count", sa.Integer(), nullable=True, comment="C23 fleet_errors_count"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("pc_id", name="pc_health_pkey"),
        sa.ForeignKeyConstraint(["pc_id"], ["pcs.id"], name="fk_pc_health_pc", ondelete="CASCADE"),
        sa.CheckConstraint(_in_list("health", HEALTH_STATES), name="ck_pc_health_health"),
    )


def _create_pc_token_renewals():
    op.create_table(
        "pc_token_renewals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="Row id"),
        sa.Column("pc_id", sa.String(50), nullable=False, comment="PC whose token was renewed"),
        sa.Column("previous_token_sha256", sa.String(64), nullable=False, comment="sha256 hex of the replaced token"),
        sa.Column("issued_at", sa.BigInteger(), nullable=False, comment="Unix time the new token was issued"),
        sa.Column("confirmed_at", sa.BigInteger(), nullable=True, comment="Unix time the PC confirmed the new token"),
        sa.PrimaryKeyConstraint("id", name="pc_token_renewals_pkey"),
        sa.ForeignKeyConstraint(["pc_id"], ["pcs.id"], name="fk_pc_token_renewals_pc", ondelete="CASCADE"),
    )


CREATORS = {
    "pc_deployments": _create_pc_deployments,
    "pc_health": _create_pc_health,
    "pc_token_renewals": _create_pc_token_renewals,
}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for table in TABLES:
        if table not in tables:
            CREATORS[table]()
    insp = sa.inspect(op.get_bind())
    for table, indexes in INDEXES.items():
        existing = {i["name"] for i in insp.get_indexes(table)}
        for name, columns in indexes:
            if name not in existing:
                op.create_index(name, table, columns)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for table in reversed(TABLES):
        if table not in tables:
            continue
        existing = {i["name"] for i in insp.get_indexes(table)}
        for name, _columns in INDEXES.get(table, ()):
            if name in existing:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
