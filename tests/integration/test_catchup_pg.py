"""
Catch-up re-push with real processes (plan 06 D17; OD-20). Card tests
"reconnect catch-up" and "broker down".

Processes, all on 127.0.0.1 with throwaway secrets: the real eventlet relay
(Python 3.11), a real Celery worker (``-P solo``), and this test as the
portal API and the stations (python-socketio). The database is a scratch
Postgres built by alembic. There is no Redis on this machine, so:

* the rate-limit key lives in tests/fake_redis.py, a small RESP server the
  relay reaches with the real redis-py client over TCP;
* the Celery broker is kombu's SQLAlchemy transport on the scratch Postgres
  (``sqla+postgresql://``), a real cross-process queue. Production uses Redis
  for both; the relay code is the same (``celery_app.send_task``).

* test_reconnect_catch_up: deploys made while two stations were offline end
  ``offline``. With DEPLOY_CATCHUP_ENABLED, a 1.1.x-like station gets the
  layout again after it registers, and a confirmable station after its first
  station_status with running_config_id, each within 30 s, with a new
  config_id and trigger ``catch_up``. A station whose key is still set is not
  re-pushed; after a relay restart nobody is re-pushed again (no loops); with
  the flag off nothing happens and Redis is never touched.
* test_broker_down_registered_within_1s: with Redis and the broker down
  (dead ports), and then with only the broker down, 50 stations whose last
  deploy was missed all get ``registered`` within 1 s; the relay pauses
  catch-up and keeps serving.

    python -m pytest tests/integration/test_catchup_pg.py -o addopts='' -p no:cacheprovider
"""

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import threading
import time

import jwt
import pytest
import socketio
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.schemas import station_protocol as sp
from app.services import catchup
from app.services import deploy_service as ds
from tests.fake_redis import FakeRedis
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, Proc, _admin_connection, _base_env
from tests.integration.test_relay_reports_pg import Pc, Relay

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

RELAY_PYTHON = os.environ.get("TRUELIVE_RELAY_PYTHON", sys.executable)
CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
CATCHUP_LIMIT_S = 30


@pytest.fixture(scope="module")
def scratch_db():
    name = f"d17_catchup_{os.getpid()}_{secrets.token_hex(3)}"
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
        for table in ("audit_logs", "pc_token_renewals", "pc_health", "pc_deployments", "pcs", "screen_layouts",
                      "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
        for table in ("kombu_message", "kombu_queue"):
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table} CASCADE")
    yield scratch_db, engine, sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _mint(pc_id, secret):
    return jwt.encode({"pc_id": pc_id, "name": f"{pc_id} wall", "exp": int(time.time()) + 8760 * 3600,
                       "type": "pc_auth"}, secret, algorithm="HS256")


def _seed(Session, tokens):
    with Session() as db:
        db.add(Team(id="TEAM_d17", name="D17"))
        db.add(ScreenLayout(id="LAYOUT_d17", name="D17 wall", team_id="TEAM_d17"))
        db.flush()
        db.bulk_insert_mappings(PC, [
            {"id": pc_id, "name": f"{pc_id} wall", "role": "controller", "team_id": "TEAM_d17",
             "screen_layout_id": "LAYOUT_d17", "auth_token": token} for pc_id, token in tokens.items()])
        db.commit()


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _deploy_while_offline(Session, monkeypatch, relay, sender_secret, pc_ids):
    """The portal deploys to PCs that are not connected: rows end offline."""
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    with Session() as db:
        pcs = db.query(PC).filter(PC.id.in_(pc_ids)).order_by(PC.id).all()
        summary = ds.deploy_config_to_pcs(pcs, db, trigger=ds.TRIGGER_LAYOUT_DEPLOY)
    results = {r["pc_id"]: r for r in summary["results"]}
    assert all(r["delivery"] == "offline" for r in results.values()), results
    return {pc: r["config_id"] for pc, r in results.items()}


class Worker:
    """A real Celery worker (solo pool) on the SQLAlchemy broker."""

    def __init__(self, env):
        name = f"d17-{secrets.token_hex(3)}@127.0.0.1"
        self.proc = Proc([RELAY_PYTHON, "-m", "celery", "-A", "app.tasks.celery_app", "worker", "-P", "solo", "-l",
                          "INFO", "--without-heartbeat", "--without-gossip", "--without-mingle", "-Q", "celery",
                          "-n", name], env)
        self.proc.wait_for("ready.", timeout=120)

    def stop(self):
        self.proc.stop()


def test_reconnect_catch_up(pg, monkeypatch):
    db_url, engine, Session = pg
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    ids = ["PC_OLD", "PC_NEW", "PC_RL", "PC_OFF"]
    tokens = {pc: _mint(pc, jwt_secret) for pc in ids}
    _seed(Session, tokens)
    redis_server = FakeRedis()
    broker = "sqla+" + db_url
    shared = dict(DEPLOY_CATCHUP_ENABLED="true", REDIS_URL=redis_server.url, CELERY_BROKER_URL=broker,
                  CELERY_RESULT_BACKEND="db+" + db_url, RELAY_CATCHUP_LOOP_SECONDS="0.2")
    relay = worker = None
    stations = []
    logs = []
    try:
        relay = Relay(db_url, jwt_secret, sender_secret, **shared)
        worker_env = _base_env(DATABASE_URL=db_url, JWT_SECRET=jwt_secret, RELAY_SENDER_SECRET=sender_secret,
                               WEBSOCKET_HOST="127.0.0.1", WEBSOCKET_PORT=str(relay.port), **shared)
        worker_env.pop("TRUELIVE_RELAY_ASYNC_MODE", None)
        worker = Worker(worker_env)
        missed = _deploy_while_offline(Session, monkeypatch, relay, sender_secret, ids)

        # PC_RL had a catch-up a minute ago: its key is still set.
        import redis as redis_py

        redis_py.Redis.from_url(redis_server.url).set(catchup.rate_key("PC_RL"), "1", ex=540)

        # A 1.1.x-like station: catch-up at register.
        started = time.monotonic()
        old = Pc(relay.url, "PC_OLD", tokens["PC_OLD"])
        stations.append(old)
        _wait(lambda: old.configs, CATCHUP_LIMIT_S, "PC_OLD never got the missed layout")
        old_after = time.monotonic() - started
        old_started = started  # PC_OLD's key was set after this moment
        content = old.configs[0]["content"]
        assert content["config_id"] != missed["PC_OLD"]

        # A confirmable station: nothing at register, catch-up on its first status with running_config_id.
        new = Pc(relay.url, "PC_NEW", tokens["PC_NEW"], capabilities=CAPS, versions={"client_version": "1.4.0"})
        stations.append(new)
        time.sleep(1.5)
        assert new.configs == []  # not at register
        assert new.call("station_status", {"message_id": "st-0", "ts": time.time(), "health": "ok"})["status"] == "ok"
        time.sleep(1.0)
        assert new.configs == []  # a status without running_config_id does not decide
        started = time.monotonic()
        assert new.call("station_status", {"message_id": "st-1", "ts": time.time(), "health": "ok",
                                           "wall": {"running_config_id": ""}})["status"] == "ok"
        _wait(lambda: new.configs, CATCHUP_LIMIT_S, "PC_NEW never got the missed layout")
        new_after = time.monotonic() - started
        new_id = new.configs[0]["content"]["config_id"]

        # The rate-limited station gets nothing.
        rl = Pc(relay.url, "PC_RL", tokens["PC_RL"])
        stations.append(rl)

        def rows(pc_id):
            with Session() as db:
                return db.query(PcDeployment).filter(PcDeployment.pc_id == pc_id).order_by(
                    PcDeployment.created_at).all()

        def received(pc_id):
            latest = rows(pc_id)[-1]
            return latest.trigger == "catch_up" and latest.status == "received"

        _wait(lambda: received("PC_OLD"), 10, "the catch-up row of PC_OLD never reached received")
        old_rows, new_rows = rows("PC_OLD"), rows("PC_NEW")
        assert [(r.trigger, r.status) for r in old_rows] == [("layout_deploy", "offline"), ("catch_up", "received")]
        assert old_rows[-1].id == content["config_id"] and old_rows[-1].config_hash == old_rows[0].config_hash
        assert old_rows[-1].confirmable is False and old_rows[-1].created_by_type == "system"
        assert [r.trigger for r in new_rows] == ["layout_deploy", "catch_up"] and new_rows[-1].id == new_id
        assert new_rows[-1].confirmable is True  # deploy_result.v1 + the root config_id
        time.sleep(2.0)
        assert rl.configs == [] and [r.trigger for r in rows("PC_RL")] == ["layout_deploy"]
        assert [r.trigger for r in rows("PC_OFF")] == ["layout_deploy"]  # never came back
        # SET NX EX 600: the TTL left is 600 s minus the time since the key was
        # set, measured from a moment before it could have been set (the fake
        # Redis runs in this process on the same monotonic clock), so the check
        # does not depend on how long the steps above took (it failed under
        # full-suite load with a fixed 590 s floor).
        for pc, set_after in (("PC_OLD", old_started), ("PC_NEW", started)):
            ttl = redis_server.ttl(catchup.rate_key(pc))
            assert ttl is not None and set_after + 600 - time.monotonic() - 0.5 <= ttl <= 600, (pc, ttl)

        # No loops: everyone reconnects after a relay restart; nobody is re-pushed.
        for station in stations:
            station.close()
        stations = []
        relay.stop()
        logs.append(relay.proc.text)
        relay = Relay(db_url, jwt_secret, sender_secret, **shared)
        worker.stop()
        logs.append(worker.proc.text)
        worker_env["WEBSOCKET_PORT"] = str(relay.port)
        worker = Worker(worker_env)
        old = Pc(relay.url, "PC_OLD", tokens["PC_OLD"])
        new = Pc(relay.url, "PC_NEW", tokens["PC_NEW"], capabilities=CAPS, versions={"client_version": "1.4.0"})
        stations += [old, new]
        new.call("station_status", {"message_id": "st-2", "ts": time.time(), "health": "ok",
                                    "wall": {"running_config_id": new_id}})
        time.sleep(3.0)
        assert old.configs == [] and new.configs == []
        assert [r.trigger for r in rows("PC_OLD")] == ["layout_deploy", "catch_up"]
        assert [r.trigger for r in rows("PC_NEW")] == ["layout_deploy", "catch_up"]
    except BaseException:
        for proc in (relay, worker):
            if proc is not None:
                print("---- tail of", type(proc).__name__, "output ----")
                print("".join(line for line in proc.proc.lines[-60:] if "socket.io" not in line))
        raise
    finally:
        for station in stations:
            station.close()
        if relay is not None:
            relay.stop()
            logs.append(relay.proc.text)
        if worker is not None:
            worker.stop()
            logs.append(worker.proc.text)

    relay_logs = "".join(logs)
    assert "Relay catch-up: enabled=true rate_limit=600s retry=false" in relay_logs
    assert relay_logs.count("Catch-up queued for PC PC_OLD") == 1
    assert relay_logs.count("Catch-up queued for PC PC_NEW") == 1
    assert "No catch-up for PC PC_RL: rate limited" in relay_logs
    assert "No catch-up for PC PC_OLD: latest deploy is received" in relay_logs
    assert "catch_up_deploy" in relay_logs and "succeeded" in relay_logs
    for value in list(tokens.values()) + [jwt_secret, sender_secret]:
        assert value not in relay_logs

    # The flag off: nothing is evaluated and Redis is never touched.
    with Session() as db:
        db.add(PcDeployment(id="off-missed", pc_id="PC_OFF", layout_id="LAYOUT_d17", trigger="pc_deploy",
                            status="offline", config_hash=rows("PC_OFF")[0].config_hash))
        db.commit()
    redis_server.commands.clear()
    relay = Relay(db_url, jwt_secret, sender_secret, **dict(shared, DEPLOY_CATCHUP_ENABLED="false"))
    try:
        off = Pc(relay.url, "PC_OFF", tokens["PC_OFF"])
        time.sleep(3.0)
        assert off.configs == [] and not redis_server.touched()
        off.close()
    finally:
        relay.stop()
        redis_server.stop()
    assert "Relay catch-up: enabled=false" in relay.proc.text and "Catch-up" not in relay.proc.text
    print(f"catch-up: 1.1.x station re-pushed {old_after:.1f}s after connecting, confirmable station "
          f"{new_after:.1f}s after its first status")


def test_broker_down_registered_within_1s(pg, monkeypatch):
    db_url, engine, Session = pg
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    ids = [f"PC_D{i:02d}" for i in range(50)]
    tokens = {pc: _mint(pc, jwt_secret) for pc in ids}
    _seed(Session, tokens)
    redis_server = FakeRedis()
    variants = (
        ("redis and broker down", f"redis://127.0.0.1:{_dead_port()}/0"),
        ("broker down", redis_server.url),
    )
    latencies = {}
    try:
        for label, redis_url in variants:
            relay = Relay(db_url, jwt_secret, sender_secret, DEPLOY_CATCHUP_ENABLED="true", REDIS_URL=redis_url,
                          CELERY_BROKER_URL=f"redis://127.0.0.1:{_dead_port()}/0",
                          CELERY_RESULT_BACKEND=f"redis://127.0.0.1:{_dead_port()}/1", RELAY_CATCHUP_LOOP_SECONDS="0.2")
            try:
                if label == "redis and broker down":
                    _deploy_while_offline(Session, monkeypatch, relay, sender_secret, ids)
                times = asyncio.run(_storm(relay.url, tokens))
                assert len(times) == len(ids), f"{label}: {len(times)} registered"
                latencies[label] = max(times.values())
                assert latencies[label] < 1.0, f"{label}: slowest registered after {latencies[label]:.2f}s"
                # The relay keeps serving while catch-up is paused.
                late = Pc(relay.url, ids[0], tokens[ids[0]])
                late.close()
                _wait(lambda: "catch-up paused" in relay.proc.text, 15, f"{label}: catch-up never paused")
            except BaseException:
                print("---- tail of the relay output ----")
                print("".join(line for line in relay.proc.lines[-60:] if "socket.io" not in line))
                raise
            finally:
                relay.stop()
            log = relay.proc.text
            assert "Catch-up queued" not in log
            assert "Error during registration" not in log
    finally:
        redis_server.stop()
    with Session() as db:
        assert db.query(PcDeployment).filter(PcDeployment.trigger == "catch_up").count() == 0
    assert any(name == "SET" for name, _ in redis_server.commands)  # the key was tried, then the broker failed
    print("broker down: slowest registered " + ", ".join(f"{k} {v * 1000:.0f} ms" for k, v in latencies.items()))


async def _storm(url, tokens):
    """Every station connects, registers at once; seconds from its register
    emit to ``registered``."""
    times = {}
    clients = []

    async def one(pc_id):
        client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        done = asyncio.get_running_loop().create_future()

        @client.on("registered")
        async def _registered(data):
            if not done.done():
                done.set_result(time.monotonic())

        clients.append(client)
        await client.connect(url, transports=["polling"], wait_timeout=15)
        sent = time.monotonic()
        await client.emit("register", {"pc_id": pc_id, "auth_token": tokens[pc_id]})
        times[pc_id] = (await asyncio.wait_for(done, 15)) - sent

    try:
        await asyncio.gather(*(one(pc_id) for pc_id in tokens))
    finally:
        await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)
    return times
