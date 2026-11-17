"""
Relay ingestion on Postgres with real processes (plan 06 D15).

* test_health_flush_on_postgres: station_reports.upsert_health writes a batch
  with one INSERT ... ON CONFLICT per column set (chunked), updates in place,
  keeps register versions when a status omits them, drops rows of deleted PCs;
  and the relay's flush_health_once writes everything buffered as one batch.
* test_relay_lifecycle_over_sockets: the real eventlet relay (Python 3.11)
  with short flush/sweep intervals, a current Client (capabilities
  deploy_result.v1 and station_status.v1) and a 1.1.x-like Client, both
  python-socketio, driven by the real deploy_service: the current Client's
  deploy goes received -> accepted -> applied (last_applied stamped) -> late
  failed; the 1.1.x row ends at received and is stamped; station_status lands
  in pc_health with the register versions; duplicates, stale sessions and
  unknown events behave; the timeout sweep moves stale rows; pcs.updated_at
  never moves; with RELAY_STATION_REPORTS=off every report answers disabled.
* test_reconnect_storm: 200 PCs (python-socketio AsyncClients) connect and
  register at once and all get ``registered`` within 5 s; their register rows
  reach pc_health in one flush.

Prerequisites (failing, never skipping, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL) and a Python 3.11 for the relay
(TRUELIVE_RELAY_PYTHON). Databases are built by alembic from template0 and
dropped; everything binds 127.0.0.1; throwaway secrets only.

    python -m pytest tests/integration/test_relay_reports_pg.py -o addopts='' -p no:cacheprovider
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import jwt
import pytest
import socketio
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.schemas import station_protocol as sp
from app.services import deploy_service as ds
from app.services import station_reports
from tests.deploy_testkit import patch_generator, synthetic_layout
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, Proc, _admin_connection, _base_env, _free_port

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

RELAY_PYTHON = os.environ.get("TRUELIVE_RELAY_PYTHON", sys.executable)
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
MARKER = "d15-layout-marker-" + secrets.token_hex(4)
CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
# What ``registered`` advertises: the report features, station_config.v1 (C10)
# and pc_token.v1 (D30).
FEATURES = CAPS + [sp.FEATURE_STATION_CONFIG, sp.FEATURE_PC_TOKEN]
VERSIONS = {"client_version": "1.4.0", "client_build": "1.4.0+0000000", "core_running_version": "1.1.0+d53952a",
            "core_build_class": "reports_v1", "delivery_policy": "live_with_fallback"}


@pytest.fixture(scope="module")
def scratch_db():
    name = f"d15_reports_{os.getpid()}_{secrets.token_hex(3)}"
    admin = _admin_connection()
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}" OWNER truelive TEMPLATE template0 ENCODING \'UTF8\' '
                    "LC_COLLATE 'C' LC_CTYPE 'C'")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(BACKEND_ROOT),
                                env=_base_env(DATABASE_URL=url), capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def pg(scratch_db):
    engine = create_engine(scratch_db, pool_size=10, max_overflow=10)
    with engine.begin() as conn:
        for table in ("audit_logs", "pc_health", "pc_deployments", "pcs", "screen_layouts", "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    yield scratch_db, engine, sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _seed(Session, pc_ids, tokens=None, updated_at=None):
    with Session() as db:
        db.add(Team(id="TEAM_d15", name="D15"))
        db.add(ScreenLayout(id="LAYOUT_d15", name="D15 wall", team_id="TEAM_d15"))
        db.flush()
        db.bulk_insert_mappings(PC, [
            {"id": pc_id, "name": f"{pc_id} wall", "role": "controller", "team_id": "TEAM_d15",
             "screen_layout_id": "LAYOUT_d15", "auth_token": (tokens or {}).get(pc_id)}
            for pc_id in pc_ids
        ])
        db.commit()
        if updated_at is not None:
            db.execute(PC.__table__.update().values(updated_at=updated_at))
            db.commit()


def _mint(pc_id, secret):
    return jwt.encode({"pc_id": pc_id, "name": f"{pc_id} wall", "exp": int(time.time()) + 8760 * 3600,
                       "type": "pc_auth"},
                      secret, algorithm="HS256")


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _status_row(health="ok", **wall):
    return {"received_at": int(time.time()), "reported_at": int(time.time()), "last_message_id": uuid.uuid4().hex,
            "health": health, "reasons": ["no_video"], "flags": {"lossy": False},
            "readiness": [{"code": "AUTOLOGON_OFF", "detail": None}], "streams_failed": ["src-1"],
            "telemetry_channels": {"heartbeat": {"target": "listener", "connected": True,
                                                 "last_message_age_sec": 2}}, **wall}


# --------------------------------------------------------------------------- #
# Health flush on Postgres
# --------------------------------------------------------------------------- #


def test_health_flush_on_postgres(pg, monkeypatch):
    url, engine, Session = pg
    ids = [f"PC_{i:03d}" for i in range(300)]
    _seed(Session, ids)
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO PC_HEALTH"):
            statements.append(statement)

    batch = {}
    for i, pc_id in enumerate(ids):
        if i < 200:
            batch[pc_id] = _status_row("ok" if i % 2 else "degraded", streams_playing=i)
        else:
            batch[pc_id] = {"registered_at": 1_800_000_000, "relay_flags": {"token_outdated": True},
                            "client_version": "1.4.0", "capabilities": CAPS}
    batch["PC_GONE"] = _status_row()
    monkeypatch.setattr(station_reports, "UPSERT_CHUNK", 120)
    with Session() as db:
        written, dropped = station_reports.upsert_health(db, batch)
        db.commit()
    assert (written, dropped) == (300, 1)
    assert len(statements) == 2 + 1  # 200 status rows in 2 chunks of <=120, 100 register rows in 1
    assert all("ON CONFLICT (pc_id) DO UPDATE" in s for s in statements)
    with Session() as db:
        assert db.query(PcHealth).count() == 300
        row = db.get(PcHealth, "PC_007")
        assert (row.health, row.streams_playing, row.reasons, row.flags, row.readiness, row.streams_failed) == (
            "ok", 7, ["no_video"], {"lossy": False}, [{"code": "AUTOLOGON_OFF", "detail": None}], ["src-1"])
        assert row.telemetry_channels == {"heartbeat": {"target": "listener", "connected": True,
                                                        "last_message_age_sec": 2}}
        assert db.get(PcHealth, "PC_250").relay_flags == {"token_outdated": True}
        created = db.get(PcHealth, "PC_007").created_at

    # A second batch updates in place; columns it does not carry are kept.
    statements.clear()
    with Session() as db:
        station_reports.upsert_health(db, {"PC_007": _status_row("unhealthy"),
                                           "PC_250": {"health": "ok", "received_at": int(time.time())}})
        db.commit()
    assert len(statements) == 2
    with Session() as db:
        assert db.query(PcHealth).count() == 300
        row = db.get(PcHealth, "PC_007")
        assert row.health == "unhealthy" and row.created_at == created and row.updated_at >= created
        row = db.get(PcHealth, "PC_250")
        assert (row.health, row.client_version, row.capabilities) == ("ok", "1.4.0", CAPS)

    # The relay's flush writes everything buffered as one batch (one transaction).
    from tests.relay_testkit import import_relay

    relay = import_relay()
    relay.health_buffer.clear()
    monkeypatch.setattr(relay, "_open_db", lambda: Session())
    for pc_id in ids[:50]:
        for n in range(3):
            relay.health_buffer.put(pc_id, {"health": "stopped" if n == 2 else "ok", "received_at": n})
    statements.clear()
    batches_before = relay.relay_counters["health_batches"]
    assert relay.flush_health_once() == 50
    assert relay.relay_counters["health_batches"] == batches_before + 1 and len(statements) == 1
    with Session() as db:
        assert {r.health for r in db.query(PcHealth).filter(PcHealth.pc_id.in_(ids[:50]))} == {"stopped"}
    event.remove(engine, "before_cursor_execute", _count)


# --------------------------------------------------------------------------- #
# Real relay helpers
# --------------------------------------------------------------------------- #


class Relay:
    def __init__(self, db_url, jwt_secret, sender_secret, **env):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        base = _base_env(
            DATABASE_URL=db_url, JWT_SECRET=jwt_secret, WEBSOCKET_PORT=str(self.port),
            RELAY_SENDER_SECRET=sender_secret, RELAY_DEBUG_LOGGING="false",
            WS_LAST_SEEN_SWEEP_SECONDS="1", RELAY_HEALTH_FLUSH_SECONDS="1", RELAY_DEPLOY_SWEEP_SECONDS="1",
        )
        base.update(env)
        base.pop("TRUELIVE_RELAY_ASYNC_MODE", None)
        self.proc = Proc([RELAY_PYTHON, "-u", "-m", "app.services.websocket_server"], base)
        self.proc.wait_for("Relay reports:", timeout=60)
        import socket as socket_

        def listening():
            try:
                with socket_.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                return False

        _wait(listening, 30, "relay did not start listening")

    def stop(self):
        self.proc.stop()


class Pc:
    """A python-socketio station. ``capabilities=None`` registers like 1.1.x."""

    def __init__(self, url, pc_id, token, capabilities=None, versions=None):
        self.pc_id = pc_id
        self.configs = []
        self.registered = None
        self.errors = []
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self.sio.on("config", lambda data: self.configs.append(data))
        self.sio.on("registered", lambda data: setattr(self, "registered", data))
        self.sio.on("error", lambda data: self.errors.append(data))
        self.sio.connect(url, wait_timeout=10)
        payload = {"pc_id": pc_id, "auth_token": token, "name": f"{pc_id} wall"}
        if capabilities is not None:
            payload["capabilities"] = capabilities
        payload.update(versions or {})
        self.sio.emit("register", payload)
        _wait(lambda: self.registered or self.errors, 10, f"{pc_id}: no registered")
        assert self.registered, self.errors

    def call(self, event_name, data, timeout=5):
        return self.sio.call(event_name, data, timeout=timeout)

    def close(self):
        try:
            self.sio.disconnect()
        except Exception:
            pass


def _result(config_id, status, message_id=None, **extra):
    return {"message_id": message_id or uuid.uuid4().hex, "config_id": config_id, "status": status,
            "ts": time.time(), **extra}


# --------------------------------------------------------------------------- #
# Relay lifecycle over sockets
# --------------------------------------------------------------------------- #


def test_relay_lifecycle_over_sockets(pg, monkeypatch):
    db_url, engine, Session = pg
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    tokens = {pc: _mint(pc, jwt_secret) for pc in ("PC_NEW", "PC_OLD")}
    _seed(Session, list(tokens), tokens, updated_at=OLD)
    patch_generator(monkeypatch, {pc: synthetic_layout(pc, marker=MARKER) for pc in tokens})
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)

    relay = Relay(db_url, jwt_secret, sender_secret)
    stations = []
    try:
        monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
        new = Pc(relay.url, "PC_NEW", tokens["PC_NEW"], capabilities=CAPS, versions=VERSIONS)
        old = Pc(relay.url, "PC_OLD", tokens["PC_OLD"])
        stations += [new, old]
        assert new.registered["features"] == FEATURES and old.registered["features"] == FEATURES
        assert new.registered["protocol_hash"] == sp.protocol_hash()

        with Session() as db:
            pcs = db.query(PC).order_by(PC.id).all()
            summary = ds.deploy_config_to_pcs(pcs, db, trigger=ds.TRIGGER_LAYOUT_DEPLOY)
        results = {r["pc_id"]: r for r in summary["results"]}
        assert (results["PC_NEW"]["confirmable"], results["PC_OLD"]["confirmable"]) == (True, False)
        new_id, old_id = results["PC_NEW"]["config_id"], results["PC_OLD"]["config_id"]

        def status_of(config_id):
            with Session() as db:
                return db.get(PcDeployment, config_id).status

        _wait(lambda: status_of(new_id) == "received" and status_of(old_id) == "received", 2,
              "rows did not reach received")
        with Session() as db:
            assert db.get(PC, "PC_OLD").last_applied is not None  # 1.1.x: stamped on receipt
            assert db.get(PC, "PC_NEW").last_applied is None  # confirmable: waits for applied

        # The current Client reports accepted, then applied.
        assert new.call("deploy_result", _result(new_id, "accepted", "acc-1")) == {"status": "ok",
                                                                                  "message_id": "acc-1"}
        assert status_of(new_id) == "accepted"
        ack = new.call("deploy_result", _result(new_id, "applied", "app-1", mode="live", persisted=True,
                                                source="stdout"))
        assert ack == {"status": "ok", "message_id": "app-1"}
        with Session() as db:
            row = db.get(PcDeployment, new_id)
            assert (row.status, row.mode, row.persisted, row.client_version) == ("applied", "live", True, "1.4.0")
            assert db.get(PC, "PC_NEW").last_applied == row.applied_at
        assert new.call("deploy_result", _result(new_id, "applied", "app-1"))["status"] == "duplicate"
        # A late Failed.
        assert new.call("deploy_result", _result(new_id, "failed", detail="Core restarted"))["status"] == "ok"
        assert status_of(new_id) == "failed"
        # Another PC's deploy and an invalid report are rejected.
        assert new.call("deploy_result", _result(old_id, "applied"))["reason"] == "config_id belongs to another PC"
        assert new.call("deploy_result", {"message_id": "x", "status": "applied"})["status"] == "rejected"
        assert status_of(old_id) == "received"

        # station_status reaches pc_health within a flush, with the register versions.
        assert new.call("station_status", {"message_id": "st-1", "ts": time.time(), "health": "degraded",
                                           "reasons": ["no_video"],
                                           "wall": {"running_config_id": new_id, "streams_total": 4,
                                                    "streams_playing": 3}})["status"] == "ok"

        def health():
            with Session() as db:
                row = db.get(PcHealth, "PC_NEW")
                return row if row is not None and row.health == "degraded" else None

        row = _wait(health, 4, "station_status never reached pc_health")
        assert (row.running_config_id, row.streams_playing, row.client_version, row.core_build_class,
                row.capabilities, row.relay_flags) == (new_id, 3, "1.4.0", "reports_v1", CAPS, {})
        with Session() as db:
            old_health = db.get(PcHealth, "PC_OLD")
            assert old_health.registered_at and old_health.health is None and old_health.client_version is None

        # Unknown events get no ACK: the call times out.
        with pytest.raises(socketio.exceptions.TimeoutError):
            new.call("get_station_config_v9", {}, timeout=2)

        # A replaced (stale) session can no longer report.
        replacement = Pc(relay.url, "PC_NEW", tokens["PC_NEW"], capabilities=CAPS, versions=VERSIONS)
        stations.append(replacement)
        assert new.call("station_status", {"message_id": "st-2", "ts": time.time(), "health": "ok"}) == {
            "status": "rejected", "reason": "not registered"}

        # The timeout sweep: a pending row too old, a confirmable delivered row past the ACK timeout.
        now = int(time.time())
        with Session() as db:
            db.add(PcDeployment(id="stale-pending", pc_id="PC_OLD", trigger="pc_deploy", status="pending",
                                status_changed_at=now - 120))
            db.add(PcDeployment(id="stale-delivered", pc_id="PC_NEW", trigger="pc_deploy", status="delivered",
                                confirmable=True, status_changed_at=now - 400, delivered_at=now - 400))
            db.add(PcDeployment(id="old-111", pc_id="PC_OLD", trigger="pc_deploy", status="received",
                                confirmable=False, status_changed_at=now - 9000, delivered_at=now - 9000))
            db.commit()
        _wait(lambda: status_of("stale-pending") == "relay_failed" and status_of("stale-delivered") == "unconfirmed",
              5, "the sweep did not move the stale rows")
        assert status_of("old-111") == "received"

        # Presence never touched pcs.updated_at (register stamps, several 1 s sweeps).
        time.sleep(1.5)
        with Session() as db:
            for pc in db.query(PC).all():
                assert pc.updated_at == OLD, pc.id
                assert pc.last_seen and pc.last_seen >= now - 5
    finally:
        for station in stations:
            station.close()
        relay.stop()
    log = relay.proc.text
    assert MARKER not in log and "fixture-user" not in log
    for value in list(tokens.values()) + [jwt_secret, sender_secret]:
        assert value not in log
    assert "features=deploy_result.v1,station_status.v1,station_config.v1,pc_token.v1" in log
    assert "Relay reports: station_reports=on health_flush=1s deploy_sweep=1s ack_timeout=300s" in log

    # Kill switch: RELAY_STATION_REPORTS=off.
    relay = Relay(db_url, jwt_secret, sender_secret, RELAY_STATION_REPORTS="off")
    stations = []
    try:
        monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
        new = Pc(relay.url, "PC_NEW", tokens["PC_NEW"], capabilities=CAPS, versions=VERSIONS)
        stations.append(new)
        assert new.registered["features"] == [sp.FEATURE_STATION_CONFIG, sp.FEATURE_PC_TOKEN,
                                              sp.FEATURE_STATION_REPORTS_DISABLED]
        assert new.call("deploy_result", _result(new_id, "applied")) == {"status": "disabled"}
        assert new.call("station_status", {"message_id": "st-9", "ts": time.time(), "health": "ok"}) == {
            "status": "disabled"}
        with Session() as db:
            summary = ds.deploy_config_to_pcs([db.get(PC, "PC_NEW")], db, trigger=ds.TRIGGER_PC_DEPLOY)
        result = summary["results"][0]
        assert result["status"] == "deployed" and result["confirmable"] is False  # deploys still flow
        _wait(lambda: status_of(result["config_id"]) == "received", 2, "no receipt with reports off")
    finally:
        for station in stations:
            station.close()
        relay.stop()
    assert "Relay reports: station_reports=off" in relay.proc.text


# --------------------------------------------------------------------------- #
# Reconnect storm
# --------------------------------------------------------------------------- #


def test_reconnect_storm(pg):
    db_url, engine, Session = pg
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    ids = [f"PC_S{i:03d}" for i in range(200)]
    tokens = {pc: _mint(pc, jwt_secret) for pc in ids}
    _seed(Session, ids, tokens, updated_at=OLD)
    relay = Relay(db_url, jwt_secret, sender_secret)

    async def storm():
        clients = []
        registered = {}
        errors = []

        async def one(pc_id):
            client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
            done = asyncio.get_running_loop().create_future()

            @client.on("registered")
            async def _registered(data):
                if not done.done():
                    done.set_result(data)

            @client.on("error")
            async def _error(data):
                errors.append((pc_id, data))

            clients.append(client)
            await client.connect(relay.url, transports=["polling"], wait_timeout=15)
            await client.emit("register", {"pc_id": pc_id, "auth_token": tokens[pc_id], "capabilities": CAPS,
                                           "client_version": "1.4.0"})
            data = await asyncio.wait_for(done, 15)
            registered[pc_id] = (time.monotonic(), data)

        started = time.monotonic()
        try:
            await asyncio.gather(*(one(pc_id) for pc_id in ids))
            elapsed = max(t for t, _ in registered.values()) - started
        finally:
            await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)
        return elapsed, registered, errors

    try:
        elapsed, registered, errors = asyncio.run(storm())
        assert not errors and len(registered) == 200
        assert elapsed < 5.0, f"200 registrations took {elapsed:.2f}s"
        assert all(data["features"] == FEATURES for _, data in registered.values())

        def all_health():
            with Session() as db:
                return db.query(PcHealth).filter(PcHealth.registered_at.isnot(None)).count() == 200

        _wait(all_health, 5, "register rows did not reach pc_health")
        with Session() as db:
            assert {r.client_version for r in db.query(PcHealth).all()} == {"1.4.0"}
            assert all(pc.updated_at == OLD and pc.last_connected for pc in db.query(PC).all())
    finally:
        relay.stop()
    print(f"reconnect storm: 200 registrations in {elapsed:.2f}s")
    log = relay.proc.text
    assert "Health flush failed" not in log and "Error during registration" not in log
