"""
M2 review regressions on the real eventlet relay (Python 3.11) and Postgres.

* test_disconnect_during_a_slow_register_leaves_no_ghost: another transaction
  holds the PC row (FOR UPDATE), so register waits in its database check off
  the hub; the station drops meanwhile. Before the fix the relay still put
  the dead session into connected_pcs (/api/connected-pcs said connected and
  the presence sweep kept stamping pcs.last_seen); and a dying second session
  of a live PC demoted the live one. Now neither happens.
* test_nul_in_station_text_does_not_stop_health_or_results: a station_status
  whose last_config_error holds a NUL, and a deploy_result whose detail does,
  are stored without it; every other PC's health is still written, and no
  flush or retry fails.
* test_production_sized_layouts_reach_every_pc: 6 PCs with a layout of about
  216 KB each and one of about 1.3 MB (over python-engineio's default limit)
  through the real API sender (polling) and relay: every PC receives its
  layout and every row reaches received.
* test_database_outage_at_register: the relay's database goes through a TCP
  proxy that refuses connections for a while. OD-23 (a): the station
  registers fail-open (db_unverified), gets no fleet config, and once the
  database answers the flag is cleared (also in pc_health.relay_flags) and
  the config is served. OD-23 (b): the registration is refused while the
  database is down and completes by itself once it answers.

Stations run in the Client's Python (TRUELIVE_CLIENT_PYTHON, default the
shomer_middlewear env: python-socketio 5.10 with websocket-client) through
tests/integration/ws_station.py. Prerequisites (failing, never skipping, when
missing): the local Postgres (TRUELIVE_TEST_PG_ADMIN_URL), TRUELIVE_RELAY_PYTHON
and the Client Python. Everything binds 127.0.0.1 with ephemeral ports and
throwaway secrets; databases are built by alembic from template0 and dropped.

    python -m pytest tests/integration/test_relay_review_pg.py -o addopts='' -p no:cacheprovider
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.schemas import station_protocol as sp
from app.services import deploy_service as ds
from tests.deploy_testkit import patch_generator, synthetic_layout
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env
from tests.integration.test_relay_reports_pg import Relay, _mint

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

CLIENT_PYTHON = os.environ.get(
    "TRUELIVE_CLIENT_PYTHON", r"C:\Users\amdal\miniconda3\envs\shomer_middlewear\python.exe"
)
STATION = Path(__file__).resolve().parent / "ws_station.py"
CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_STATION_CONFIG]
ACTOR = ("service_account", "sa_review_fixture", "review fixture")


@pytest.fixture(scope="module")
def scratch_db():
    if not Path(CLIENT_PYTHON).exists():
        pytest.fail(f"Client Python not found at {CLIENT_PYTHON} (set TRUELIVE_CLIENT_PYTHON)")
    name = f"m2review_{os.getpid()}_{secrets.token_hex(3)}"
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
    engine = create_engine(scratch_db, pool_size=5, max_overflow=5)
    with engine.begin() as conn:
        for table in ("audit_logs", "pc_token_renewals", "pc_health", "pc_deployments", "pcs", "screen_layouts",
                      "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    yield scratch_db, engine, sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _seed(Session, tokens):
    with Session() as db:
        db.add(Team(id="TEAM_rev", name="Review"))
        db.add(ScreenLayout(id="LAYOUT_rev", name="Review wall", team_id="TEAM_rev"))
        db.flush()
        for pc_id, token in tokens.items():
            db.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id="TEAM_rev",
                      screen_layout_id="LAYOUT_rev", auth_token=token))
        db.commit()


def _wait(predicate, timeout, message, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(message)


def _connected(relay):
    with urllib.request.urlopen(f"{relay.url}/api/connected-pcs", timeout=5) as response:
        return {p["pc_id"] for p in json.loads(response.read())["connected_pcs"]}


def _log(relay):
    return "".join(relay.proc.lines)


class WsStation:
    """One websocket station process (tests/integration/ws_station.py)."""

    def __init__(self, url, tmp_path):
        env = {k: v for k, v in os.environ.items()
               if k not in ("JWT_SECRET", "SECRET_KEY", "RELAY_SENDER_SECRET") and not k.startswith("PYTHON")}
        env["NO_PROXY"] = "127.0.0.1,localhost"
        self.events = []
        self.proc = subprocess.Popen([CLIENT_PYTHON, "-I", str(STATION)], cwd=str(tmp_path), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
        self._send({"url": url})
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.proc.stderr.read(), daemon=True).start()
        self.sid = self.wait("connected")["sid"]

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass

    def _send(self, item):
        self.proc.stdin.write(json.dumps(item) + "\n")
        self.proc.stdin.flush()

    def wait(self, name, timeout=15, n=1):
        def found():
            hits = [e for e in self.events if e["event"] == name]
            return hits[n - 1] if len(hits) >= n else None

        return _wait(found, timeout, f"station never reported {name!r} x{n}; saw {[e['event'] for e in self.events]}")

    def count(self, name):
        return sum(1 for e in self.events if e["event"] == name)

    def register(self, pc_id, token, **extra):
        self._send({"cmd": "emit", "event": "register",
                    "data": {"pc_id": pc_id, "auth_token": token, "name": f"{pc_id} wall", **extra}})

    def call(self, event, data, timeout=5):
        tag = uuid.uuid4().hex
        self._send({"cmd": "call", "event": event, "data": data, "tag": tag, "timeout": timeout})
        return _wait(lambda: next((e for e in self.events if e.get("tag") == tag), None), timeout + 5,
                     f"no answer to {event}")

    def drop(self):
        """End the process at once: the socket closes, no Socket.IO disconnect."""
        try:
            self._send({"cmd": "exit"})
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def close(self):
        if self.proc.poll() is None:
            self.drop()


class PgProxy:
    """A TCP proxy to the local Postgres that can refuse connections (and cut
    the ones it carries), so the relay's database can be "down"."""

    def __init__(self, target_port):
        self.target = ("127.0.0.1", target_port)
        self.up = True
        self._lock = threading.Lock()
        self._socks = set()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _addr = self.server.accept()
            except OSError:
                return
            if not self.up:
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=5)
                upstream.settimeout(None)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._socks.update((client, upstream))
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            self._close(a, b)

    def _close(self, *socks):
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                self._socks.discard(sock)

    def set_up(self, up):
        self.up = up
        if not up:
            with self._lock:
                socks = list(self._socks)
            self._close(*socks)

    def close(self):
        self.set_up(False)
        self.server.close()


def _lock_pc(db_url, pc_id):
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute("SELECT id FROM pcs WHERE id = %s FOR UPDATE", (pc_id,))
    return conn


def _lock_waiters(engine):
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ).scalar()


def _last_seen(Session, pc_id):
    with Session() as db:
        return db.get(PC, pc_id).last_seen


# --------------------------------------------------------------------------- #
# Register races
# --------------------------------------------------------------------------- #


def test_disconnect_during_a_slow_register_leaves_no_ghost(pg, tmp_path, monkeypatch):
    db_url, engine, Session = pg
    jwt_secret = secrets.token_hex(24)
    sender_secret = secrets.token_urlsafe(32)
    tokens = {pc: _mint(pc, jwt_secret) for pc in ("PC_G", "PC_L")}
    _seed(Session, tokens)
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
    relay = Relay(db_url, jwt_secret, sender_secret)
    stations = []
    try:
        # A) The station drops while its register waits on the PC row.
        lock = _lock_pc(db_url, "PC_G")
        ghost = WsStation(relay.url, tmp_path)
        stations.append(ghost)
        ghost.register("PC_G", tokens["PC_G"])
        _wait(lambda: _lock_waiters(engine) >= 1, 10, "register never waited on the PC row")
        ghost.drop()
        _wait(lambda: f"Client disconnected: {ghost.sid}" in _log(relay), 10, "relay never saw the drop")
        lock.rollback()
        lock.close()
        _wait(lambda: "closed while its registration was checked" in _log(relay), 10, "register did not finish")
        assert "PC_G" not in _connected(relay)
        stamped = _last_seen(Session, "PC_G")  # the check's own stamp, once
        time.sleep(2.5)  # the presence sweep runs every 1 s
        assert _last_seen(Session, "PC_G") == stamped
        assert "PC registered: PC_G" not in _log(relay)

        # B) A second session of a live PC drops during its register: the live one keeps it.
        live = WsStation(relay.url, tmp_path)
        stations.append(live)
        live.register("PC_L", tokens["PC_L"], capabilities=CAPS)
        live.wait("registered")
        lock = _lock_pc(db_url, "PC_L")
        dying = WsStation(relay.url, tmp_path)
        stations.append(dying)
        dying.register("PC_L", tokens["PC_L"], capabilities=CAPS)
        _wait(lambda: _lock_waiters(engine) >= 1, 10, "second register never waited on the PC row")
        dying.drop()
        _wait(lambda: f"Client disconnected: {dying.sid}" in _log(relay), 10, "relay never saw the drop")
        lock.rollback()
        lock.close()
        _wait(lambda: _log(relay).count("closed while its registration was checked") >= 2, 10,
              "second register did not finish")
        assert _connected(relay) == {"PC_L"}
        ack = live.call("station_status", {"message_id": uuid.uuid4().hex, "ts": time.time(), "health": "ok"})
        assert ack["data"]["status"] == "ok"
        answers = ds.socketio_sender([{"config_id": "cfg-live", "targetId": "PC_L", "content": {"screens": []}}],
                                     url=relay.url)
        assert answers["cfg-live"]["status"] == "delivered"
        assert live.wait("config")["config_id"] is None  # the content had no root id
        assert "Duplicate registration for PC PC_L" not in _log(relay)
    finally:
        for station in stations:
            station.close()
        relay.stop()
    for value in list(tokens.values()) + [jwt_secret]:
        assert value not in _log(relay)


# --------------------------------------------------------------------------- #
# Station text the database cannot store
# --------------------------------------------------------------------------- #


def test_nul_in_station_text_does_not_stop_health_or_results(pg, tmp_path):
    db_url, engine, Session = pg
    jwt_secret = secrets.token_hex(24)
    pcs = ("PC_BAD", "PC_OK1", "PC_OK2")
    tokens = {pc: _mint(pc, jwt_secret) for pc in pcs}
    _seed(Session, tokens)
    with Session() as db:
        db.add(PcDeployment(id="cfg-bad", pc_id="PC_BAD", trigger="pc_deploy", status="accepted", confirmable=True,
                            status_changed_at=int(time.time())))
        db.add(PcDeployment(id="cfg-ok", pc_id="PC_OK1", trigger="pc_deploy", status="accepted", confirmable=True,
                            status_changed_at=int(time.time())))
        db.commit()
    relay = Relay(db_url, jwt_secret, secrets.token_urlsafe(32))
    stations = {}
    try:
        for pc in pcs:
            stations[pc] = WsStation(relay.url, tmp_path)
            stations[pc].register(pc, tokens[pc], capabilities=CAPS)
            stations[pc].wait("registered")
        bad = stations["PC_BAD"].call("station_status", {
            "message_id": uuid.uuid4().hex, "ts": time.time(), "health": "degraded",
            "config_status": {"last_config_error": "parse error at byte \u0000 here"},
            "readiness": [{"code": "AUTOLOGON_OFF", "detail": "x\u0000y"}]})
        assert bad["data"]["status"] == "ok"
        for pc in ("PC_OK1", "PC_OK2"):
            ack = stations[pc].call("station_status", {"message_id": uuid.uuid4().hex, "ts": time.time(),
                                                       "health": "ok"})
            assert ack["data"]["status"] == "ok"
        result = stations["PC_BAD"].call("deploy_result", {
            "message_id": uuid.uuid4().hex, "config_id": "cfg-bad", "status": "failed",
            "detail": "Core said \u0000 no", "ts": time.time()})
        assert result["data"]["status"] == "ok"
        ok = stations["PC_OK1"].call("deploy_result", {"message_id": uuid.uuid4().hex, "config_id": "cfg-ok",
                                                       "status": "applied", "ts": time.time()})
        assert ok["data"]["status"] == "ok"

        def health_rows():
            with Session() as db:
                rows = {r.pc_id: r for r in db.query(PcHealth).all() if r.health}
                return rows if len(rows) == 3 else None

        rows = _wait(health_rows, 10, "not every PC's health was written")
        assert rows["PC_BAD"].last_config_error == "parse error at byte  here"
        assert rows["PC_BAD"].readiness == [{"code": "AUTOLOGON_OFF", "detail": "xy"}]
        with Session() as db:
            failed = db.get(PcDeployment, "cfg-bad")
            assert (failed.status, failed.status_detail) == ("failed", "Core said  no")
            assert db.get(PcDeployment, "cfg-ok").status == "applied"
        log = _log(relay)
        assert "Health flush failed" not in log and "Deploy event retry aborted" not in log
        assert "refused the row" not in log
    finally:
        for station in stations.values():
            station.close()
        relay.stop()


# --------------------------------------------------------------------------- #
# Production-sized layouts
# --------------------------------------------------------------------------- #


def _big_layout(pc_id, size):
    """A synthetic layout of about ``size`` encoded bytes (many view-like entries)."""
    layout = synthetic_layout(pc_id)

    def item(i):
        return {"id": f"{pc_id}-view-{i:05d}", "title": "Camera " + "w" * 180, "source_id": f"{i:032x}",
                "site_name": "Site " + "s" * 60}

    per_item = len(json.dumps(item(0))) + 2
    count = max(0, (size - len(json.dumps(layout))) // per_item)
    layout["review_filler"] = [item(i) for i in range(count)]
    return layout


def test_production_sized_layouts_reach_every_pc(pg, tmp_path, monkeypatch):
    db_url, engine, Session = pg
    jwt_secret = secrets.token_hex(24)
    sender_secret = secrets.token_urlsafe(32)
    pcs = [f"PC_{i}" for i in range(7)]
    tokens = {pc: _mint(pc, jwt_secret) for pc in pcs}
    _seed(Session, tokens)
    layouts = {pc: _big_layout(pc, 216_000) for pc in pcs[:6]}
    layouts[pcs[6]] = _big_layout(pcs[6], 1_300_000)
    patch_generator(monkeypatch, layouts)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    relay = Relay(db_url, jwt_secret, sender_secret)
    stations = {}
    try:
        for pc in pcs:
            stations[pc] = WsStation(relay.url, tmp_path)
            stations[pc].register(pc, tokens[pc])
            stations[pc].wait("registered")
        monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
        monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
        with Session() as db:
            targets = db.query(PC).filter(PC.id.in_(pcs)).order_by(PC.id).all()
            summary = ds.deploy_config_to_pcs(targets, db, trigger=ds.TRIGGER_LAYOUT_DEPLOY, actor=ACTOR)
        results = {r["pc_id"]: r for r in summary["results"]}
        assert {pc: (r["status"], r["delivery"]) for pc, r in results.items()} == {
            pc: ("deployed", "delivered") for pc in pcs}
        for pc in pcs:
            config = stations[pc].wait("config", timeout=20)
            assert config["config_id"] == results[pc]["config_id"]
            assert config["size"] > (1_000_000 if pc == pcs[6] else 200_000)

        def all_received():
            with Session() as db:
                states = {pc: db.get(PcDeployment, results[pc]["config_id"]).status for pc in pcs}
            return states if set(states.values()) == {"received"} else None

        _wait(all_received, 10, "not every row reached received")
        assert "Rejected" not in _log(relay)
    finally:
        for station in stations.values():
            station.close()
        relay.stop()


# --------------------------------------------------------------------------- #
# Database outage at register (D12, OD-23)
# --------------------------------------------------------------------------- #


def _proxied(db_url, port):
    head, name = db_url.rsplit("/", 1)
    return f"postgresql://truelive@127.0.0.1:{port}/{name}"


def test_database_outage_at_register(pg, tmp_path):
    db_url, engine, Session = pg
    jwt_secret = secrets.token_hex(24)
    tokens = {pc: _mint(pc, jwt_secret) for pc in ("PC_U", "PC_V")}
    _seed(Session, tokens)
    proxy = PgProxy(int(db_url.rsplit(":", 1)[1].split("/")[0]))
    relays, stations = [], []
    try:
        # OD-23 (a): fail-open, then checked again.
        relay = Relay(_proxied(db_url, proxy.port), jwt_secret, secrets.token_urlsafe(32), RELAY_REVERIFY_SECONDS="1")
        relays.append(relay)
        proxy.set_up(False)
        station = WsStation(relay.url, tmp_path)
        stations.append(station)
        station.register("PC_U", tokens["PC_U"], capabilities=CAPS)
        station.wait("registered")
        assert "registered without a database check (db_unverified)" in _log(relay)
        refused = station.call("get_station_config", {})
        assert (refused["data"]["status"], refused["data"]["reason"]) == ("rejected", "not verified")
        proxy.set_up(True)
        _wait(lambda: "PC PC_U checked against the database after registering without a check" in _log(relay), 15,
              "the unverified registration was never checked again")

        def flags_clear():
            with Session() as db:
                row = db.get(PcHealth, "PC_U")
                return row is not None and row.relay_flags == {}

        _wait(flags_clear, 10, "pc_health.relay_flags still says db_unverified")
        served = station.call("get_station_config", {})
        assert served["data"]["status"] == "ok"
        assert _connected(relay) == {"PC_U"}
        station.close()
        relay.stop()

        # OD-23 (b): fail closed, then the registration completes by itself.
        relay = Relay(_proxied(db_url, proxy.port), jwt_secret, secrets.token_urlsafe(32),
                      RELAY_REVERIFY_SECONDS="1", RELAY_REQUIRE_CURRENT_TOKEN="true")
        relays.append(relay)
        proxy.set_up(False)
        station = WsStation(relay.url, tmp_path)
        stations.append(station)
        station.register("PC_V", tokens["PC_V"], capabilities=CAPS)
        assert station.wait("error")["message"] == "The portal cannot verify this PC right now; it retries automatically"
        time.sleep(2.5)  # two quiet retries while the database is still down
        assert station.count("registered") == 0 and station.count("error") == 1
        assert "PC_V" not in _connected(relay)
        proxy.set_up(True)
        station.wait("registered", timeout=15)
        assert _connected(relay) == {"PC_V"}
        assert station.call("get_station_config", {})["data"]["status"] == "ok"
        assert "Relay registration: db_error=fail_closed reverify=1s" in _log(relay)
    finally:
        for station in stations:
            station.close()
        for relay in relays:
            relay.stop()
        proxy.close()
    for relay in relays:
        for value in list(tokens.values()) + [jwt_secret]:
            assert value not in _log(relay)


# --------------------------------------------------------------------------- #
# The database refusing one row, on Postgres itself
# --------------------------------------------------------------------------- #


def test_postgres_refusing_one_row_does_not_block_the_others(pg):
    """The relay cleans station text before buffering it; this writes a raw
    NUL anyway (as a future field might) to check the isolation itself on
    Postgres: psycopg2 refuses the value (ValueError), the other PCs' health
    rows are written, and one refused deploy event does not undo the others
    (real savepoints)."""
    from app.services import deploy_tracking as dt
    from app.services import station_reports

    db_url, engine, Session = pg
    _seed(Session, {pc: None for pc in ("PC_A", "PC_B", "PC_C")})
    batch = {
        "PC_A": {"health": "ok", "received_at": 1},
        "PC_B": {"health": "degraded", "received_at": 1, "last_config_error": "raw \x00 text"},
        "PC_C": {"health": "ok", "received_at": 1},
    }
    outcome = station_reports.write_health_batch(Session, batch)
    assert (outcome.written, outcome.refused, outcome.restore, outcome.error) == (
        2, [("PC_B", "ValueError")], {}, "ValueError")
    with Session() as db:
        assert sorted(r.pc_id for r in db.query(PcHealth).all()) == ["PC_A", "PC_C"]

    now = int(time.time())
    with Session() as db:
        for config_id, pc_id in (("ev-ok", "PC_A"), ("ev-bad", "PC_B"), ("ev-ok2", "PC_C")):
            db.add(PcDeployment(id=config_id, pc_id=pc_id, trigger="pc_deploy", status="accepted", confirmable=True,
                                status_changed_at=now))
        db.commit()
    pending = dt.PendingTransitions(ttl_seconds=60)
    pending.add("ev-ok", lambda db: dt.transition(db, "ev-ok", dt.APPLIED, pc_id="PC_A"), label="applied")
    pending.add("ev-bad", lambda db: dt.transition(db, "ev-bad", dt.FAILED, pc_id="PC_B", detail="Core \x00"),
                label="failed")
    pending.add("ev-ok2", lambda db: dt.transition(db, "ev-ok2", dt.FAILED, pc_id="PC_C", detail="clean"),
                label="failed")
    assert pending.retry(Session) == (2, 0)
    assert pending.refused == 1
    with Session() as db:
        assert {r.id: r.status for r in db.query(PcDeployment).all()} == {
            "ev-ok": "applied", "ev-bad": "accepted", "ev-ok2": "failed"}
