"""
Phase 0 compatibility: the real eventlet relay with real 1.1.1 Clients
(plan 06 D12, OD-23; M2 gate "a 1.1.1 ConnectionManager still registers and
acks").

Each PC is an unmodified ConnectionManager from the Client's 1.1.1 release
commit (``git archive``), run by the Client's own Python (PySide6,
python-socketio 5.10) with an isolated profile. The relay runs under
eventlet on Python 3.11, against a scratch Postgres built by alembic from
template0, with throwaway secrets, bound to 127.0.0.1.

* test_phase0_with_sender_secret: 1.1.1 PCs register, receive a layout from an
  authenticated sender (and from the API's own deploy path, D14: the row's
  config_id at the payload root, the 1.1.1 ACK moves the row to received
  within 2 s and stamps last_applied) and ACK it; an unauthenticated
  session's message is rejected and nobody is
  disconnected; a wrong sender secret is refused; PCs never receive
  clients_update; an outdated token is flagged; a deleted PC cannot
  register; the relay log holds no token, secret or layout.
* test_phase0_legacy_open: without RELAY_SENDER_SECRET, a sender that does
  not authenticate (the pre-D12 API) still delivers.
* test_phase0_require_current_token: with RELAY_REQUIRE_CURRENT_TOKEN the
  outdated token is refused and the current one registers.

Prerequisites (the test fails, never skips, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL, default postgresql://truelive@127.0.0.1:5433/postgres),
a Python 3.11 for the relay (TRUELIVE_RELAY_PYTHON, default this interpreter),
the Client repo next to the backend (TRUELIVE_CLIENT_REPO) and its Python
(TRUELIVE_CLIENT_PYTHON). TRUELIVE_CLIENT_111_REF names the release commit
(default d33b6f7, "Release 1.1.1").

    python -m pytest tests/integration/test_relay_phase0.py -o addopts='' -p no:cacheprovider
"""

import io
import json
import os
import queue
import secrets
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import jwt
import psycopg2
import pytest
import socketio

from app.services.relay_auth import sender_auth

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PG_ADMIN_URL = os.environ.get("TRUELIVE_TEST_PG_ADMIN_URL", "postgresql://truelive@127.0.0.1:5433/postgres")
RELAY_PYTHON = os.environ.get("TRUELIVE_RELAY_PYTHON", sys.executable)
CLIENT_REPO = Path(os.environ.get("TRUELIVE_CLIENT_REPO", str(BACKEND_ROOT.parent / "shomer_client")))
CLIENT_PYTHON = os.environ.get(
    "TRUELIVE_CLIENT_PYTHON", r"C:\Users\amdal\miniconda3\envs\shomer_middlewear\python.exe"
)
CLIENT_REF = os.environ.get("TRUELIVE_CLIENT_111_REF", "d33b6f7")
DRIVER = Path(__file__).with_name("station_client_driver.py")
LAYOUT_MARKER = "phase0-layout-marker-" + secrets.token_hex(4)
D12_ENV = ("SECRET_KEY", "JWT_SECRET", "RELAY_SENDER_SECRET", "RELAY_REQUIRE_CURRENT_TOKEN", "RELAY_DEBUG_LOGGING",
           "SETTINGS_ENCRYPTION_KEY", "TRUELIVE_RELAY_ASYNC_MODE", "DATABASE_URL")

from tests.integration.test_env_wiring import Proc, _base_env, _free_port  # noqa: E402  (shared harness)


def _layout(pc_id):
    return {"screens": [{"id": f"{pc_id}-screen-1", "name": LAYOUT_MARKER, "views": []}], "app_id": pc_id}


def _mint(pc_id, secret, exp_delta=8760 * 3600):
    return jwt.encode({"pc_id": pc_id, "name": f"{pc_id} wall", "exp": int(time.time()) + exp_delta,
                       "type": "pc_auth"}, secret, algorithm="HS256")


def _wait(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def client_src(tmp_path_factory):
    """The Client's src/ at the 1.1.1 release commit, extracted unmodified."""
    if not (CLIENT_REPO / ".git").exists():
        pytest.fail(f"Client repo not found at {CLIENT_REPO} (set TRUELIVE_CLIENT_REPO)")
    if not Path(CLIENT_PYTHON).exists():
        pytest.fail(f"Client Python not found at {CLIENT_PYTHON} (set TRUELIVE_CLIENT_PYTHON)")
    root = tmp_path_factory.mktemp("client111")
    archive = subprocess.run(["git", "-C", str(CLIENT_REPO), "archive", "--format=tar", CLIENT_REF, "src"],
                             capture_output=True, timeout=60)
    assert archive.returncode == 0, archive.stderr.decode(errors="replace")[-500:]
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root)
    version = (root / "src" / "config.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "1.1.1"' in version, "TRUELIVE_CLIENT_111_REF is not the 1.1.1 release"
    return root


@pytest.fixture(scope="module")
def scratch_db():
    """Alembic-built DB (template0) with a team and two PCs; dropped at the end."""
    name = f"d12_phase0_{os.getpid()}_{secrets.token_hex(3)}"
    try:
        admin = psycopg2.connect(PG_ADMIN_URL, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.fail(f"local test Postgres not reachable at {PG_ADMIN_URL} ({type(exc).__name__}); see env_server.md")
    admin.autocommit = True
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
def fleet(scratch_db):
    """Fresh PC rows and tokens for one relay run."""
    jwt_secret = secrets.token_hex(24)
    tokens = {"PC_A": _mint("PC_A", jwt_secret), "PC_B_stored": _mint("PC_B", jwt_secret)}
    tokens["PC_B"] = _mint("PC_B", jwt_secret, exp_delta=4000 * 3600)  # valid, but not the stored one
    tokens["PC_GONE"] = _mint("PC_GONE", jwt_secret)  # valid token, PC deleted
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        for table in ("audit_logs", "pc_deployments", "pcs", "screen_layouts", "teams"):
            cur.execute(f"DELETE FROM {table}")
        cur.execute("INSERT INTO teams (id, name) VALUES ('TEAM_p0', 'Phase 0')")
        cur.execute("INSERT INTO screen_layouts (id, name, team_id) VALUES ('LAYOUT_p0', 'Phase 0 wall', 'TEAM_p0')")
        for pc_id, token in (("PC_A", tokens["PC_A"]), ("PC_B", tokens["PC_B_stored"])):
            cur.execute("INSERT INTO pcs (id, name, role, team_id, auth_token, screen_layout_id) VALUES "
                        "(%s, %s, 'controller', 'TEAM_p0', %s, 'LAYOUT_p0')", (pc_id, f"{pc_id} wall", token))
    return {"db": scratch_db, "jwt_secret": jwt_secret, "tokens": tokens}


class Relay:
    def __init__(self, fleet, sender_secret="", require_current=False):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.sender_secret = sender_secret
        env = _base_env(
            DATABASE_URL=fleet["db"], JWT_SECRET=fleet["jwt_secret"], WEBSOCKET_PORT=str(self.port),
            RELAY_SENDER_SECRET=sender_secret, RELAY_REQUIRE_CURRENT_TOKEN="true" if require_current else "false",
            RELAY_DEBUG_LOGGING="false", WS_LAST_SEEN_SWEEP_SECONDS="1",
        )
        env.pop("TRUELIVE_RELAY_ASYNC_MODE", None)
        self.proc = Proc([RELAY_PYTHON, "-u", "-m", "app.services.websocket_server"], env)
        self.proc.wait_for("Relay trust:", timeout=60)
        _wait(self._listening, 30, "relay did not start listening")

    def _listening(self):
        import socket as socket_

        try:
            with socket_.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def stop(self):
        self.proc.stop()


class Station:
    """One 1.1.1 ConnectionManager in the Client's Python."""

    def __init__(self, client_src, tmp_path, url, pc_id, token):
        profile = tmp_path / f"profile_{pc_id}_{secrets.token_hex(2)}"
        env = {k: v for k, v in os.environ.items() if k not in D12_ENV and not k.startswith("PYTHON")}
        for name, sub in (("USERPROFILE", "home"), ("HOME", "home"), ("APPDATA", "appdata"),
                          ("LOCALAPPDATA", "localappdata"), ("PROGRAMDATA", "programdata")):
            (profile / sub).mkdir(parents=True, exist_ok=True)
            env[name] = str(profile / sub)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["NO_PROXY"] = "127.0.0.1,localhost"
        self.profile = profile
        self.events = queue.Queue()
        self.seen = []
        self.stderr_lines = []
        self.proc = subprocess.Popen([CLIENT_PYTHON, "-I", str(DRIVER), str(client_src)], cwd=str(profile), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace")
        self.proc.stdin.write(json.dumps({"url": url, "pc_id": pc_id, "token": token, "run_seconds": 120}) + "\n")
        self.proc.stdin.flush()
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self):
        for line in self.proc.stdout:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            self.seen.append(item)
            self.events.put(item)

    def _read_err(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line)

    def wait_event(self, name, timeout=20):
        def found():
            return next((e for e in self.seen if e["event"] == name), None)

        return _wait(found, timeout, f"station never reported {name!r}; saw {[e['event'] for e in self.seen]}")

    def count(self, name):
        return sum(1 for e in self.seen if e["event"] == name)

    def values(self, name):
        return [e["value"] for e in self.seen if e["event"] == name]

    def stop(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)


class Sender:
    """A portal-side sender session (python-socketio from the backend env)."""

    def __init__(self, url, auth=None, connect=True):
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self.events = []
        for name in ("message_sent", "message_ack", "error", "clients_update", "clients_list"):
            self.sio.on(name, self._recorder(name))
        if connect:
            self.sio.connect(url, auth=auth, wait_timeout=10)

    def _recorder(self, name):
        def handler(data=None):
            self.events.append((name, data))

        return handler

    def got(self, name):
        return [d for n, d in self.events if n == name]

    def clients(self, timeout=5):
        before = len(self.got("clients_list"))
        self.sio.emit("get_clients")
        _wait(lambda: len(self.got("clients_list")) > before, timeout, "no clients_list")
        return {c["pc_id"]: c for c in self.got("clients_list")[-1]["clients"]}

    def close(self):
        try:
            self.sio.disconnect()
        except Exception:
            pass


@pytest.fixture
def cleanup():
    items = []
    yield items
    for item in reversed(items):
        item.stop() if hasattr(item, "stop") else item.close()


def _assert_no_leaks(text, fleet, extra=()):
    for token in fleet["tokens"].values():
        assert token not in text
    assert fleet["jwt_secret"] not in text
    assert LAYOUT_MARKER not in text
    for value in extra:
        if value:
            assert value not in text


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_phase0_with_sender_secret(client_src, fleet, tmp_path, cleanup, monkeypatch):
    sender_secret = secrets.token_urlsafe(32)
    relay = Relay(fleet, sender_secret=sender_secret)
    cleanup.append(relay)
    tokens = fleet["tokens"]

    station_a = Station(client_src, tmp_path, relay.url, "PC_A", tokens["PC_A"])
    cleanup.append(station_a)
    assert station_a.wait_event("registered")["value"] == "PC_A"
    station_b = Station(client_src, tmp_path, relay.url, "PC_B", tokens["PC_B"])  # outdated token
    cleanup.append(station_b)
    assert station_b.wait_event("registered")["value"] == "PC_B"
    gone = Station(client_src, tmp_path, relay.url, "PC_GONE", tokens["PC_GONE"])  # deleted PC
    cleanup.append(gone)
    assert gone.wait_event("error")["value"] == "This PC is not registered in the portal"

    sender = Sender(relay.url, auth=sender_auth(sender_secret))
    cleanup.append(sender)
    clients = sender.clients()
    assert set(clients) == {"PC_A", "PC_B"}
    assert clients["PC_A"]["flags"] == {}
    assert clients["PC_B"]["flags"] == {"token_outdated": True}

    # A layout from the authenticated sender reaches the 1.1.1 PC, which ACKs it.
    sender.sio.emit("message", {"type": "config", "targetId": "PC_A", "content": _layout("PC_A")})
    assert station_a.wait_event("config")["value"] == _layout("PC_A")
    _wait(lambda: sender.got("message_ack"), 10, "no message_ack: the 1.1.1 PC did not ACK the config")
    assert sender.got("message_sent") == [{"targetId": "PC_A", "status": "delivered"}]
    assert sender.got("message_ack") == [{"targetId": "PC_A", "status": "received"}]

    # The API's own deploy path (deploy_service, D14) authenticates and delivers to the 1.1.1 PC.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.models.pc import PC
    from app.models.pc_tracking import PcDeployment
    from app.services import deploy_service

    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    monkeypatch.setattr("app.services.config_loader.load_pc_config", lambda pc_id, db: {"pc": pc_id})
    monkeypatch.setattr("app.services.config_generator.generate_config", lambda cfg, db: _layout("PC_B"))
    engine = create_engine(fleet["db"])
    Session = sessionmaker(bind=engine, autoflush=False)
    try:
        with Session() as db:
            summary = deploy_service.deploy_config_to_pcs([db.get(PC, "PC_B")], db,
                                                          trigger=deploy_service.TRIGGER_PC_DEPLOY)
        returned = time.monotonic()
        assert summary["deployed"] == 1, summary
        result = summary["results"][0]
        assert (result["delivery"], result["confirmable"]) == ("delivered", False)  # 1.1.1 cannot confirm
        # The 1.1.1 Client receives the layout with the deploy's config_id at the root ...
        assert station_b.wait_event("config")["value"] == dict(_layout("PC_B"), config_id=result["config_id"])

        # ... and its ACK moves the row to received within 2 s, stamping last_applied.
        def received():
            with Session() as db:
                row = db.get(PcDeployment, result["config_id"])
                return row.status == "received" and row

        row = _wait(received, 2.0, "the 1.1.1 ACK did not move the row to received within 2 s")
        assert time.monotonic() - returned < 2.0
        with Session() as db:
            assert db.get(PC, "PC_B").last_applied == row.received_at
            assert db.get(PC, "PC_A").last_applied is None
        # ... and with the wrong secret the API cannot connect: the deploy is failed, not "deployed".
        monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", "wrong-" + secrets.token_hex(16))
        with Session() as db:
            failed = deploy_service.deploy_config_to_pcs([db.get(PC, "PC_B")], db,
                                                         trigger=deploy_service.TRIGGER_PC_DEPLOY)
        assert failed["failed"] == 1 and failed["deployed"] == 0
        with Session() as db:
            assert db.get(PcDeployment, failed["results"][0]["config_id"]).status == "relay_failed"
        assert station_b.count("config") == 1
    finally:
        engine.dispose()

    # An unauthenticated session may connect, but its message is rejected and nobody is disconnected.
    intruder = Sender(relay.url)
    cleanup.append(intruder)
    intruder.sio.emit("message", {"type": "config", "targetId": "PC_A", "content": _layout("PC_A")})
    _wait(lambda: intruder.got("error"), 10, "intruder got no error")
    assert intruder.got("error") == [{"message": "Not authorized"}]
    intruder.sio.emit("get_clients")
    _wait(lambda: len(intruder.got("error")) == 2, 10, "get_clients was not refused")
    time.sleep(1.0)
    assert intruder.sio.connected and sender.sio.connected
    assert station_a.count("config") == 1
    assert station_a.proc.poll() is None and station_b.proc.poll() is None
    assert station_a.values("state")[-1] == "connected" and station_b.values("state")[-1] == "connected"

    # A disallowed type from the real sender is rejected too.
    sender.sio.emit("message", {"type": "command", "targetId": "PC_A", "content": {}})
    _wait(lambda: sender.got("error"), 10, "type allow-list did not reject")
    assert sender.got("error") == [{"message": "Message type not allowed"}]

    # A sender with the wrong secret is refused at connect.
    with pytest.raises(socketio.exceptions.ConnectionError):
        Sender(relay.url, auth=sender_auth("wrong-" + secrets.token_hex(8)))

    # Presence: PC_B leaves; the sender is told, the PCs never are.
    station_b.stop()
    _wait(lambda: any(sorted(c["pc_id"] for c in u["clients"]) == ["PC_A"] for u in sender.got("clients_update")),
          15, "sender got no clients_update after PC_B left")
    time.sleep(0.5)
    for station in (station_a, station_b, gone):
        assert station.count("clients_update") == 0
    assert gone.count("registered") == 0

    # The relay stamped the registered PCs (and the sweep keeps last_seen fresh).
    with psycopg2.connect(fleet["db"]) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, last_connected, last_seen FROM pcs ORDER BY id")
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["PC_A", "PC_B"] and all(r[1] and r[2] for r in rows)

    for item in (sender, intruder, station_a, gone):
        item.stop() if hasattr(item, "stop") else item.close()
    relay.stop()
    _assert_no_leaks(relay.proc.text, fleet, extra=(sender_secret,))
    assert "Relay trust: sender_auth=required require_current_token=false packet_logging=off" in relay.proc.text
    assert "token_outdated" in relay.proc.text and "PC_GONE not found in database" in relay.proc.text


def test_phase0_legacy_open(client_src, fleet, tmp_path, cleanup):
    relay = Relay(fleet, sender_secret="")
    cleanup.append(relay)
    station = Station(client_src, tmp_path, relay.url, "PC_A", fleet["tokens"]["PC_A"])
    cleanup.append(station)
    assert station.wait_event("registered")["value"] == "PC_A"

    old_portal = Sender(relay.url)  # connects without any auth, like the pre-D12 API
    cleanup.append(old_portal)
    old_portal.sio.emit("message", {"type": "config", "targetId": "PC_A", "content": _layout("PC_A")})
    assert station.wait_event("config")["value"] == _layout("PC_A")
    _wait(lambda: old_portal.got("message_ack"), 10, "no message_ack in legacy-open mode")
    assert old_portal.got("message_sent") == [{"targetId": "PC_A", "status": "delivered"}]
    assert station.count("clients_update") == 0

    old_portal.close()
    station.stop()
    relay.stop()
    assert "legacy-open" in relay.proc.text
    _assert_no_leaks(relay.proc.text, fleet)


def test_phase0_require_current_token(client_src, fleet, tmp_path, cleanup):
    relay = Relay(fleet, sender_secret=secrets.token_urlsafe(32), require_current=True)
    cleanup.append(relay)
    outdated = Station(client_src, tmp_path, relay.url, "PC_B", fleet["tokens"]["PC_B"])
    cleanup.append(outdated)
    assert outdated.wait_event("error")["value"] == (
        "Authentication token has been replaced; enter the current PC token"
    )
    current = Station(client_src, tmp_path, relay.url, "PC_B", fleet["tokens"]["PC_B_stored"])
    cleanup.append(current)
    assert current.wait_event("registered")["value"] == "PC_B"
    time.sleep(0.5)
    assert outdated.count("registered") == 0
    current.stop()
    outdated.stop()
    relay.stop()
    _assert_no_leaks(relay.proc.text, fleet, extra=(relay.sender_secret,))
