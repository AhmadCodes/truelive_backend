"""
Deploy path on Postgres with real processes (plan 06 D14).

* test_two_session_race_on_scratch_postgres: two API sessions deploying to
  one PC at the same moment are serialised by the PC row lock, and exactly
  one row stays live (the other is superseded); a deploy waits while another
  transaction holds the PC row; and the API's ``delivered`` racing the
  relay's ``received`` / ``accepted`` writes in another session never moves
  a row backwards, and stamps pcs.last_applied exactly when the 1.1.x
  receipt wins.
* test_real_relay_plus_fake_pc: the real eventlet relay (Python 3.11) plus
  python-socketio PCs that ACK like Client 1.1.x, driven by the real
  deploy_service sender: each PC receives its layout with the row's
  config_id at the root, rows reach ``received`` within 2 s and stamp
  last_applied; an offline PC is failed and not stamped; the audit rows are
  written without layouts; a wrong sender secret or a stopped relay leaves
  relay_failed rows; the relay log holds no layout, token or secret.

Prerequisites (failing, never skipping, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL, default postgresql://truelive@127.0.0.1:5433/postgres)
and a Python 3.11 for the relay (TRUELIVE_RELAY_PYTHON). Every database is
built by alembic from template0 and dropped; everything binds 127.0.0.1.

    python -m pytest tests/integration/test_deploy_path_pg.py -o addopts='' -p no:cacheprovider
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid

import jwt
import psycopg2
import pytest
import socketio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, latest_deployment
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.services import deploy_service as ds
from app.services import deploy_tracking as dt
from tests.deploy_testkit import FakeSender, camera_url, patch_generator, synthetic_layout
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env

pytestmark = [pytest.mark.integration]

ACTOR = ("service_account", "sa_d14_fixture", "d14 fixture")
LAYOUT_MARKER = "d14-layout-marker-" + secrets.token_hex(4)


@pytest.fixture(scope="module")
def scratch_db():
    name = f"d14_deploy_{os.getpid()}_{secrets.token_hex(3)}"
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
    """A clean fleet (team, layout, PCs) and a session factory on the scratch DB."""
    engine = create_engine(scratch_db, pool_size=10, max_overflow=10)
    Session = sessionmaker(bind=engine, autoflush=False)
    with engine.begin() as conn:
        for table in ("audit_logs", "pc_deployments", "pcs", "screen_layouts", "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    yield engine, Session
    engine.dispose()


def _seed(Session, pc_ids, tokens=None):
    with Session() as db:
        db.add(Team(id="TEAM_d14", name="D14"))
        db.add(ScreenLayout(id="LAYOUT_d14", name="D14 wall", team_id="TEAM_d14"))
        db.flush()
        for pc_id in pc_ids:
            db.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id="TEAM_d14",
                      screen_layout_id="LAYOUT_d14", auth_token=(tokens or {}).get(pc_id)))
        db.commit()


def _deploy(Session, pc_ids, sender, trigger=ds.TRIGGER_PC_DEPLOY):
    with Session() as db:
        pcs = db.query(PC).filter(PC.id.in_(pc_ids)).order_by(PC.id).all()
        return ds.deploy_config_to_pcs(pcs, db, trigger=trigger, actor=ACTOR, sender=sender)


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(message)


# --------------------------------------------------------------------------- #
# Two-session race
# --------------------------------------------------------------------------- #


def test_two_session_race_on_scratch_postgres(pg, monkeypatch):
    engine, Session = pg
    patch_generator(monkeypatch)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    _seed(Session, ["PC_A", "PC_B"])

    # (1) Concurrent deploys to one PC: one row stays live, the other is superseded.
    for round_ in range(6):
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def run():
            try:
                barrier.wait(5)
                outcomes.append(_deploy(Session, ["PC_A"], FakeSender()))
            except Exception as exc:  # surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors, errors
        ids = {o["results"][0]["config_id"] for o in outcomes}
        with Session() as db:
            rows = {r.id: r for r in db.query(PcDeployment).filter(PcDeployment.id.in_(ids)).all()}
            assert len(rows) == 2, round_
            assert sorted(r.status for r in rows.values()) == ["delivered", "superseded"], round_
            live = next(r for r in rows.values() if r.status == "delivered")
            assert latest_deployment(db, "PC_A").id == live.id
            # Across every round, only the newest deploy of PC_A is in flight.
            in_flight = db.query(PcDeployment).filter(
                PcDeployment.pc_id == "PC_A", PcDeployment.status.in_(sorted(dt.SUPERSEDABLE_STATES))).all()
            assert [r.id for r in in_flight] == [live.id]

    # (2) A deploy waits while another transaction holds the PC row.
    locker = psycopg2.connect(engine.url.render_as_string(hide_password=False))
    try:
        with locker.cursor() as cur:
            cur.execute("SELECT id FROM pcs WHERE id = 'PC_B' FOR UPDATE")
        done = threading.Event()
        result = {}

        def blocked():
            result["summary"] = _deploy(Session, ["PC_B"], FakeSender())
            done.set()

        worker = threading.Thread(target=blocked)
        worker.start()
        assert not done.wait(1.5), "the deploy did not wait for the PC row lock"
        with Session() as db:
            assert db.query(PcDeployment).filter(PcDeployment.pc_id == "PC_B").count() == 0
        locker.commit()
        assert done.wait(15)
        worker.join(5)
        assert result["summary"]["deployed"] == 1
    finally:
        locker.close()

    # (3) API "delivered" vs relay "received" / "accepted", in separate sessions.
    with Session() as db:
        db.query(PC).filter(PC.id == "PC_B").update({PC.last_applied: None})
        db.commit()
    cases = []
    with Session() as db:
        for i in range(30):
            config_id = f"race-{i}-{uuid.uuid4().hex[:6]}"
            db.add(PcDeployment(id=config_id, pc_id="PC_B", trigger="pc_deploy", status="pending"))
            cases.append((config_id, "accepted" if i % 3 == 0 else "received"))
        db.commit()
    for config_id, relay_event in cases:
        barrier = threading.Barrier(2)
        out = {}

        def api():
            with Session() as db:
                barrier.wait(5)
                out["api"] = dt.transition(db, config_id, dt.DELIVERED)
                db.commit()

        def relay_side():
            with Session() as db:
                barrier.wait(5)
                if relay_event == "received":
                    out["relay"] = dt.record_receipt(db, config_id, "PC_B", confirmable=False)
                else:
                    out["relay"] = dt.transition(db, config_id, dt.ACCEPTED, pc_id="PC_B")
                db.commit()

        threads = [threading.Thread(target=api), threading.Thread(target=relay_side)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert out["relay"].changed, (config_id, out)  # the relay's state is reachable from pending and delivered
        with Session() as db:
            row = db.get(PcDeployment, config_id)
            assert row.status == relay_event, (config_id, row.status, out)
            assert row.delivered_at is not None  # the API's write landed or was kept as evidence
            if relay_event == "received":
                assert row.received_at is not None
    with Session() as db:
        assert db.get(PC, "PC_B").last_applied is not None  # a 1.1.x receipt stamps it


# --------------------------------------------------------------------------- #
# Real relay plus fake PCs
# --------------------------------------------------------------------------- #


def _mint(pc_id, secret):
    return jwt.encode({"pc_id": pc_id, "name": f"{pc_id} wall", "exp": int(time.time()) + 8760 * 3600,
                       "type": "pc_auth"},
                      secret, algorithm="HS256")


class FakePc:
    """A python-socketio PC that registers and ACKs ``config`` like Client
    1.1.x (its handler returns nothing)."""

    def __init__(self, url, pc_id, token, capabilities=None):
        self.pc_id = pc_id
        self.configs = []
        self.registered = None
        self.errors = []
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self.sio.on("config", self._on_config)
        self.sio.on("registered", lambda data: setattr(self, "registered", data))
        self.sio.on("error", lambda data: self.errors.append(data))
        self.sio.connect(url, wait_timeout=10)
        payload = {"pc_id": pc_id, "auth_token": token, "name": f"{pc_id} wall"}
        if capabilities is not None:
            payload["capabilities"] = capabilities
        self.sio.emit("register", payload)
        _wait(lambda: self.registered or self.errors, 10, f"{pc_id} got no registered")
        assert self.registered, self.errors

    def _on_config(self, data):
        self.configs.append(data)

    def close(self):
        try:
            self.sio.disconnect()
        except Exception:
            pass


def test_real_relay_plus_fake_pc(pg, monkeypatch):
    from tests.integration.test_relay_phase0 import Relay

    engine, Session = pg
    jwt_secret = secrets.token_hex(24)
    sender_secret = secrets.token_urlsafe(32)
    tokens = {pc: _mint(pc, jwt_secret) for pc in ("PC_A", "PC_B", "PC_C")}
    _seed(Session, list(tokens), tokens)
    layouts = {pc: synthetic_layout(pc, marker=LAYOUT_MARKER) for pc in tokens}
    patch_generator(monkeypatch, layouts)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)

    relay = Relay({"db": engine.url.render_as_string(hide_password=False), "jwt_secret": jwt_secret},
                  sender_secret=sender_secret)
    pcs = []
    try:
        pcs.append(FakePc(relay.url, "PC_A", tokens["PC_A"]))
        pcs.append(FakePc(relay.url, "PC_B", tokens["PC_B"], capabilities=["deploy_result.v1", "station_status.v1"]))
        # PC_C stays offline.
        monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
        monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)

        summary = _deploy(Session, ["PC_A", "PC_B", "PC_C"], sender=None, trigger=ds.TRIGGER_LAYOUT_DEPLOY)
        returned = time.monotonic()
        results = {r["pc_id"]: r for r in summary["results"]}
        assert (results["PC_A"]["status"], results["PC_B"]["status"], results["PC_C"]["status"]) == (
            "deployed", "deployed", "failed")
        assert results["PC_C"]["delivery"] == "offline"
        text = json.dumps(summary)
        assert "rtsp://" not in text and LAYOUT_MARKER not in text and camera_url() not in text

        # Each PC got exactly its layout, with the row's config_id at the root.
        for fake in pcs:
            _wait(lambda: fake.configs, 5, f"{fake.pc_id} got no config")
            [message] = fake.configs
            content = message["content"]
            assert content["config_id"] == results[fake.pc_id]["config_id"]
            assert {k: v for k, v in content.items() if k != "config_id"} == layouts[fake.pc_id]
        # confirmable follows what the relay advertised to that PC (D15 adds deploy_result.v1).
        advertised = "deploy_result.v1" in pcs[1].registered["features"]
        assert results["PC_A"]["confirmable"] is False
        assert results["PC_B"]["confirmable"] is advertised

        # The ACKs make the relay mark the rows received within 2 s.
        def all_received():
            with Session() as db:
                rows = [db.get(PcDeployment, results[p]["config_id"]) for p in ("PC_A", "PC_B")]
                return all(r.status == "received" for r in rows) and rows

        rows = _wait(all_received, 2.0, "rows did not reach received within 2 s")
        assert time.monotonic() - returned < 2.0
        with Session() as db:
            for row in rows:
                assert row.received_at and row.delivered_at
            offline = db.get(PcDeployment, results["PC_C"]["config_id"])
            assert offline.status == "offline"
            last = {pc.id: pc.last_applied for pc in db.query(PC).all()}
        assert last["PC_A"] is not None  # 1.1.x row: stamped on receipt
        assert (last["PC_B"] is None) is advertised  # confirmable rows wait for applied (D15)
        assert last["PC_C"] is None

        # One audit row per PC, without the layout.
        with engine.connect() as conn:
            audit = conn.exec_driver_sql(
                "SELECT resource_type, resource_id, action, changes, actor_type FROM audit_logs "
                "ORDER BY resource_id").fetchall()
        assert [(a[0], a[1], a[2], a[4]) for a in audit] == [
            ("pc", p, "config.deployed", "service_account") for p in ("PC_A", "PC_B", "PC_C")]
        for row in audit:
            changes = json.loads(row[3])
            assert changes["config_id"] == results[row[1]]["config_id"] and changes["trigger"] == "layout_deploy"
            assert "rtsp://" not in row[3] and LAYOUT_MARKER not in row[3]

        # A wrong sender secret: refused at connect, rows relay_failed, nothing delivered.
        monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", "wrong-" + secrets.token_hex(8))
        refused = _deploy(Session, ["PC_A"], sender=None)
        assert refused["results"][0]["delivery"] == "error"
        with Session() as db:
            assert db.get(PcDeployment, refused["results"][0]["config_id"]).status == "relay_failed"
        time.sleep(0.5)
        assert len(pcs[0].configs) == 1

        for fake in pcs:
            fake.close()
    finally:
        for fake in pcs:
            fake.close()
        relay.stop()

    # The relay is gone: unreachable, relay_failed.
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
    gone = _deploy(Session, ["PC_B"], sender=None)
    assert gone["results"][0]["message"] == ds.MSG_UNREACHABLE
    with Session() as db:
        row = db.get(PcDeployment, gone["results"][0]["config_id"])
        assert (row.status, row.status_detail) == ("relay_failed", "relay unreachable")

    log = relay.proc.text
    assert LAYOUT_MARKER not in log and "fixture-user" not in log
    for value in list(tokens.values()) + [jwt_secret, sender_secret]:
        assert value not in log
    assert "forwarded to PC PC_A" in log and "received by PC PC_A" in log
