"""
SETTINGS_ENCRYPTION_KEY provisioning and checks (plan 06 C18, merged into D28).

Covers test_missing_key_refuses_encrypted_core_write (service and API) and the
unit half of test_api_to_relay_key_parity. The Postgres and real-process
halves are in tests/integration/test_env_wiring.py.

    python -m pytest tests/unit/test_settings_encryption_key.py -o addopts='' -p no:cacheprovider

Only the system_settings table is created, on SQLite (plan 6.1). Its
``updated_by`` column is a PostgreSQL UUID, so the test copy stores it as text;
the write paths here pass ``user_id=None`` (the API's success path, which sets
it, runs on Postgres in the integration test).
"""

import base64
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, String, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every mapper the ORM needs)
from app.models.system_setting import SystemSetting
from app.services import system_settings_service as sss

PLAINTEXT = "wss://203.0.113.20/ws/truelive/{pc_id}"


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    table = SystemSetting.__table__.to_metadata(MetaData())
    table.c.updated_by.type = String(36)
    for constraint in list(table.foreign_key_constraints):
        table.constraints.discard(constraint)
    table.create(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    db.add_all(
        [
            SystemSetting(key="core.logs_server_address", value="", category="core", is_encrypted=True,
                          data_type="string"),
            SystemSetting(key="core.heartbeat_interval_sec", value="0", category="core", is_encrypted=False,
                          data_type="integer"),
            SystemSetting(key="smtp.password", value="", category="smtp", is_encrypted=True, data_type="string"),
            SystemSetting(key="smtp.host", value="127.0.0.1", category="smtp", is_encrypted=False,
                          data_type="string"),
        ]
    )
    db.commit()
    yield db
    db.close()
    engine.dispose()


def _service(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    monkeypatch.setattr(sss.app_settings, "SETTINGS_ENCRYPTION_KEY", None)
    return sss.SystemSettingsService()


def _value(db, key):
    db.expire_all()
    return db.query(SystemSetting).filter(SystemSetting.key == key).one().value


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


def test_key_status(monkeypatch, caplog):
    assert _service(monkeypatch, Fernet.generate_key().decode()).key_status == "ok"
    with caplog.at_level("ERROR"):
        service = _service(monkeypatch, None)
    assert service.key_status == sss.KEY_STATUS_NO_KEY == "no_key"
    assert not service.has_shared_key
    assert any("No SETTINGS_ENCRYPTION_KEY" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert _service(monkeypatch, "   ").key_status == "no_key"


def test_key_from_settings_when_not_in_the_environment(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(sss.app_settings, "SETTINGS_ENCRYPTION_KEY", key)
    service = sss.SystemSettingsService()
    assert service.key_status == "ok"
    assert Fernet(key.encode()).decrypt(base64.b64decode(service._encrypt_value("x"))) == b"x"


def test_invalid_key_still_fails_fast(monkeypatch):
    with pytest.raises(ValueError):
        _service(monkeypatch, "not-a-fernet-key")


def test_missing_key_refuses_encrypted_core_write(monkeypatch, session):
    service = _service(monkeypatch, None)
    with pytest.raises(sss.EncryptionKeyUnavailable) as excinfo:
        service.update_setting(session, "core.logs_server_address", PLAINTEXT, None)
    assert excinfo.value.keys == ["core.logs_server_address"]
    assert PLAINTEXT not in str(excinfo.value)
    assert _value(session, "core.logs_server_address") == ""


def test_missing_key_refuses_the_whole_bulk_before_any_write(monkeypatch, session):
    service = _service(monkeypatch, None)
    batch = {"smtp.host": "198.51.100.25", "core.logs_server_address": PLAINTEXT, "core.heartbeat_interval_sec": "15"}
    with pytest.raises(sss.EncryptionKeyUnavailable):
        service.update_settings_bulk(session, batch, None)
    assert _value(session, "smtp.host") == "127.0.0.1"
    assert _value(session, "core.heartbeat_interval_sec") == "0"
    assert _value(session, "core.logs_server_address") == ""


def test_missing_key_keeps_other_writes_working(monkeypatch, session):
    """Only encrypted shared (core.*) keys need the shared key; the rest keep
    today's behaviour (smtp.password still uses the per-process key, NEW-47)."""
    service = _service(monkeypatch, None)
    service.update_setting(session, "core.heartbeat_interval_sec", "15", None)
    service.update_setting(session, "smtp.host", "198.51.100.25", None)
    service.update_setting(session, "smtp.password", "fixture-smtp-password", None)
    assert _value(session, "core.heartbeat_interval_sec") == "15"
    assert _value(session, "smtp.host") == "198.51.100.25"
    assert _value(session, "smtp.password") not in ("", "fixture-smtp-password")


def test_with_key_core_write_is_encrypted_and_readable_by_another_process(monkeypatch, session):
    key = Fernet.generate_key().decode()
    api_side = _service(monkeypatch, key)
    api_side.update_setting(session, "core.logs_server_address", PLAINTEXT, None)
    stored = _value(session, "core.logs_server_address")
    assert stored and stored != PLAINTEXT
    # A second instance built from the same env, like the relay process (C10 reads core.*).
    relay_side = _service(monkeypatch, key)
    assert relay_side.get_setting(session, "core.logs_server_address", use_cache=False) == PLAINTEXT
    # A process without the key cannot read it: the failure mode D28 removes.
    no_key = _service(monkeypatch, None)
    assert no_key.get_setting(session, "core.logs_server_address", use_cache=False) != PLAINTEXT


def test_ensure_writable_lists_only_blocked_keys(monkeypatch, session):
    service = _service(monkeypatch, None)
    service.ensure_writable(session, ["smtp.password", "core.heartbeat_interval_sec", "unknown.key"])
    with pytest.raises(sss.EncryptionKeyUnavailable) as excinfo:
        service.ensure_writable(session, ["smtp.password", "core.logs_server_address"])
    assert excinfo.value.keys == ["core.logs_server_address"]
    _service(monkeypatch, Fernet.generate_key().decode()).ensure_writable(session, ["core.logs_server_address"])


# --------------------------------------------------------------------------- #
# API: 503
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(session):
    from app.api.deps import get_super_admin_user
    from app.database import get_db
    from app.main import app

    def _db():
        yield session

    admin = SimpleNamespace(user_id=uuid.uuid4(), username="fixture-admin", role="super_admin", is_active=True)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_super_admin_user] = lambda: admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_api_put_encrypted_core_key_without_key_is_503(monkeypatch, session, client):
    from app.api.v1 import settings as settings_api

    monkeypatch.setattr(settings_api, "system_settings_service", _service(monkeypatch, None))
    response = client.put("/api/v1/settings/core.logs_server_address", json={"value": PLAINTEXT})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "SETTINGS_ENCRYPTION_KEY" in detail and "core.logs_server_address" in detail
    assert PLAINTEXT not in response.text
    assert _value(session, "core.logs_server_address") == ""


def test_api_bulk_with_encrypted_core_key_without_key_is_503_and_writes_nothing(monkeypatch, session, client):
    from app.api.v1 import settings as settings_api

    monkeypatch.setattr(settings_api, "system_settings_service", _service(monkeypatch, None))
    response = client.put(
        "/api/v1/settings/bulk",
        json={"settings": {"smtp.host": "198.51.100.25", "core.logs_server_address": PLAINTEXT}},
    )
    assert response.status_code == 503
    assert PLAINTEXT not in response.text
    assert _value(session, "smtp.host") == "127.0.0.1"
    assert _value(session, "core.logs_server_address") == ""


def test_api_unknown_key_is_still_404(monkeypatch, session, client):
    from app.api.v1 import settings as settings_api

    monkeypatch.setattr(settings_api, "system_settings_service", _service(monkeypatch, None))
    response = client.put("/api/v1/settings/core.no_such_key", json={"value": "x"})
    assert response.status_code == 404
