"""
Startup self-check of the shared backend environment (plan 06, D28 test
``startup_check``).

    python -m pytest tests/unit/test_startup_check.py -o addopts='' -p no:cacheprovider
"""

import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core import startup_check as sc

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _cfg(**overrides):
    values = dict(
        APP_VERSION="1.0.0",
        DEPLOY_INJECT_CONFIG_ID=True,
        RELAY_STATION_REPORTS=True,
        DEPLOY_CATCHUP_ENABLED=False,
        DEPLOY_ACK_TIMEOUT_SEC=300,
        RELAY_DEBUG_LOGGING=False,
        RELAY_REQUIRE_CURRENT_TOKEN=False,
        RELAY_SENDER_SECRET="s" * 43,
        SETTINGS_ENCRYPTION_KEY=None,
        JWT_SECRET="j" * 32,
        CELERY_BROKER_URL="redis://127.0.0.1:1/0",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeRedis:
    """Answers +OK to every RESP command and +PONG to PING, on 127.0.0.1:0."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.commands = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.server.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                buffer = b""
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while buffer.startswith(b"*"):
                        parts = buffer.split(b"\r\n")
                        count = int(parts[0][1:])
                        needed = 1 + 2 * count
                        if len(parts) <= needed:
                            break
                        words = [parts[2 + 2 * i] for i in range(count)]
                        consumed = sum(len(p) + 2 for p in parts[:needed])
                        buffer = buffer[consumed:]
                        self.commands.append(words[0].upper())
                        conn.sendall(b"+PONG\r\n" if words[0].upper() == b"PING" else b"+OK\r\n")

    def close(self):
        self._stop = True
        self.thread.join(timeout=5)
        self.server.close()


# --------------------------------------------------------------------------- #
# Pieces
# --------------------------------------------------------------------------- #


def test_fingerprint_is_a_short_sha256_and_never_the_value():
    assert sc.fingerprint("") == "unset"
    assert sc.fingerprint(None) == "unset"
    value = "fixture-sender-secret"
    assert sc.fingerprint(value) == "fp:" + hashlib.sha256(value.encode()).hexdigest()[:12]
    assert value not in sc.fingerprint(value)


def test_settings_key_status():
    assert sc.settings_key_status(None) == sc.KEY_MISSING
    assert sc.settings_key_status("not-a-fernet-key") == sc.KEY_INVALID
    assert sc.settings_key_status(Fernet.generate_key().decode()) == sc.KEY_OK


def test_key_resolution_prefers_the_environment(monkeypatch):
    env_key, cfg_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    assert sc.resolve_settings_encryption_key(_cfg(SETTINGS_ENCRYPTION_KEY=cfg_key)) == cfg_key
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", env_key)
    assert sc.resolve_settings_encryption_key(_cfg(SETTINGS_ENCRYPTION_KEY=cfg_key)) == env_key
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "   ")
    assert sc.resolve_settings_encryption_key(_cfg()) is None


def test_g1_detects_the_client_signing_constant(monkeypatch):
    constant = "fixture-client-constant-0123456789"
    digest = hashlib.sha256(sc._G1_DOMAIN + constant.encode()).hexdigest()
    monkeypatch.setattr(sc, "CLIENT_JWT_CONSTANT_DIGESTS", (digest,))
    assert sc.jwt_secret_is_client_constant(constant) is True
    assert sc.jwt_secret_is_client_constant(constant + "x") is False
    assert sc.jwt_secret_is_client_constant("") is False
    report = sc.collect("websocket", cfg=_cfg(JWT_SECRET=constant))
    assert report.jwt_secret_is_client_constant is True
    assert "jwt_secret" in report.problems


def test_shipped_g1_digests_are_sha256_hex():
    assert sc.CLIENT_JWT_CONSTANT_DIGESTS
    for digest in sc.CLIENT_JWT_CONSTANT_DIGESTS:
        assert len(digest) == 64 and int(digest, 16) >= 0


def test_broker_endpoint_strips_userinfo():
    assert sc.broker_endpoint("redis://:pw-fixture@redis:6379/0") == "redis:6379"
    assert sc.broker_endpoint("redis://user:pw-fixture@127.0.0.1/1") == "127.0.0.1:6379"


def test_broker_unreachable_detail_has_no_password():
    status, detail = sc.check_broker(f"redis://:pw-fixture@127.0.0.1:{_closed_port()}/0", timeout=0.5)
    assert status == "unreachable"
    assert "pw-fixture" not in detail and "127.0.0.1:" in detail


def test_broker_reachable_with_a_fake_redis():
    fake = FakeRedis()
    try:
        status, detail = sc.check_broker(f"redis://:pw-fixture@127.0.0.1:{fake.port}/0", timeout=2)
    finally:
        fake.close()
    assert (status, detail) == ("reachable", f"127.0.0.1:{fake.port}")
    assert b"PING" in fake.commands and b"AUTH" in fake.commands


# --------------------------------------------------------------------------- #
# The report and its log
# --------------------------------------------------------------------------- #


def test_report_carries_flags_and_fingerprints():
    key = Fernet.generate_key().decode()
    cfg = _cfg(
        SETTINGS_ENCRYPTION_KEY=key,
        RELAY_STATION_REPORTS=False,
        DEPLOY_INJECT_CONFIG_ID=False,
        DEPLOY_CATCHUP_ENABLED=True,
        DEPLOY_ACK_TIMEOUT_SEC=120,
        RELAY_DEBUG_LOGGING=True,
        RELAY_REQUIRE_CURRENT_TOKEN=True,
    )
    report = sc.collect("api", cfg=cfg)
    assert report.flags == {
        "deploy_inject_config_id": False,
        "relay_station_reports": "off",
        "deploy_catchup_enabled": True,
        "deploy_ack_timeout_sec": 120,
        "relay_debug_logging": True,
        "relay_require_current_token": True,
    }
    assert report.relay_sender_secret == sc.fingerprint(cfg.RELAY_SENDER_SECRET)
    assert report.settings_encryption_key == "ok"
    assert report.settings_encryption_key_fp == sc.fingerprint(key)
    assert report.broker == "not_checked"
    assert report.problems == {}
    line = report.log_line()
    for part in (
        "startup_check service=api",
        "deploy_inject_config_id=false",
        "relay_station_reports=off",
        "deploy_catchup_enabled=true",
        "deploy_ack_timeout_sec=120",
        "relay_debug_logging=true",
        "relay_require_current_token=true",
        f"relay_sender_secret={sc.fingerprint(cfg.RELAY_SENDER_SECRET)}",
        f"settings_encryption_key=ok {sc.fingerprint(key)}",
        "jwt_secret_is_client_constant=false",
        "broker=not_checked",
    ):
        assert part in line
    assert key not in line and cfg.RELAY_SENDER_SECRET not in line


def test_same_env_gives_the_same_fingerprints_in_every_service():
    cfg = _cfg(SETTINGS_ENCRYPTION_KEY=Fernet.generate_key().decode())
    reports = [sc.collect(service, cfg=cfg) for service in sc.SERVICES]
    assert len({(r.relay_sender_secret, r.settings_encryption_key_fp) for r in reports}) == 1


def test_problems_are_logged_with_their_severity(caplog):
    caplog.set_level(logging.INFO, logger="app.startup_check")
    sender = "short-sender-fixture"
    cfg = _cfg(RELAY_SENDER_SECRET=sender, JWT_SECRET="fixture-jwt-secret-value")
    report = sc.run_startup_check("websocket", cfg=cfg, check_broker_reachable=True)
    assert report.settings_encryption_key == "no_key"
    assert report.broker == "unreachable"
    assert any(r.levelno == logging.INFO and r.getMessage().startswith("startup_check service=websocket")
               for r in caplog.records)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("SETTINGS_ENCRYPTION_KEY is missing" in m for m in errors)
    assert any("RELAY_SENDER_SECRET is shorter than 32" in m for m in warnings)
    assert any("Celery broker unreachable" in m for m in warnings)
    text = caplog.text
    assert sender not in text and "fixture-jwt-secret-value" not in text


def test_unset_sender_secret_is_a_warning_not_an_error(caplog):
    caplog.set_level(logging.INFO, logger="app.startup_check")
    key = Fernet.generate_key().decode()
    sc.run_startup_check("api", cfg=_cfg(RELAY_SENDER_SECRET="", SETTINGS_ENCRYPTION_KEY=key))
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.WARNING, "startup_check service=api: RELAY_SENDER_SECRET is unset: the relay accepts "
            "unauthenticated senders (legacy-open, D12)") in messages
    assert not [m for level, m in messages if level >= logging.ERROR]


def test_invalid_key_is_an_error(caplog):
    caplog.set_level(logging.INFO, logger="app.startup_check")
    report = sc.run_startup_check("celery_beat", cfg=_cfg(SETTINGS_ENCRYPTION_KEY="not-a-fernet-key"))
    assert report.settings_encryption_key == "invalid_key"
    assert any(r.levelno == logging.ERROR and "not a valid Fernet key" in r.getMessage() for r in caplog.records)
    assert "not-a-fernet-key" not in caplog.text


def test_the_check_never_raises(caplog):
    caplog.set_level(logging.INFO, logger="app.startup_check")
    report = sc.run_startup_check("api", cfg=SimpleNamespace(APP_VERSION="1"))
    assert "startup_check" in report.problems
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_celery_signal_handlers_run_the_check(monkeypatch):
    from app.tasks import celery_app

    seen = []
    monkeypatch.setattr(sc, "run_startup_check", lambda service, **kw: seen.append(service))
    celery_app._worker_startup_check(sender=None)
    celery_app._beat_startup_check(sender=None)
    assert seen == ["celery_worker", "celery_beat"]


def test_celery_imports_pc_maintenance_and_its_task_reports_no_values(monkeypatch):
    from app.tasks.celery_app import celery_app

    assert "app.tasks.pc_maintenance.env_self_check" in celery_app.tasks
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    result = celery_app.tasks["app.tasks.pc_maintenance.env_self_check"].apply().get(timeout=10)
    assert result["service"] == "celery_worker"
    assert result["settings_encryption_key"] == "ok"
    assert result["settings_encryption_key_fp"] == sc.fingerprint(key)
    assert key not in json.dumps(result)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli(env_overrides, *args):
    env = dict(os.environ)
    env.pop("SETTINGS_ENCRYPTION_KEY", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "app.core.startup_check", *args],
        cwd=str(BACKEND_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def test_cli_prints_json_without_values_and_strict_fails_without_key():
    sender = "fixture-sender-" + "x" * 32
    result = _cli({"RELAY_SENDER_SECRET": sender}, "--service", "api", "--strict")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["settings_encryption_key"] == "no_key"
    assert report["relay_sender_secret"] == sc.fingerprint(sender)
    assert sender not in result.stdout + result.stderr

    key = Fernet.generate_key().decode()
    result = _cli({"RELAY_SENDER_SECRET": sender, "SETTINGS_ENCRYPTION_KEY": key}, "--service", "websocket", "--strict")
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    assert report["settings_encryption_key_fp"] == sc.fingerprint(key)
    assert key not in result.stdout + result.stderr
