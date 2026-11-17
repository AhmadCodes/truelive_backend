"""
Fleet Core settings over the real relay, on Postgres (plan 06 C10).

* test_relay_call_roundtrip: an alembic-built scratch database is seeded the
  way the API seeds at startup (twice: idempotent). The settings API (in
  process, super-admin override, SETTINGS_ENCRYPTION_KEY K) writes fleet
  values; the real eventlet relay (Python 3.11, the same K) serves them to a
  registered python-socketio station through ``sio.call("get_station_config")``:
  typed values, '' rather than None, a revision that is the sha256 of the
  values. 200 calls within 1 s from 10 stations cost one database read (the
  relay logs one "Station config loaded" line per read). Unregistered and
  stale sessions are refused. A portal edit reaches the station after the
  cache TTL with a new revision; re-saving the same value keeps the revision.
  Neither the relay log nor any ACK carries ciphertext, and the log carries
  no set value or secret.
* test_relay_without_a_valid_key: a relay without SETTINGS_ENCRYPTION_KEY, and
  one with an invalid key (which must still start), serve the plain keys and
  list every encrypted key in ``errors``.

Prerequisites (failing, never skipping, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL) and a Python 3.11 for the relay
(TRUELIVE_RELAY_PYTHON). Databases are built by alembic from template0 and
dropped (never truelive_portal or the snapshot); everything binds 127.0.0.1;
throwaway secrets only.

    python -m pytest tests/integration/test_station_config_pg.py -o addopts='' -p no:cacheprovider
"""

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.pc import PC
from app.models.system_setting import SystemSetting
from app.models.team import Team
from app.models.user import User
from app.schemas import station_protocol as sp
from app.services import core_station_config as csc
from app.services import settings_seeder
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env
from tests.integration.test_relay_reports_pg import Pc, Relay, _mint, _wait

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

SECRET_QUERY = "c10-secret-" + secrets.token_hex(6)
ADDRESS = f"wss://203.0.113.50/pulse/{{pc_id}}?name={{pc_name_urlenc}}&key={SECRET_QUERY}"
EDITED = f"wss://203.0.113.51/ack/{{pc_id}}?key={SECRET_QUERY}"
CACHE_SECONDS = 5
LOADED = "Station config loaded: revision="


@pytest.fixture(scope="module")
def scratch_db():
    name = f"c10_station_config_{os.getpid()}_{secrets.token_hex(3)}"
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
    Session = sessionmaker(bind=engine, autoflush=False)
    with engine.begin() as conn:
        for table in ("pcs", "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
        conn.exec_driver_sql("DELETE FROM system_settings WHERE category = 'core'")
    yield scratch_db, engine, Session
    engine.dispose()


def _service(monkeypatch, key):
    from app.services import system_settings_service as sss

    if key is None:
        monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    monkeypatch.setattr(sss.app_settings, "SETTINGS_ENCRYPTION_KEY", None)
    return sss.SystemSettingsService()


def _seed_fleet(Session, jwt_secret, pc_ids):
    tokens = {pc_id: _mint(pc_id, jwt_secret) for pc_id in pc_ids}
    with Session() as db:
        db.add(Team(id="TEAM_c10", name="C10"))
        db.flush()
        db.bulk_insert_mappings(PC, [{"id": pc_id, "name": f"{pc_id} wall", "role": "controller",
                                      "team_id": "TEAM_c10", "auth_token": token} for pc_id, token in tokens.items()])
        db.commit()
    return tokens


class PortalApi:
    """The settings API in process against the scratch database, as a super admin."""

    def __init__(self, Session, service, monkeypatch):
        from fastapi.testclient import TestClient

        from app.api.deps import get_super_admin_user
        from app.api.v1 import settings as settings_api
        from app.database import get_db
        from app.main import app

        with Session() as db:
            admin = User(user_id=uuid.uuid4(), username=f"c10-admin-{secrets.token_hex(3)}",
                         full_name="C10 Fixture Admin", email=f"c10-{secrets.token_hex(3)}@example.com",
                         password_hash="!unusable", role="super_admin", is_active=True)
            db.add(admin)
            db.commit()
            db.refresh(admin)
            db.expunge(admin)

        def _db():
            session = Session()
            try:
                yield session
            finally:
                session.close()

        monkeypatch.setattr(settings_api, "system_settings_service", service)
        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_super_admin_user] = lambda: admin
        self.app = app
        self.client = TestClient(app)

    def put(self, key, value):
        response = self.client.put(f"/api/v1/settings/{key}", json={"value": value})
        assert response.status_code == 200, response.text
        return response.json()

    def close(self):
        self.app.dependency_overrides.clear()


def _loads(relay):
    return sum(1 for line in relay.proc.lines if LOADED in line)


def _call(station, data=None):
    return station.call("get_station_config", data if data is not None else {}, timeout=10)


def test_relay_call_roundtrip(pg, monkeypatch):
    db_url, engine, Session = pg
    key = Fernet.generate_key().decode()
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    pc_ids = [f"PC_C10_{n}" for n in range(10)]
    tokens = _seed_fleet(Session, jwt_secret, pc_ids)

    # Seeding as the API does at startup: twice, idempotent; the old websocket.*
    # rows of migration 004 stay, no new ones are seeded.
    with Session() as db:
        websocket_before = {r.key for r in db.query(SystemSetting).filter(SystemSetting.category == "websocket")}
        core_before = db.query(SystemSetting).filter(SystemSetting.category == "core").count()
        first = settings_seeder.seed_settings(db)
        second = settings_seeder.seed_settings(db)
        # An alembic-built database already holds migration 004's rows (other key
        # names); the seeder adds the missing ones, then nothing.
        assert core_before == 0 and first["created"] >= len(csc.KEYS) and second["created"] == 0
        assert {r.key for r in db.query(SystemSetting).filter(SystemSetting.category == "websocket")} == \
            websocket_before
        assert {r.key for r in db.query(SystemSetting).filter(SystemSetting.category == "core")} == set(csc.KEYS)

    api = PortalApi(Session, _service(monkeypatch, key), monkeypatch)
    relay = None
    stations = []
    try:
        assert api.put(csc.KEY_NOTIFICATION_ADDRESS, ADDRESS)["value"] == "********"
        assert api.put(csc.KEY_LOGS_SERVER_ADDRESS, "none")["value"] == "none"
        assert api.put(csc.KEY_HEARTBEAT_INTERVAL_SEC, "15")["value"] == "15"
        admin_hash = csc.make_admin_password_hash("c10-fixture-" + secrets.token_hex(4))
        assert api.put(csc.KEY_CLIENT_ADMIN_PASSWORD_HASH, admin_hash)["value"] == "********"
        with Session() as db:
            stored = {r.key: r.value for r in db.query(SystemSetting).filter(SystemSetting.category == "core")}
        assert SECRET_QUERY not in json.dumps(stored) and stored[csc.KEY_NOTIFICATION_ADDRESS]

        relay = Relay(db_url, jwt_secret, sender_secret, SETTINGS_ENCRYPTION_KEY=key,
                      RELAY_STATION_CONFIG_CACHE_SECONDS=str(CACHE_SECONDS))
        relay.proc.wait_for(f"Relay station config: cache={CACHE_SECONDS}s", timeout=10)
        caps = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
        for pc_id in pc_ids:
            stations.append(Pc(relay.url, pc_id, tokens[pc_id], capabilities=caps))
        station = stations[0]
        assert sp.FEATURE_STATION_CONFIG in station.registered["features"]

        expected = {
            csc.KEY_CLIENT_MANAGED_CONFIG: True,
            csc.KEY_NOTIFICATION_ADDRESS: ADDRESS,
            csc.KEY_LOGS_SERVER_ADDRESS: "none",
            csc.KEY_HEART_BEAT_SERVER_ADDRESS: "",
            csc.KEY_ACK_ADDRESS: "",
            csc.KEY_HEARTBEAT_INTERVAL_SEC: 15,
            csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: admin_hash,
        }
        ack = _call(station, {"pc_id": pc_ids[1], "config_status": {"core_revision": "x"}})
        assert ack == {"status": "ok", "revision": csc.revision(expected), "values": expected, "errors": []}
        assert all(value is not None for value in ack["values"].values())
        assert _loads(relay) == 1

        # 200 calls within 1 s from 10 stations: one database read.
        acks, failures = [], []

        def burst(st):
            try:
                for _ in range(20):
                    acks.append(_call(st))
            except Exception as exc:  # pragma: no cover - reported below
                failures.append(repr(exc))

        started = time.monotonic()
        threads = [threading.Thread(target=burst, args=(st,)) for st in stations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        elapsed = time.monotonic() - started
        assert not failures, failures[:3]
        assert len(acks) == 200 and all(a == ack for a in acks)
        assert elapsed < 1.0, f"200 calls took {elapsed:.2f}s"
        print(f"c10: 200 get_station_config calls from 10 stations in {elapsed:.3f}s, {_loads(relay)} read")
        assert _loads(relay) == 1

        # Unregistered and stale sessions are refused.
        import socketio

        anon = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        anon.connect(relay.url, wait_timeout=10)
        try:
            assert anon.call("get_station_config", {"pc_id": pc_ids[0]}, timeout=10) == {
                "status": "rejected", "reason": "not registered", "revision": None, "values": {}, "errors": []}
        finally:
            anon.disconnect()
        replacement = Pc(relay.url, pc_ids[0], tokens[pc_ids[0]], capabilities=caps)
        stations.append(replacement)
        assert _call(station)["status"] == "rejected"
        assert _call(replacement) == ack

        # A portal edit reaches the station once the cache expires, with a new revision.
        api.put(csc.KEY_ACK_ADDRESS, EDITED)
        edited = {**expected, csc.KEY_ACK_ADDRESS: EDITED}

        def sees_edit():
            current = _call(replacement)
            return current if current["values"] == edited else None

        new_ack = _wait(sees_edit, CACHE_SECONDS + 5, "the portal edit never reached the station")
        assert new_ack["revision"] == csc.revision(edited) != ack["revision"]
        loads_after_edit = _loads(relay)
        # The same value saved again (new ciphertext): after the next reload the revision is unchanged.
        api.put(csc.KEY_ACK_ADDRESS, EDITED)
        def after_reload():
            current = _call(replacement)  # a call past the TTL makes the relay read again
            return current if _loads(relay) > loads_after_edit else None

        reloaded = _wait(after_reload, CACHE_SECONDS + 5, "the relay never reloaded")
        assert _loads(relay) > loads_after_edit
        assert reloaded["revision"] == new_ack["revision"] and reloaded["values"] == edited
    finally:
        for st in stations:
            st.close()
        if relay is not None:
            relay.stop()
        api.close()

    log = relay.proc.text
    assert "features=deploy_result.v1,station_status.v1,station_config.v1,pc_token.v1" in log
    with Session() as db:
        ciphertexts = [r.value for r in db.query(SystemSetting).filter(SystemSetting.category == "core",
                                                                        SystemSetting.is_encrypted.is_(True)) if r.value]
    assert len(ciphertexts) == 4
    for secret_value in ciphertexts + [SECRET_QUERY, admin_hash, key, jwt_secret, sender_secret] + list(tokens.values()):
        assert secret_value not in log
    assert "errors=-" in log


def test_relay_without_a_valid_key(pg, monkeypatch):
    db_url, engine, Session = pg
    key = Fernet.generate_key().decode()
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    tokens = _seed_fleet(Session, jwt_secret, ["PC_C10_NK"])
    with Session() as db:
        settings_seeder.seed_settings(db)
    api = PortalApi(Session, _service(monkeypatch, key), monkeypatch)
    try:
        api.put(csc.KEY_ACK_ADDRESS, ADDRESS)
        api.put(csc.KEY_HEARTBEAT_INTERVAL_SEC, "30")
    finally:
        api.close()
    encrypted = [k for k in csc.KEYS if csc.SPECS[k].encrypted]
    for label, env in (("no_key", {}), ("invalid_key", {"SETTINGS_ENCRYPTION_KEY": "not-a-fernet-key"})):
        relay = Relay(db_url, jwt_secret, sender_secret, **env)
        station = None
        try:
            relay.proc.wait_for(f"settings_encryption_key={label}", timeout=10)
            station = Pc(relay.url, "PC_C10_NK", tokens["PC_C10_NK"])
            assert sp.FEATURE_STATION_CONFIG in station.registered["features"]
            ack = _call(station)
            assert ack["status"] == "ok"
            assert ack["values"] == {csc.KEY_CLIENT_MANAGED_CONFIG: True, csc.KEY_HEARTBEAT_INTERVAL_SEC: 30}
            assert ack["errors"] == [{"key": k, "error": label} for k in encrypted]
            assert ack["revision"] == csc.revision(ack["values"])
        finally:
            if station is not None:
                station.close()
            relay.stop()
        assert SECRET_QUERY not in relay.proc.text
        assert f"{csc.KEY_ACK_ADDRESS}:{label}" in relay.proc.text
