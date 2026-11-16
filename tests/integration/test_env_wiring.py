"""
Shared env wiring with real processes and a scratch Postgres (plan 06 D28 + C18).

* test_relay_starts_with_the_broker_down: the real eventlet relay logs its
  startup_check, warns that the broker is unreachable and still serves.
* test_api_to_relay_key_parity: with the env docker-compose.yml gives the API
  and the relay, a core.* value the API encrypts into Postgres decrypts in a
  relay-env process, and their fingerprints match. Without the relay's
  env_file (the compose file before D28) it cannot.
* test_fingerprints_match_in_all_four_services: API, relay, Celery worker and
  Celery beat (real processes) log the same fingerprints.
* test_missing_key_refuses_encrypted_core_write_on_postgres: the settings API
  answers 503 and writes nothing without the key, and 200 with it.

Needs a local Postgres: TRUELIVE_TEST_PG_ADMIN_URL (default
postgresql://truelive@127.0.0.1:5433/postgres, the dev-box cluster from
env_server.md). The relay runs under eventlet 0.33.3, which needs Python 3.11
(as in production); from a 3.12 env, set TRUELIVE_RELAY_PYTHON to a 3.11
interpreter with the backend requirements. Every database is created from
template0 with alembic and dropped afterwards; no production data is used.
Every process binds 127.0.0.1, and every process started here is stopped.

    python -m pytest tests/integration/test_env_wiring.py -o addopts='' -p no:cacheprovider
"""

import json
import os
import re
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
import yaml
from cryptography.fernet import Fernet

from app.core.startup_check import fingerprint

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PG_ADMIN_URL = os.environ.get("TRUELIVE_TEST_PG_ADMIN_URL", "postgresql://truelive@127.0.0.1:5433/postgres")
RELAY_PYTHON = os.environ.get("TRUELIVE_RELAY_PYTHON", sys.executable)
PLAINTEXT = "wss://203.0.113.30/ws/truelive/{pc_id}?key=fixture-integration"
D28_VARIABLES = (
    "SECRET_KEY", "JWT_SECRET", "SETTINGS_ENCRYPTION_KEY", "RELAY_SENDER_SECRET",
    "DEPLOY_INJECT_CONFIG_ID", "RELAY_STATION_REPORTS", "DEPLOY_CATCHUP_ENABLED",
    "DEPLOY_ACK_TIMEOUT_SEC", "RELAY_DEBUG_LOGGING",
)
_STARTUP_RE = re.compile(
    r"startup_check service=(\S+) .*relay_sender_secret=(\S+) settings_encryption_key=(\S+)(?: (fp:\w+))?"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _admin_connection():
    try:
        conn = psycopg2.connect(PG_ADMIN_URL, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.fail(f"local test Postgres not reachable at {PG_ADMIN_URL} ({type(exc).__name__}); see env_server.md")
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def scratch_db():
    """An empty database built by alembic (template0), dropped at the end."""
    name = f"d28_envwiring_{os.getpid()}_{secrets.token_hex(3)}"
    admin = _admin_connection()
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}" OWNER truelive TEMPLATE template0 ENCODING \'UTF8\' '
                    "LC_COLLATE 'C' LC_CTYPE 'C'")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        env = _base_env(DATABASE_URL=url)
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(BACKEND_ROOT),
                                env=env, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _base_env(**overrides):
    """A safe process env: throwaway secrets, every outward default on 127.0.0.1."""
    env = {key: value for key, value in os.environ.items() if key not in D28_VARIABLES}
    env.update(
        SECRET_KEY=secrets.token_hex(32),
        JWT_SECRET=secrets.token_hex(16),
        DATABASE_URL=PG_ADMIN_URL.rsplit("/", 1)[0] + "/d28_unused",
        ENVIRONMENT="test",
        REDIS_URL=f"redis://127.0.0.1:{_free_port()}/0",
        CELERY_BROKER_URL=f"redis://127.0.0.1:{_free_port()}/0",
        CELERY_RESULT_BACKEND=f"redis://127.0.0.1:{_free_port()}/1",
        SMTP_HOST="127.0.0.1", SMTP_PORT="18025", SMTP_USER="", SMTP_PASSWORD="",
        MINIO_ENDPOINT="127.0.0.1:18099", MINIO_SECURE="false", ALERT_DOMAIN="alerts.example.com",
        WEBSOCKET_HOST="127.0.0.1",
        PYTHONUNBUFFERED="1",
        PGGSSENCMODE="disable",
    )
    env.update(overrides)
    return env


def _compose_env(service, dotenv, with_env_file=True):
    """The D28 variables docker compose gives ``service``: env_file first, then
    ``environment`` (wins), with ${VAR:-default} read from the project .env.
    Infrastructure URLs (container host names) are not taken over."""
    doc = yaml.safe_load((BACKEND_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = doc["services"][service]
    effective = dict(dotenv) if with_env_file and ".env" in (svc.get("env_file") or []) else {}
    for item in svc.get("environment") or []:
        key, value = item.split("=", 1)
        effective[key] = re.sub(
            r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}",
            lambda m: dotenv.get(m.group(1)) or (m.group(2) or ""),
            value,
        )
    return {key: effective[key] for key in D28_VARIABLES if key in effective}


def _dotenv():
    return {
        "SECRET_KEY": secrets.token_hex(32),
        "JWT_SECRET": secrets.token_hex(16),
        "SETTINGS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "RELAY_SENDER_SECRET": secrets.token_urlsafe(32),
    }


class Proc:
    """A child process whose combined output is collected by a thread."""

    def __init__(self, cmd, env):
        self.proc = subprocess.Popen(cmd, cwd=str(BACKEND_ROOT), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.lines = []
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)

    def wait_for(self, needle, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if needle in line:
                    return line
            if self.proc.poll() is not None and not self._thread.is_alive():
                break
            time.sleep(0.1)
        tail = "".join(self.lines[-5:])
        raise AssertionError(
            f"{needle!r} not seen within {timeout}s (exit code {self.proc.poll()}). Last output:\n{tail}\n"
            "(a relay that exits at once on Python 3.12 needs TRUELIVE_RELAY_PYTHON, see the module docstring)"
        )

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=15)
        self._thread.join(timeout=5)

    @property
    def text(self):
        return "".join(self.lines)


def _startup_fields(line):
    match = _STARTUP_RE.search(line)
    assert match, "startup_check line has an unexpected shape"
    return {"service": match.group(1), "sender": match.group(2), "key": match.group(3), "key_fp": match.group(4)}


def _run_python(code, env, timeout=120):
    result = subprocess.run([sys.executable, "-c", code], cwd=str(BACKEND_ROOT), env=env,
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


_ENCRYPT = """
import json, sys
from app.database import SessionLocal
from app.models.system_setting import SystemSetting
from app.services.system_settings_service import system_settings_service as s
from app.core.startup_check import collect
db = SessionLocal()
if not db.query(SystemSetting).filter(SystemSetting.key == "core.it_address").first():
    db.add(SystemSetting(key="core.it_address", value="", category="core", is_encrypted=True, data_type="string"))
    db.commit()
s.update_setting(db, "core.it_address", sys.argv[1] if len(sys.argv) > 1 else %r, None)
r = collect("api")
print(json.dumps({"key_status": s.key_status, "fp": r.settings_encryption_key_fp, "sender": r.relay_sender_secret}))
"""

_DECRYPT = """
import json, hashlib
from app.database import SessionLocal
from app.services.system_settings_service import system_settings_service as s
from app.core.startup_check import collect
value = s.get_setting(SessionLocal(), "core.it_address", use_cache=False)
r = collect("websocket")
print(json.dumps({"key_status": s.key_status, "fp": r.settings_encryption_key_fp, "sender": r.relay_sender_secret,
                  "match": value == %r}))
"""


# --------------------------------------------------------------------------- #


def test_relay_starts_with_the_broker_down():
    dotenv = _dotenv()
    port = _free_port()
    env = _base_env(WEBSOCKET_PORT=str(port), **_compose_env("websocket", dotenv))
    relay = Proc([RELAY_PYTHON, "-u", "-m", "app.services.websocket_server"], env)
    try:
        line = relay.wait_for("startup_check service=websocket", timeout=60)
        fields = _startup_fields(line)
        assert fields["key"] == "ok" and fields["key_fp"] == fingerprint(dotenv["SETTINGS_ENCRYPTION_KEY"])
        assert fields["sender"] == fingerprint(dotenv["RELAY_SENDER_SECRET"])
        assert "broker=unreachable" in line
        relay.wait_for("Celery broker unreachable", timeout=10)
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline and status != 200:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/connected-pcs", timeout=2) as response:
                    status = response.status
                    body = json.loads(response.read())
            except OSError:
                time.sleep(0.2)
        assert status == 200 and body["total_connected"] == 0
    finally:
        relay.stop()
    for secret_value in dotenv.values():
        assert secret_value not in relay.text


def test_api_to_relay_key_parity(scratch_db):
    dotenv = _dotenv()
    api_env = _base_env(DATABASE_URL=scratch_db, **_compose_env("backend", dotenv))
    relay_env = _base_env(DATABASE_URL=scratch_db, **_compose_env("websocket", dotenv))
    assert api_env["SETTINGS_ENCRYPTION_KEY"] == relay_env["SETTINGS_ENCRYPTION_KEY"]

    api = _run_python(_ENCRYPT % PLAINTEXT, api_env)
    relay = _run_python(_DECRYPT % PLAINTEXT, relay_env)
    assert api["key_status"] == relay["key_status"] == "ok"
    assert api["fp"] == relay["fp"] == fingerprint(dotenv["SETTINGS_ENCRYPTION_KEY"])
    assert api["sender"] == relay["sender"]
    assert relay["match"] is True

    # The stored value is ciphertext, not the plaintext.
    with psycopg2.connect(scratch_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM system_settings WHERE key = 'core.it_address'")
        stored = cur.fetchone()[0]
    assert stored and PLAINTEXT not in stored

    # The relay as wired before D28 (no env_file): no key, cannot decrypt.
    legacy_env = _base_env(DATABASE_URL=scratch_db, **_compose_env("websocket", dotenv, with_env_file=False))
    assert "SETTINGS_ENCRYPTION_KEY" not in legacy_env
    legacy = _run_python(_DECRYPT % PLAINTEXT, legacy_env)
    assert legacy["key_status"] == "no_key" and legacy["match"] is False


def test_fingerprints_match_in_all_four_services(tmp_path):
    dotenv = _dotenv()
    lines = {}
    procs = []
    try:
        port = _free_port()
        relay = Proc([RELAY_PYTHON, "-u", "-m", "app.services.websocket_server"],
                     _base_env(WEBSOCKET_PORT=str(port), **_compose_env("websocket", dotenv)))
        procs.append(relay)
        worker = Proc([sys.executable, "-m", "celery", "-A", "app.tasks.celery_app", "worker", "--pool=solo",
                       "-l", "INFO", "--without-mingle", "--without-gossip", "--without-heartbeat"],
                      _base_env(**_compose_env("celery_worker", dotenv)))
        procs.append(worker)
        beat = Proc([sys.executable, "-m", "celery", "-A", "app.tasks.celery_app", "beat", "-l", "INFO",
                     "-s", str(tmp_path / "beat-schedule"), "--pidfile", str(tmp_path / "beat.pid")],
                    _base_env(**_compose_env("celery_beat", dotenv)))
        procs.append(beat)
        for name, proc in (("websocket", relay), ("celery_worker", worker), ("celery_beat", beat)):
            lines[name] = proc.wait_for(f"startup_check service={name}", timeout=90)
    finally:
        for proc in procs:
            proc.stop()
    api = subprocess.run([sys.executable, "-m", "app.core.startup_check", "--service", "api"], cwd=str(BACKEND_ROOT),
                         env=_base_env(**_compose_env("backend", dotenv)), capture_output=True, text=True, timeout=120)
    assert api.returncode == 0, api.stderr[-2000:]
    api_report = json.loads(api.stdout)

    expected_key_fp = fingerprint(dotenv["SETTINGS_ENCRYPTION_KEY"])
    expected_sender = fingerprint(dotenv["RELAY_SENDER_SECRET"])
    api_fields = (api_report["settings_encryption_key_fp"], api_report["relay_sender_secret"])
    assert api_fields == (expected_key_fp, expected_sender)
    for name, line in lines.items():
        fields = _startup_fields(line)
        assert (fields["key"], fields["key_fp"], fields["sender"]) == ("ok", expected_key_fp, expected_sender), name
    for proc in procs:
        for secret_value in dotenv.values():
            assert secret_value not in proc.text


def test_missing_key_refuses_encrypted_core_write_on_postgres(scratch_db, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.api.deps import get_super_admin_user
    from app.api.v1 import settings as settings_api
    from app.database import get_db
    from app.main import app
    from app.models.system_setting import SystemSetting
    from app.models.user import User
    from app.services import system_settings_service as sss

    engine = create_engine(scratch_db)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()
    admin = User(user_id=uuid.uuid4(), username=f"d28-admin-{secrets.token_hex(3)}", full_name="D28 Fixture Admin",
                 email=f"d28-{secrets.token_hex(3)}@example.com", password_hash="!unusable", role="super_admin",
                 is_active=True)
    db.add(admin)
    db.add(SystemSetting(key="core.pg_address", value="", category="core", is_encrypted=True, data_type="string"))
    db.add(SystemSetting(key="core.pg_plain", value="0", category="core", is_encrypted=False, data_type="integer"))
    db.commit()

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    def _stored(key):
        with Session() as session:
            return session.query(SystemSetting).filter(SystemSetting.key == key).one().value

    def _service(key):
        if key:
            monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
        else:
            monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
        monkeypatch.setattr(sss.app_settings, "SETTINGS_ENCRYPTION_KEY", None)
        return sss.SystemSettingsService()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_super_admin_user] = lambda: admin
    try:
        client = TestClient(app)
        monkeypatch.setattr(settings_api, "system_settings_service", _service(None))
        response = client.put("/api/v1/settings/core.pg_address", json={"value": PLAINTEXT})
        assert response.status_code == 503 and PLAINTEXT not in response.text
        response = client.put("/api/v1/settings/bulk",
                              json={"settings": {"core.pg_plain": "15", "core.pg_address": PLAINTEXT}})
        assert response.status_code == 503
        assert _stored("core.pg_address") == "" and _stored("core.pg_plain") == "0"

        key = Fernet.generate_key().decode()
        monkeypatch.setattr(settings_api, "system_settings_service", _service(key))
        response = client.put("/api/v1/settings/core.pg_address", json={"value": PLAINTEXT})
        assert response.status_code == 200
        assert response.json()["value"] == "********"
        stored = _stored("core.pg_address")
        assert stored and PLAINTEXT not in stored
        db.expire_all()
        assert _service(key).get_setting(db, "core.pg_address", use_cache=False) == PLAINTEXT
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()
