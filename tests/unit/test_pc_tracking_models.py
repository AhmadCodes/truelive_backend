"""
Storage models of migration 015 (plan 06 D13): "model create on SQLite".

* Each new table creates on SQLite on its own, with nothing else in the
  database (NEW-49: no JSONB, ARRAY or UUID).
* With pcs, teams and screen_layouts next to them (foreign keys enforced):
  JSON round trips, the CHECK constraints, ON DELETE CASCADE from pcs,
  ON DELETE SET NULL from screen_layouts, failed_at after applied_at, and the
  "latest deployment" order (non-superseded first, greatest created_at, ties
  by greatest id).
* pc_health has a column for every StationStatusV1 field (D1), including the
  E20 versions and C23's config-status block.

The Postgres half (migration round trip, restricted autogenerate, pcs
unchanged) is tests/integration/test_migration_015.py.

    python -m pytest tests/unit/test_pc_tracking_models.py -o addopts='' -p no:cacheprovider
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import JSON, MetaData, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every mapper the ORM needs)
from app.models.pc import PC
from app.models.pc_tracking import (
    DEPLOYMENT_STATUSES,
    HEALTH_STATES,
    PcDeployment,
    PcHealth,
    PcTokenRenewal,
    latest_deployment,
    latest_deployments_subquery,
)
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.schemas import station_protocol as sp

NEW_TABLES = (PcDeployment.__table__, PcHealth.__table__, PcTokenRenewal.__table__)
T0 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def _sqlite_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@pytest.mark.parametrize("table", NEW_TABLES, ids=lambda t: t.name)
def test_each_table_creates_alone_on_sqlite(table):
    engine = _sqlite_engine()
    table.create(engine)  # nothing else exists: no pcs, no screen_layouts
    with engine.connect() as conn:
        names = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")]
    assert names == [table.name]
    for column in table.columns:
        type_name = type(column.type).__name__.upper()
        assert type_name not in ("JSONB", "ARRAY", "UUID"), column.name
        assert type(column.type).__module__.startswith("sqlalchemy.sql"), (column.name, type(column.type))
    engine.dispose()


@pytest.fixture
def db():
    """teams, screen_layouts, pcs and the three new tables on SQLite."""
    engine = _sqlite_engine()
    meta = MetaData()
    copies = [model.__table__.to_metadata(meta) for model in (Team, ScreenLayout, PC)]
    copies += [table.to_metadata(meta) for table in NEW_TABLES]
    meta.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    session.add(Team(id="TEAM_t", name="Team"))
    session.add(ScreenLayout(id="LAYOUT_1", name="Lobby wall", team_id="TEAM_t"))
    for pc_id in ("PC_A", "PC_B"):
        session.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id="TEAM_t"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _deployment(pc_id, config_id, created_at, status="pending", **fields):
    return PcDeployment(id=config_id, pc_id=pc_id, trigger="pc_deploy", status=status, created_at=created_at,
                        updated_at=created_at, **fields)


def test_deployment_row_round_trip(db):
    db.add(_deployment("PC_A", "cfg-1", T0, layout_id="LAYOUT_1", layout_name="Lobby wall", config_hash="a" * 64,
                       screens=3, status_detail="delivered to the station", created_by_type="user",
                       created_by_id="7b3f0a52-0000-4000-8000-000000000001", created_by_label="operator"))
    db.commit()
    row = db.get(PcDeployment, "cfg-1")
    assert row.status == "pending"
    assert row.confirmable is False  # server default
    assert (row.screens, row.layout_name, row.created_by_label, row.updated_by_type) == (3, "Lobby wall", "operator",
                                                                                       "system")
    # Every state can be stamped, and a late Failed may follow Applied (OD-18).
    row.status, row.applied_at, row.failed_at, row.status_changed_at = "failed", 1_800_000_000, 1_800_000_030, 1_800_000_030
    row.confirmable, row.persisted, row.used_fallback = True, True, False
    db.commit()
    db.expire_all()
    row = db.get(PcDeployment, "cfg-1")
    assert (row.applied_at, row.failed_at, row.confirmable, row.persisted) == (1_800_000_000, 1_800_000_030, True, True)


def test_every_status_is_allowed_and_others_are_not(db):
    for i, status in enumerate(DEPLOYMENT_STATUSES):
        db.add(_deployment("PC_A", f"cfg-{i}", T0, status=status))
    db.commit()
    db.add(_deployment("PC_A", "cfg-bad", T0, status="deployed"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(_deployment("PC_A", "cfg-neg", T0, screens=-1))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deploy_result_statuses_are_storable():
    """Every DeployResultV1 status (D1) has a pc_deployments state."""
    assert set(sp.DEPLOY_STATUSES) <= set(DEPLOYMENT_STATUSES)
    assert set(HEALTH_STATES) == set(sp.HEALTH_STATES)


def test_trigger_and_pc_are_required(db):
    db.add(PcDeployment(id="cfg-x", pc_id="PC_A", created_at=T0, updated_at=T0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(_deployment("PC_NOPE", "cfg-y", T0))  # FK to pcs
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_health_row_round_trip_and_check(db):
    channels = {"heartbeat": {"target": "listener", "connected": True, "last_message_age_sec": 4}}
    db.add(PcHealth(pc_id="PC_A", health="degraded", reasons=["streams_failed"], received_at=1_800_000_000,
                    flags={"port_conflict": False}, relay_flags={"token_outdated": True},
                    readiness=[{"code": "AUTOLOGON_OFF"}], streams_failed=["src-1", "src-2"],
                    telemetry_channels=channels, capabilities=["delivery_policy"], core_caps=["config_id"],
                    drift_key_names=["heartbeat_server_address"], core_pid=4294967295, core_last_exit="0x544C0002"))
    db.commit()
    db.expire_all()
    row = db.get(PcHealth, "PC_A")
    assert row.telemetry_channels == channels and row.streams_failed == ["src-1", "src-2"]
    assert row.relay_flags == {"token_outdated": True} and row.readiness == [{"code": "AUTOLOGON_OFF"}]
    assert row.core_pid == 4294967295
    db.add(PcHealth(pc_id="PC_B", health="fine"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(PcHealth(pc_id="PC_B"))  # health may be absent (row written at register, E20)
    db.commit()


def test_renewal_rows_hold_no_token(db):
    db.add(PcTokenRenewal(pc_id="PC_A", previous_token_sha256="b" * 64, issued_at=1_800_000_000))
    db.commit()
    row = db.query(PcTokenRenewal).one()
    assert row.id == 1 and row.confirmed_at is None
    assert {c.name for c in PcTokenRenewal.__table__.columns} == {
        "id", "pc_id", "previous_token_sha256", "issued_at", "confirmed_at"}


def test_deleting_a_pc_cascades_and_a_layout_sets_null(db):
    db.add_all([
        _deployment("PC_A", "cfg-a", T0, layout_id="LAYOUT_1"),
        _deployment("PC_B", "cfg-b", T0, layout_id="LAYOUT_1"),
        PcHealth(pc_id="PC_A", health="ok"),
        PcTokenRenewal(pc_id="PC_A", previous_token_sha256="c" * 64, issued_at=1),
    ])
    db.commit()
    db.query(PC).filter(PC.id == "PC_A").delete()
    db.commit()
    assert [r.id for r in db.query(PcDeployment).all()] == ["cfg-b"]
    assert db.query(PcHealth).count() == 0 and db.query(PcTokenRenewal).count() == 0
    db.query(ScreenLayout).filter(ScreenLayout.id == "LAYOUT_1").delete()
    db.commit()
    db.expire_all()
    assert db.get(PcDeployment, "cfg-b").layout_id is None


def test_latest_deployment_order(db):
    assert latest_deployment(db, "PC_A") is None
    db.add_all([
        _deployment("PC_A", "cfg-old", T0, status="applied"),
        _deployment("PC_A", "cfg-new", T0 + timedelta(seconds=5), status="received"),
        _deployment("PC_A", "cfg-newest-superseded", T0 + timedelta(seconds=9), status="superseded"),
        # same-second tie on PC_B: the greater id wins
        _deployment("PC_B", "cfg-b-1", T0, status="delivered"),
        _deployment("PC_B", "cfg-b-2", T0, status="delivered"),
    ])
    db.commit()
    assert latest_deployment(db, "PC_A").id == "cfg-new"
    assert latest_deployment(db, "PC_B").id == "cfg-b-2"
    latest = latest_deployments_subquery()
    assert sorted(db.execute(select(latest.c.pc_id, latest.c.id)).all()) == [("PC_A", "cfg-new"), ("PC_B", "cfg-b-2")]
    # When every row is superseded, the most recent one is still returned.
    for row in db.query(PcDeployment).filter(PcDeployment.pc_id == "PC_A"):
        row.status = "superseded"
    db.commit()
    assert latest_deployment(db, "PC_A").id == "cfg-newest-superseded"


def _station_status_fields():
    """(field path, pc_health column) for every StationStatusV1 field."""
    renames = {
        "message_id": "last_message_id",
        "ts": "reported_at",
        "core.state": "core_state", "core.pid": "core_pid", "core.build_class": "core_build_class",
        "core.exe_sha256": "core_exe_sha256", "core.uptime_sec": "core_uptime_sec",
        "core.restarts_24h": "core_restarts_24h", "core.last_exit": "core_last_exit",
        "telemetry.mode": "telemetry_mode", "telemetry.tap_state": "tap_state",
        "telemetry.channels": "telemetry_channels",
        "config_status.core_version": "config_core_version",
    }
    blocks = {"core": sp.CoreStatusV1, "telemetry": sp.TelemetryV1, "wall": sp.WallV1,
              "config_status": sp.ConfigStatusV1}
    out = []
    for name in sp.StationStatusV1.model_fields:
        if name in blocks:
            for sub in blocks[name].model_fields:
                path = f"{name}.{sub}"
                out.append((path, renames.get(path, sub)))
        else:
            out.append((name, renames.get(name, name)))
    return out


def test_pc_health_holds_every_station_status_field():
    columns = {c.name: c for c in PcHealth.__table__.columns}
    fields = _station_status_fields()
    assert len(fields) >= 40  # 11 top-level + 29 in the four blocks (D1 at 015)
    for path, column in fields:
        assert column in columns, f"StationStatusV1.{path} has no pc_health column ({column})"
    for name in ("reasons", "flags", "readiness", "streams_failed", "telemetry_channels", "core_caps",
                 "drift_key_names", "capabilities", "relay_flags"):
        assert isinstance(columns[name].type, JSON), name
    # E20: the register-time versions too.
    for field in ("client_version", "client_build", "core_version", "core_installed_version", "core_running_version",
                  "core_version_source", "core_exe_size", "capabilities", "delivery_policy"):
        assert field in sp.RegisterV1.model_fields and field in columns, field


def test_pcs_model_is_unchanged():
    """D13 adds no column or relationship to pcs (the migration never touches it)."""
    assert {c.name for c in PC.__table__.columns} == {
        "id", "name", "ip_address", "gpu_type", "role", "manager_id", "screen_layout_id", "team_id", "auth_token",
        "token_expiry", "last_connected", "last_seen", "last_applied", "created_at", "updated_at",
        "created_by_type", "created_by_id", "created_by_label", "updated_by_type", "updated_by_id", "updated_by_label",
    }
    assert set(PC.__mapper__.relationships.keys()) == {"manager", "screen_layout", "team", "controlled_pcs"}
