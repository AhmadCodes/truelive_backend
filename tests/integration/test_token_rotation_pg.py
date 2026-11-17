"""
JWT_SECRET rotation through D30's dual-secret window, with real processes
(plan 06 D30; the card's staging test ``secret_rotation_200_pcs``, run locally).

The real eventlet relay (Python 3.11) runs against a scratch Postgres built by
alembic, on 127.0.0.1, with throwaway secrets. Every PC row holds a token
signed with the OLD secret (the fleet before the rotation).

* test_secret_rotation_200_pcs:
  1. Rotation: the relay runs with JWT_SECRET=new, JWT_SECRET_PREVIOUS=old and
     a 14-day window. 200 stations (python-socketio AsyncClients, D15's storm
     clients extended with a ``pc_token_renewed`` handler that applies the
     Client's rules: type pc_auth, its own pc_id, a later expiry) connect with
     their old tokens at once. All register within 5 s, all get a new-secret
     token on their own sid and confirm it; pcs.auth_token now verifies with
     the new secret, every renewal row is confirmed, and every issue and
     confirmation is audited without a token. Stations without the handler
     (old capabilities, and a real 1.1.1 ConnectionManager) register
     unaffected, get nothing, keep their stored token, and are flagged
     token_drop_needed and listed by the fleet report.
  2. One reconnect cycle later the 200 register with the new tokens, without
     flags and without another renewal. A reconnect cycle is a relay restart
     (how a rotation is rolled out; it also avoids a transport quirk: the
     relay never sees a python-socketio *polling* client's disconnect and
     drops it only at the ping timeout, about 85 s).
  3. After the window (relay restarted with the end in the past) the new
     tokens register and every old-secret token is refused.
* test_expiry_renewal_exactly_once_over_sockets: a current-secret token that
  expires in 10 days is renewed once; a station that does not confirm gets the
  same token again on reconnect (no second mint); confirming closes it.

Not run here (needs the Client part, M6): one real Client 1.4.0 under
``--background`` taking the token.

Prerequisites (failing, never skipping, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL), a Python 3.11 for the relay
(TRUELIVE_RELAY_PYTHON), and for the 1.1.1 station the Client repo and its
Python (see test_relay_phase0.py).

    python -m pytest tests/integration/test_token_rotation_pg.py -o addopts='' -p no:cacheprovider
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import socketio
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.pc import PC
from app.models.pc_tracking import PcHealth, PcTokenRenewal
from app.models.team import Team
from app.schemas import station_protocol as sp
from app.services import pc_tokens, relay_auth
from app.services.relay_auth import sender_auth
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env
from tests.integration.test_relay_phase0 import Station, client_src  # noqa: F401  (fixture)
from tests.integration.test_relay_reports_pg import Relay

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

N = 200
DAY = 86400
CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_PC_TOKEN]
OLD_CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]


@pytest.fixture(scope="module")
def scratch_db():
    name = f"d30_rotation_{os.getpid()}_{secrets.token_hex(3)}"
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
        for table in ("audit_logs", "pc_token_renewals", "pc_health", "pc_deployments", "pcs", "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    yield scratch_db, engine, sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def _mint(pc_id, secret, exp_delta=8760 * 3600):
    return jwt.encode({"pc_id": pc_id, "name": f"{pc_id} wall", "exp": int(time.time()) + exp_delta,
                       "type": "pc_auth"}, secret, algorithm="HS256")


def _claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def _seed(Session, tokens):
    with Session() as db:
        db.add(Team(id="TEAM_d30", name="D30"))
        db.flush()
        db.bulk_insert_mappings(PC, [
            {"id": pc_id, "name": f"{pc_id} wall", "role": "controller", "team_id": "TEAM_d30",
             "auth_token": token, "token_expiry": _claims(token)["exp"]}
            for pc_id, token in tokens.items()
        ])
        db.commit()


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


class LoopThread:
    """An asyncio loop on its own thread, so the stations keep processing
    events (and send their ACKs) while the test waits synchronously."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


class Fleet:
    """python-socketio AsyncClient stations. Stations with ``pc_token.v1``
    answer ``pc_token_renewed`` the way the Client part (M6) must: check the
    type, the pc_id and a later expiry, store the token, ACK ``stored``."""

    def __init__(self, url):
        self.url = url
        self.held = {}  # pc_id -> the token the station holds
        self.registered = {}
        self.errors = {}
        self.renewals = {}  # pc_id -> [payload, ...]
        self.confirm = True

    async def run(self, entries, hold_open_until=None):
        """Connect and register every (pc_id, capabilities); return the seconds
        until the last ``registered``. Clients are left connected."""
        self.clients = []
        started = time.monotonic()
        self.registered = {}
        self.errors = {}

        async def one(pc_id, capabilities):
            client = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
            done = asyncio.get_running_loop().create_future()

            @client.on("registered")
            async def _registered(data):
                if not done.done():
                    done.set_result(("ok", data))

            @client.on("error")
            async def _error(data):
                self.errors[pc_id] = data
                if not done.done():
                    done.set_result(("error", data))

            if sp.FEATURE_PC_TOKEN in capabilities:
                @client.on(pc_tokens.EVENT_RENEWED)
                async def _renewed(data):
                    self.renewals.setdefault(pc_id, []).append(data)
                    token = data.get("token") if isinstance(data, dict) else None
                    try:
                        claims = _claims(token)
                    except Exception:
                        return {"status": "refused", "reason": "invalid"}
                    if claims.get("type") != "pc_auth":
                        return {"status": "refused", "reason": "wrong_type"}
                    if claims.get("pc_id") != pc_id:
                        return {"status": "refused", "reason": "other_pc"}
                    if not isinstance(claims.get("exp"), int) or claims["exp"] <= _claims(self.held[pc_id])["exp"]:
                        return {"status": "refused", "reason": "older_exp"}
                    if not self.confirm:
                        return None  # a bare ACK: the station did not store it
                    self.held[pc_id] = token
                    return {"status": "stored"}

            self.clients.append(client)
            await client.connect(self.url, transports=["polling"], wait_timeout=15)
            payload = {"pc_id": pc_id, "auth_token": self.held[pc_id], "client_version": "1.4.0"}
            if capabilities:
                payload["capabilities"] = capabilities
            await client.emit("register", payload)
            kind, _data = await asyncio.wait_for(done, 15)
            if kind == "ok":
                self.registered[pc_id] = time.monotonic()

        await asyncio.gather(*(one(pc_id, caps) for pc_id, caps in entries))
        return (max(self.registered.values()) - started) if self.registered else None

    async def wait_renewals(self, pc_ids, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(self.renewals.get(pc_id) for pc_id in pc_ids):
                return
            await asyncio.sleep(0.05)
        missing = [p for p in pc_ids if not self.renewals.get(p)]
        raise AssertionError(f"{len(missing)} station(s) got no renewal, e.g. {missing[:3]}")

    async def close(self):
        await asyncio.gather(*(c.disconnect() for c in self.clients), return_exceptions=True)
        self.clients = []


def _sender_clients(url, secret):
    sender = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    got = []
    sender.on("clients_list", lambda data: got.append(data))
    sender.connect(url, auth=sender_auth(secret), wait_timeout=10)
    try:
        sender.emit("get_clients", {})
        _wait(lambda: got, 5, "no clients_list")
        return {c["pc_id"]: c["flags"] for c in got[0]["clients"]}
    finally:
        sender.disconnect()


def _audit_rows(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT action, resource_id, changes, actor_type FROM audit_logs "
                                 "WHERE action LIKE 'pc.token.%'")).all()


def test_secret_rotation_200_pcs(pg, client_src, tmp_path):
    db_url, engine, Session = pg
    old_secret, new_secret = secrets.token_hex(24), secrets.token_hex(24)
    sender_secret = secrets.token_urlsafe(32)
    renewable = [f"PC_R{i:03d}" for i in range(N)]
    no_handler = ["PC_OLD1", "PC_OLD2", "PC_OLD3"]
    legacy = "PC_LEGACY"  # the real 1.1.1 ConnectionManager
    old_tokens = {pc: _mint(pc, old_secret) for pc in renewable + no_handler + [legacy]}
    _seed(Session, old_tokens)
    window_end = datetime.now(timezone.utc) + timedelta(days=14)

    fleet = Fleet(None)
    fleet.held = dict(old_tokens)
    logs = []
    relay = Relay(db_url, new_secret, sender_secret, JWT_SECRET_PREVIOUS=old_secret,
                  JWT_ROTATION_GRACE_UNTIL=window_end.isoformat())
    station = None
    try:
        fleet.url = relay.url

        # 1. The rotation: every station reconnects with its old-secret token.
        entries = [(pc, CAPS) for pc in renewable] + [(pc, OLD_CAPS) for pc in no_handler[:2]] + [(no_handler[2], [])]
        started = time.monotonic()
        loop = LoopThread()
        try:
            elapsed = loop.run(fleet.run(entries))
            loop.run(fleet.wait_renewals(renewable, 20))
            renewed_after = time.monotonic() - started
            station = Station(client_src, tmp_path, relay.url, legacy, old_tokens[legacy])
            station.wait_event("registered", timeout=60)
            assert station.count("error") == 0, station.values("error")

            assert not fleet.errors, list(fleet.errors.items())[:3]
            assert len(fleet.registered) == N + len(no_handler)
            assert elapsed < 5.0, f"{N} registrations took {elapsed:.2f}s"

            def confirmed():
                with Session() as db:
                    return db.query(PcTokenRenewal).filter(PcTokenRenewal.confirmed_at.isnot(None)).count() == N

            _wait(confirmed, 20, "not every renewal was confirmed")
            confirmed_after = time.monotonic() - started

            # Each renewable station got exactly one token, on its own sid, for itself, signed with the new secret.
            for pc in renewable:
                assert len(fleet.renewals[pc]) == 1, pc
                payload = fleet.renewals[pc][0]
                claims = jwt.decode(payload["token"], new_secret, algorithms=["HS256"])
                assert (claims["pc_id"], claims["type"], claims["exp"]) == (pc, "pc_auth", payload["exp"])
                assert fleet.held[pc] == payload["token"]
            assert not any(fleet.renewals.get(pc) for pc in no_handler)
            with Session() as db:
                rows = {r.id: r for r in db.query(PC).all()}
                for pc in renewable:
                    assert rows[pc].auth_token == fleet.held[pc] and rows[pc].token_expiry == _claims(fleet.held[pc])["exp"]
                    assert (rows[pc].updated_by_type, rows[pc].updated_by_label) == ("system", "system")
                for pc in no_handler + [legacy]:
                    assert rows[pc].auth_token == old_tokens[pc]  # untouched
                renewals = db.query(PcTokenRenewal).all()
                assert len(renewals) == N and {r.pc_id for r in renewals} == set(renewable)
                assert all(r.previous_token_sha256 == relay_auth.token_digest(old_tokens[r.pc_id]) for r in renewals)

            flags = _sender_clients(relay.url, sender_secret)
            assert all(flags[pc] == {} for pc in renewable), [flags[pc] for pc in renewable[:3]]
            for pc in no_handler + [legacy]:
                assert flags[pc] == {"token_previous_secret": True, "token_drop_needed": True}, (pc, flags[pc])

            # The fleet report (what ops watches) after the relay's health flush.
            def flushed():
                with Session() as db:
                    row = db.get(PcHealth, legacy)
                    return row is not None and (row.relay_flags or {}).get("token_drop_needed")

            _wait(flushed, 5, "relay flags never reached pc_health")
            cfg = type("Cfg", (), dict(JWT_SECRET=new_secret, JWT_SECRET_PREVIOUS=old_secret, ALGORITHM="HS256",
                                       JWT_ROTATION_GRACE_UNTIL=window_end, PC_TOKEN_RENEW_BEFORE_DAYS=30))
            with Session() as db:
                report = pc_tokens.fleet_report(db, cfg)
            assert report["window"] == "open"
            assert report["secrets"] == {"current": N, "previous": 4, "unknown": 0, "none": 0}
            assert report["counts"]["ok"] == N and report["counts"]["needs_drop"] == 4
            assert {p["pc_id"] for p in report["pcs"]} == set(no_handler + [legacy])
            report_text = json.dumps(report)

            # 2. One reconnect cycle later (the relay restarts): new tokens, no flags, no second renewal.
            loop.run(fleet.close())
            station.stop()
            station = None
            relay.stop()
            logs.append(relay.proc.text)
            relay = Relay(db_url, new_secret, sender_secret, JWT_SECRET_PREVIOUS=old_secret,
                          JWT_ROTATION_GRACE_UNTIL=window_end.isoformat())
            fleet.url = relay.url
            before = {pc: len(fleet.renewals.get(pc, [])) for pc in renewable}
            elapsed2 = loop.run(fleet.run([(pc, CAPS) for pc in renewable]))
            assert not fleet.errors and len(fleet.registered) == N and elapsed2 < 5.0
            time.sleep(1.0)
            assert {pc: len(fleet.renewals.get(pc, [])) for pc in renewable} == before
            flags = _sender_clients(relay.url, sender_secret)
            odd = {pc: flags.get(pc) for pc in renewable if flags.get(pc) != {}}
            assert not odd, (len(odd), list(odd.items())[:5])
            loop.run(fleet.close())
        finally:
            if getattr(fleet, "clients", None):
                loop.run(fleet.close())
            loop.close()
    finally:
        if station is not None:
            station.stop()
        relay.stop()
    logs.append(relay.proc.text)
    log = "".join(logs)
    assert "Relay tokens: rotation_window=open until=" in logs[0] and "renew_before_days=30" in logs[0]
    assert "features=deploy_result.v1,station_status.v1,station_config.v1,pc_token.v1" in log
    assert "token_drop_needed" in log
    for value in [old_secret, new_secret, sender_secret] + list(old_tokens.values()) + list(fleet.held.values()):
        assert value not in log

    # Audit: one issue and one confirmation per renewed PC, never a token or a digest.
    audit = _audit_rows(engine)
    actions = [a for a, _r, _c, _t in audit]
    assert actions.count(pc_tokens.AUDIT_ISSUED) == N and actions.count(pc_tokens.AUDIT_CONFIRMED) == N
    assert {t for *_x, t in audit} == {"system"}
    audit_text = json.dumps([c for _a, _r, c, _t in audit])
    for token in list(old_tokens.values()) + [fleet.held[pc] for pc in renewable]:
        assert token not in audit_text and token not in report_text
        assert relay_auth.token_digest(token) not in audit_text
    assert "eyJ" not in audit_text and "eyJ" not in report_text

    # 3. After the window: new tokens register, every old-secret token is refused.
    relay = Relay(db_url, new_secret, sender_secret, JWT_SECRET_PREVIOUS=old_secret,
                  JWT_ROTATION_GRACE_UNTIL=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    station = None
    loop = LoopThread()
    try:
        fleet.url = relay.url
        elapsed3 = loop.run(fleet.run([(pc, CAPS) for pc in renewable[:50]]
                                                     + [(pc, OLD_CAPS) for pc in no_handler]))
        assert len(fleet.registered) == 50 and set(fleet.errors) == set(no_handler)
        assert all(e == {"message": "Invalid or expired authentication token"} for e in fleet.errors.values())
        station = Station(client_src, tmp_path, relay.url, legacy, old_tokens[legacy])
        station.wait_event("error", timeout=60)
        assert station.values("error")[0] == "Invalid or expired authentication token"
        assert station.count("registered") == 0
        loop.run(fleet.close())
    finally:
        loop.close()
        if station is not None:
            station.stop()
        relay.stop()
    assert "Relay tokens: rotation_window=closed" in relay.proc.text
    print(f"rotation: {N} registrations in {elapsed:.2f}s, all renewed after {renewed_after:.2f}s, "
          f"confirmed after {confirmed_after:.2f}s; reconnect with new tokens {elapsed2:.2f}s; "
          f"after the window {elapsed3:.2f}s")


def test_expiry_renewal_exactly_once_over_sockets(pg):
    db_url, engine, Session = pg
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    tokens = {"PC_EXP": _mint("PC_EXP", jwt_secret, exp_delta=10 * DAY), "PC_FRESH": _mint("PC_FRESH", jwt_secret)}
    _seed(Session, tokens)
    fleet = Fleet(None)
    fleet.held = dict(tokens)
    logs = []
    relay = Relay(db_url, jwt_secret, sender_secret)
    loop = LoopThread()
    try:
        fleet.url = relay.url
        fleet.confirm = False  # first time: the station answers with a bare ACK (it did not store it)
        loop.run(fleet.run([("PC_EXP", CAPS), ("PC_FRESH", CAPS)]))
        loop.run(fleet.wait_renewals(["PC_EXP"], 10))
        first = fleet.renewals["PC_EXP"][0]
        time.sleep(0.5)
        assert "PC_FRESH" not in fleet.renewals
        with Session() as db:
            row = db.query(PcTokenRenewal).one()
            assert row.pc_id == "PC_EXP" and row.confirmed_at is None
            assert db.get(PC, "PC_EXP").auth_token == first["token"]
        loop.run(fleet.close())
        relay.stop()
        logs.append(relay.proc.text)
        relay = Relay(db_url, jwt_secret, sender_secret)
        fleet.url = relay.url

        # Reconnect with the old token: the same new token again, no second mint.
        fleet.confirm = True
        loop.run(fleet.run([("PC_EXP", CAPS)]))
        loop.run(fleet.wait_renewals(["PC_EXP"], 10))
        _wait(lambda: len(fleet.renewals["PC_EXP"]) == 2, 10, "no second delivery")
        assert fleet.renewals["PC_EXP"][1] == first

        def confirmed():
            with Session() as db:
                return db.query(PcTokenRenewal).one().confirmed_at is not None

        _wait(confirmed, 10, "the renewal was not confirmed")
        loop.run(fleet.close())
        relay.stop()
        logs.append(relay.proc.text)
        relay = Relay(db_url, jwt_secret, sender_secret)
        fleet.url = relay.url

        # With the new token: nothing more.
        loop.run(fleet.run([("PC_EXP", CAPS)]))
        time.sleep(1.0)
        assert len(fleet.renewals["PC_EXP"]) == 2
        loop.run(fleet.close())
    finally:
        loop.close()
        relay.stop()
    logs.append(relay.proc.text)
    with Session() as db:
        assert db.query(PcTokenRenewal).count() == 1
    actions = [a for a, *_ in _audit_rows(engine)]
    assert actions.count(pc_tokens.AUDIT_ISSUED) == 1 and actions.count(pc_tokens.AUDIT_CONFIRMED) == 1
    log = "".join(logs)
    assert "same token again" in log
    for value in list(tokens.values()) + [first["token"], jwt_secret, sender_secret]:
        assert value not in log
