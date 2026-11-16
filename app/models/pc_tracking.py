"""
Deploy tracking and station health storage (plan 06 D13, migration 015; OD-22).

* ``pc_deployments``: one row per deploy of a layout to a PC. The primary key is
  the deploy's ``config_id`` (D14 mints it; 64 characters max, D1). The row
  walks the deploy states (D14, D15); each state has its own epoch timestamp
  (Unix seconds, like ``pcs.last_applied``), and ``failed_at`` may follow
  ``applied_at`` (a late Failed, OD-18).
* ``pc_health``: one row per PC with the latest StationStatusV1 (D1, D15),
  the register-time versions (E20) and C23's config-status block. Lists and
  dicts are ``sa.JSON``. ``relay_flags`` holds what the relay observed
  (token_outdated, duplicate_registration, D12) apart from the Client's own
  ``flags``.
* ``pc_token_renewals`` (D30): one row per PC token renewal. It stores the
  sha256 of the replaced token, never a token.

No JSONB, ARRAY or UUID columns: every table also creates on SQLite on its own
(NEW-49). No relationship is declared on ``PC``, so ``pcs`` is unchanged and
deleting a PC relies on the database's ON DELETE CASCADE.
"""

from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    case,
    false,
    func,
    select,
)

from app.database import Base
from app.models.base import ActorStampMixin, BaseModel

# Every state a pc_deployments row can be in (D14, D15, D17, D18):
#   pending       row committed, not yet handed to the relay (D14)
#   delivered     the relay handed the layout to the PC's session (D14)
#   received      the Client acknowledged the Socket.IO message (D14); the
#                 terminal state of Client 1.1.x rows
#   accepted      Core parsed the layout (ack OK, D15)
#   applied       Core finished the request (OD-18 (b), D15)
#   failed        Core, the Client or the backend reported a failure
#   unconfirmed   no confirmation arrived in time (D15 sweep)
#   superseded    a newer deploy to the same PC replaced it
#   offline       the PC was not connected to the relay (D14)
#   relay_failed  the relay call failed or never answered (D14, D15 sweep)
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

# StationStatusV1.health (D1).
HEALTH_STATES = ("ok", "degraded", "unhealthy", "stopped", "unknown")


def _in_list(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PcDeployment(BaseModel, ActorStampMixin):
    """One deploy of a layout to one PC. ``created_by_*`` is who deployed."""

    __tablename__ = "pc_deployments"

    id = Column(String(64), primary_key=True, comment="config_id of the deploy (minted by the backend, D14)")
    pc_id = Column(
        String(50),
        ForeignKey("pcs.id", ondelete="CASCADE", name="fk_pc_deployments_pc"),
        nullable=False,
        comment="Target PC",
    )
    layout_id = Column(
        String(100),
        ForeignKey("screen_layouts.id", ondelete="SET NULL", name="fk_pc_deployments_layout"),
        nullable=True,
        comment="Screen layout that was deployed (NULL once the layout is deleted)",
    )
    layout_name = Column(String(255), nullable=True, comment="Layout name at deploy time")
    config_hash = Column(String(64), nullable=True, comment="sha256 hex of the deployed payload")
    screens = Column(Integer, nullable=True, comment="Number of screens in the payload")
    trigger = Column(String(32), nullable=False, comment="What started the deploy, e.g. pc_deploy, layout_deploy, catch_up")
    status = Column(String(20), nullable=False, server_default="pending", comment="Deploy state, see DEPLOYMENT_STATUSES")
    status_detail = Column(String(500), nullable=True, comment="Reason or detail of the current state (no config, no URLs)")
    confirmable = Column(
        Boolean, nullable=False, server_default=false(),
        comment="The station can confirm applied (Client >= 1.4.0); 1.1.x rows end at received",
    )
    mode = Column(String(32), nullable=True, comment="Delivery mode reported by the Client, e.g. live, fallback_file")
    used_fallback = Column(Boolean, nullable=True, comment="The Client fell back to file + restart (D8)")
    persisted = Column(Boolean, nullable=True, comment="Core persisted the layout (D7)")
    source = Column(String(32), nullable=True, comment="What confirmed the state, e.g. stdout, channel, timeout")
    status_changed_at = Column(BigInteger, nullable=True, comment="Unix time of the last state change")
    delivered_at = Column(BigInteger, nullable=True, comment="Unix time the relay delivered it")
    received_at = Column(BigInteger, nullable=True, comment="Unix time the Client acknowledged receipt")
    accepted_at = Column(BigInteger, nullable=True, comment="Unix time Core accepted it")
    applied_at = Column(BigInteger, nullable=True, comment="Unix time Core applied it")
    failed_at = Column(BigInteger, nullable=True, comment="Unix time it failed (may follow applied_at)")
    unconfirmed_at = Column(BigInteger, nullable=True, comment="Unix time it was marked unconfirmed")
    superseded_at = Column(BigInteger, nullable=True, comment="Unix time a newer deploy replaced it")
    client_version = Column(String(64), nullable=True, comment="Client version that handled the deploy")
    core_version = Column(String(64), nullable=True, comment="Core version that handled the deploy")

    __table_args__ = (
        CheckConstraint(_in_list("status", DEPLOYMENT_STATUSES), name="ck_pc_deployments_status"),
        CheckConstraint("screens IS NULL OR screens >= 0", name="ck_pc_deployments_screens"),
        Index("ix_pc_deployments_pc_id_created_at", "pc_id", "created_at"),
        Index("ix_pc_deployments_status_created_at", "status", "created_at"),
    )

    def __repr__(self):
        return f"<PcDeployment(id='{self.id}', pc_id='{self.pc_id}', status='{self.status}')>"


class PcHealth(BaseModel):
    """Latest station status of one PC (StationStatusV1 + register versions)."""

    __tablename__ = "pc_health"

    pc_id = Column(
        String(50),
        ForeignKey("pcs.id", ondelete="CASCADE", name="fk_pc_health_pc"),
        primary_key=True,
        comment="PC this row describes",
    )
    received_at = Column(BigInteger, nullable=True, comment="Unix time the relay received the last station_status")
    reported_at = Column(BigInteger, nullable=True, comment="Station clock (ts) of the last station_status")
    registered_at = Column(BigInteger, nullable=True, comment="Unix time of the register that wrote the versions")
    last_seen = Column(BigInteger, nullable=True, comment="Presence timestamp (D15 may move the sweep here, NEW-52)")
    last_message_id = Column(String(64), nullable=True, comment="message_id of the last station_status")
    health = Column(String(16), nullable=True, comment="ok, degraded, unhealthy, stopped or unknown")
    reasons = Column(JSON, nullable=True, comment="Health reason tokens")
    # Versions (E20): RegisterV1 and StationStatusV1.
    client_version = Column(String(64), nullable=True, comment="Client version")
    client_build = Column(String(64), nullable=True, comment="Client build")
    core_version = Column(String(64), nullable=True, comment="Core version reported at register")
    core_installed_version = Column(String(64), nullable=True, comment="Installed Core version")
    core_running_version = Column(String(64), nullable=True, comment="Running Core version")
    core_version_source = Column(String(32), nullable=True, comment="Where the Core version came from")
    core_exe_size = Column(BigInteger, nullable=True, comment="Size of the Core executable in bytes")
    capabilities = Column(JSON, nullable=True, comment="Client capabilities from register")
    delivery_policy = Column(String(32), nullable=True, comment="live_with_fallback or file_restart")
    # StationStatusV1.core
    core_state = Column(String(64), nullable=True, comment="CoreState phase, e.g. RUNNING")
    core_pid = Column(BigInteger, nullable=True, comment="Core process id")
    core_build_class = Column(String(16), nullable=True, comment="legacy, reports_v1, reports_v2 or unknown")
    core_exe_sha256 = Column(String(64), nullable=True, comment="sha256 of the Core executable")
    core_uptime_sec = Column(BigInteger, nullable=True, comment="Core uptime in seconds")
    core_restarts_24h = Column(Integer, nullable=True, comment="Core restarts in the last 24 h")
    core_last_exit = Column(String(10), nullable=True, comment="Last Core exit code as hex, e.g. 0x544C0002")
    # StationStatusV1.telemetry
    telemetry_mode = Column(String(32), nullable=True, comment="How the Client hears Core")
    tap_state = Column(String(8), nullable=True, comment="Stdout tap state: ok, lossy or dead")
    telemetry_channels = Column(JSON, nullable=True, comment="Core report channels as the Client sees them")
    # StationStatusV1.wall
    running_config_id = Column(String(64), nullable=True, comment="config_id the wall runs")
    deploying = Column(Boolean, nullable=True, comment="A deploy is in progress on the station")
    screens = Column(Integer, nullable=True, comment="Screens on the wall")
    streams_total = Column(Integer, nullable=True, comment="Streams in the layout")
    streams_playing = Column(Integer, nullable=True, comment="Streams playing")
    streams_connecting = Column(Integer, nullable=True, comment="Streams connecting")
    streams_failed = Column(JSON, nullable=True, comment="Failed source ids (at most 50)")
    streams_failed_count = Column(Integer, nullable=True, comment="True number of failed streams")
    # Flags and readiness
    flags = Column(JSON, nullable=True, comment="Client flags {token: bool}")
    readiness = Column(JSON, nullable=True, comment="Station readiness issues [{code, detail}]")
    relay_flags = Column(JSON, nullable=True, comment="Relay-observed flags, e.g. token_outdated (D12)")
    # StationStatusV1.config_status (C23): key names, never values.
    core_config_mode = Column(String(32), nullable=True, comment="C23 core_config_mode")
    mode_set_by = Column(String(32), nullable=True, comment="C23 mode_set_by")
    core_revision = Column(String(64), nullable=True, comment="C23 core_revision")
    config_core_version = Column(String(64), nullable=True, comment="C23 core_version")
    core_caps = Column(JSON, nullable=True, comment="C23 core_caps")
    migration_status = Column(String(32), nullable=True, comment="C23 migration_status")
    drift_key_names = Column(JSON, nullable=True, comment="C23 drift_key_names")
    legacy_available = Column(Boolean, nullable=True, comment="C23 legacy_available")
    last_config_error = Column(String(200), nullable=True, comment="C23 last_config_error")
    data_dir_acl_warning = Column(Boolean, nullable=True, comment="C23 data_dir_acl_warning")
    fleet_errors_count = Column(Integer, nullable=True, comment="C23 fleet_errors_count")

    __table_args__ = (
        CheckConstraint(_in_list("health", HEALTH_STATES), name="ck_pc_health_health"),
    )

    def __repr__(self):
        return f"<PcHealth(pc_id='{self.pc_id}', health='{self.health}')>"


class PcTokenRenewal(Base):
    """One PC token renewal (D30). Never holds a token value."""

    __tablename__ = "pc_token_renewals"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="Row id")
    pc_id = Column(
        String(50),
        ForeignKey("pcs.id", ondelete="CASCADE", name="fk_pc_token_renewals_pc"),
        nullable=False,
        comment="PC whose token was renewed",
    )
    previous_token_sha256 = Column(String(64), nullable=False, comment="sha256 hex of the replaced token")
    issued_at = Column(BigInteger, nullable=False, comment="Unix time the new token was issued")
    confirmed_at = Column(BigInteger, nullable=True, comment="Unix time the PC confirmed the new token")

    __table_args__ = (Index("ix_pc_token_renewals_pc_id", "pc_id"),)

    def __repr__(self):
        return f"<PcTokenRenewal(id={self.id}, pc_id='{self.pc_id}')>"


# --------------------------------------------------------------------------- #
# The "latest" deployment of a PC
# --------------------------------------------------------------------------- #


def latest_deployment_order():
    """ORDER BY of the latest deployment: non-superseded rows first, then the
    greatest created_at, ties broken by the greatest id (D13, D16). When every
    row of a PC is superseded, the most recent superseded row is the latest."""
    return (
        case((PcDeployment.status == "superseded", 1), else_=0),
        PcDeployment.created_at.desc(),
        PcDeployment.id.desc(),
    )


def latest_deployment_rank():
    """``row_number()`` per PC in latest-first order; rank 1 is the latest."""
    return func.row_number().over(partition_by=PcDeployment.pc_id, order_by=latest_deployment_order())


def latest_deployments_subquery():
    """``(pc_id, id)`` of every PC's latest deployment, one row per PC."""
    ranked = select(
        PcDeployment.pc_id.label("pc_id"),
        PcDeployment.id.label("id"),
        latest_deployment_rank().label("rank"),
    ).subquery()
    return select(ranked.c.pc_id, ranked.c.id).where(ranked.c.rank == 1).subquery("latest_deployments")


def latest_deployment(db, pc_id: str) -> Optional[PcDeployment]:
    """The latest deployment of ``pc_id``, or None."""
    return (
        db.query(PcDeployment)
        .filter(PcDeployment.pc_id == pc_id)
        .order_by(*latest_deployment_order())
        .first()
    )
