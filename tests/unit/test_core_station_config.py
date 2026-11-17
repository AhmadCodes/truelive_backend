"""
Fleet Core settings (category ``core``) and the relay call get_station_config
(plan 06 C10; OD-26, OD-4, NEW-48, NEW-53, X-17).

Card tests (all run with ``-o addopts=''``; only the tables they need are
created on SQLite: system_settings, plus pcs and pc_token_renewals for the
relay through tests/relay_testkit.py):

* test_seed_core_idempotent_and_lists_equal: the one seed list (the script
  imports the seeder's), the core keys, no websocket.* seeds, idempotent.
* test_empty_values_are_strings: unset values come back as '' / 0 / true,
  never None.
* test_strict_decrypt_and_revision: strict decrypt (never ciphertext), the
  errors list (missing, decrypt_failed, no_key, invalid_key, invalid_value)
  and the revision (sha256 of the values; re-encryption does not change it).
* test_key_parity: seed list, loader and validators share one key set; what
  the API's service encrypts, a relay-side process decryptor reads.
* test_relay_get_station_config: only the PC's current session is served.
* test_get_station_config_cached: 200 calls within 1 s cost one DB read,
  concurrent calls share one read, TTL and failure caching.
* test_masking_and_validation: the settings API shows '' and none unmasked
  for core keys only, masks set values, never returns ciphertext, never
  mutates the rows, and validates core writes (422, nothing written).

The Postgres and real-process half is tests/integration/test_station_config_pg.py.

    python -m pytest tests/unit/test_core_station_config.py -o addopts='' -p no:cacheprovider
"""

import base64
import hashlib
import importlib.util
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, String, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every mapper the ORM needs)
from app.models.system_setting import SystemSetting
from app.schemas import station_protocol as sp
from app.services import core_station_config as csc
from app.services import settings_seeder
from app.services import system_settings_service as sss
from tests.relay_testkit import RelayHarness, import_relay, make_pc_token

relay = import_relay()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
# Core's config.json keys these fleet keys feed (StreamSwitcher Config.hpp).
CORE_ADDRESS_KEYS = {"notification_address", "logs_server_address", "heart_beat_server_address", "ack_address"}
SECRET_QUERY = "fixture-secret-" + "q" * 12
SET_ADDRESS = f"wss://203.0.113.40/pulse/{{pc_id}}?name={{pc_name_urlenc}}&key={SECRET_QUERY}"
ENCRYPTED = {csc.KEY_NOTIFICATION_ADDRESS, csc.KEY_LOGS_SERVER_ADDRESS, csc.KEY_HEART_BEAT_SERVER_ADDRESS,
             csc.KEY_ACK_ADDRESS, csc.KEY_CLIENT_ADMIN_PASSWORD_HASH}
UNSET = {
    csc.KEY_CLIENT_MANAGED_CONFIG: True,
    csc.KEY_NOTIFICATION_ADDRESS: "",
    csc.KEY_LOGS_SERVER_ADDRESS: "",
    csc.KEY_HEART_BEAT_SERVER_ADDRESS: "",
    csc.KEY_ACK_ADDRESS: "",
    csc.KEY_HEARTBEAT_INTERVAL_SEC: 0,
    csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: "",
}


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _create_settings_table(engine):
    """system_settings alone on SQLite: ``updated_by`` (a PostgreSQL UUID) as
    text and no foreign key to users."""
    table = SystemSetting.__table__.to_metadata(MetaData())
    table.c.updated_by.type = String(36)
    for constraint in list(table.foreign_key_constraints):
        table.constraints.discard(constraint)
    table.create(engine)


def _uuid_shim(session):
    """The API stores ``str(user_id)`` in the UUID column; Postgres accepts the
    string, SQLite's UUID binding needs a uuid.UUID (test-only shim)."""

    @event.listens_for(session, "before_flush")
    def _to_uuid(sess, _ctx, _instances):
        for obj in list(sess.dirty) + list(sess.new):
            if isinstance(obj, SystemSetting) and isinstance(obj.updated_by, str):
                obj.updated_by = uuid.UUID(obj.updated_by)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    _create_settings_table(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    session = sessionmaker(bind=engine, autoflush=False)()
    _uuid_shim(session)
    yield session
    session.close()


def _service(monkeypatch, key):
    """A SystemSettingsService built from ``key`` (None: no key) the way every
    process builds it (environment first)."""
    if key is None:
        monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    monkeypatch.setattr(sss.app_settings, "SETTINGS_ENCRYPTION_KEY", None)
    return sss.SystemSettingsService()


def _rows(db):
    db.expire_all()
    return {row.key: row for row in db.query(SystemSetting).all()}


def _stored(db, key):
    return _rows(db)[key].value


def _set(db, service, values):
    service.update_settings_bulk(db, values, None)


def _load(db, service):
    return csc.load_station_config(db, key_status=service.key_status,
                                   decrypt=service.decrypt_strict if service.key_status == "ok" else None)


def _expected_revision(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _load_script():
    path = BACKEND_ROOT / "scripts" / "seed_settings.py"
    spec = importlib.util.spec_from_file_location("seed_settings_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _admin_hash(password="fixture-admin-password", salt="fixtureSaltValue22chrs"):
    return csc.make_admin_password_hash(password, salt=salt)


# --------------------------------------------------------------------------- #
# Seeds
# --------------------------------------------------------------------------- #


def test_seed_core_idempotent_and_lists_equal(db):
    script = _load_script()
    # One list: the script seeds exactly what the API seeds at startup.
    assert script.DEFAULT_SETTINGS is settings_seeder.DEFAULT_SETTINGS
    assert script.seed_settings is settings_seeder.seed_settings
    assert [s["key"] for s in script.DEFAULT_SETTINGS] == [s["key"] for s in settings_seeder.DEFAULT_SETTINGS]
    keys = [s["key"] for s in settings_seeder.DEFAULT_SETTINGS]
    assert len(keys) == len(set(keys))
    # NEW-53: the dead websocket.* seeds are gone; nothing reads them.
    assert not [k for k in keys if k.startswith("websocket.")]
    assert "websocket" not in {s["category"] for s in settings_seeder.DEFAULT_SETTINGS}
    core = [s for s in settings_seeder.DEFAULT_SETTINGS if s["category"] == "core"]
    assert [s["key"] for s in core] == list(csc.KEYS)
    assert core == csc.seed_entries()
    assert {s["key"]: s["value"] for s in core} == {
        csc.KEY_CLIENT_MANAGED_CONFIG: "true", csc.KEY_NOTIFICATION_ADDRESS: "", csc.KEY_LOGS_SERVER_ADDRESS: "",
        csc.KEY_HEART_BEAT_SERVER_ADDRESS: "", csc.KEY_ACK_ADDRESS: "", csc.KEY_HEARTBEAT_INTERVAL_SEC: "0",
        csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: "",
    }
    assert {s["key"] for s in core if s["is_encrypted"]} == ENCRYPTED
    # Every core key starts with the prefix whose encrypted writes need the shared key (C18).
    assert all(k.startswith(sss.SHARED_ENCRYPTED_PREFIXES) for k in csc.KEYS)

    first = settings_seeder.seed_settings(db)
    assert first == {"created": len(keys), "skipped": 0, "updated": 0, "total": len(keys)}
    snapshot = {k: (r.value, r.category, r.is_encrypted, r.data_type, r.description) for k, r in _rows(db).items()}
    second = settings_seeder.seed_settings(db)
    assert second == {"created": 0, "skipped": len(keys), "updated": 0, "total": len(keys)}
    third = script.seed_settings(db)
    assert third["created"] == 0
    assert {k: (r.value, r.category, r.is_encrypted, r.data_type, r.description)
            for k, r in _rows(db).items()} == snapshot


def test_seed_keeps_existing_rows(db):
    """An existing database (rows from migration 004 or the old seeder, a value
    ops already set) gains only the missing keys; nothing is deleted or reset."""
    db.add_all([
        SystemSetting(key="websocket.port", value="8080", category="websocket", is_encrypted=False,
                      data_type="integer"),
        SystemSetting(key="websocket.ping_interval", value="25", category="websocket", is_encrypted=False,
                      data_type="integer"),
        SystemSetting(key=csc.KEY_HEARTBEAT_INTERVAL_SEC, value="15", category="core", is_encrypted=False,
                      data_type="integer"),
    ])
    db.commit()
    result = settings_seeder.seed_settings(db)
    assert result["skipped"] == 1 and result["created"] == len(settings_seeder.DEFAULT_SETTINGS) - 1
    rows = _rows(db)
    assert rows["websocket.port"].value == "8080" and rows["websocket.ping_interval"].value == "25"
    assert rows[csc.KEY_HEARTBEAT_INTERVAL_SEC].value == "15"
    assert set(csc.KEYS) <= set(rows)


# --------------------------------------------------------------------------- #
# The loader
# --------------------------------------------------------------------------- #


def test_empty_values_are_strings(db, monkeypatch):
    settings_seeder.seed_settings(db)
    service = _service(monkeypatch, Fernet.generate_key().decode())
    result = _load(db, service)
    assert dict(result.values) == UNSET
    assert result.errors == ()
    for key, value in result.values.items():
        assert value is not None, key
        assert type(value) is {"boolean": bool, "integer": int, "string": str}[csc.SPECS[key].data_type], key
    ack = result.ack()
    assert ack == {"status": "ok", "revision": _expected_revision(UNSET), "values": UNSET, "errors": []}
    assert json.loads(json.dumps(ack)) == ack
    # A NULL value reads as '' too.
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_ACK_ADDRESS).update({"value": None})
    db.commit()
    assert _load(db, service).values[csc.KEY_ACK_ADDRESS] == ""
    # ... whatever the decryptor does with it (NULL becomes '' before decrypting).
    assert csc.load_station_config(db, key_status="ok", decrypt=lambda stored: stored).values[
        csc.KEY_ACK_ADDRESS] == ""
    # Every ack is a fresh copy: a caller cannot change the cached result.
    ack["values"][csc.KEY_ACK_ADDRESS] = "changed"
    assert result.ack()["values"][csc.KEY_ACK_ADDRESS] == ""


def test_strict_decrypt_and_revision(db, monkeypatch, caplog):
    settings_seeder.seed_settings(db)
    key = Fernet.generate_key().decode()
    service = _service(monkeypatch, key)

    # The service's strict decrypt never returns the ciphertext.
    token = service._encrypt_value(SET_ADDRESS)
    assert service.decrypt_strict(token) == SET_ADDRESS
    assert service.decrypt_strict("") == "" and service.decrypt_strict(None) == ""
    other = _service(monkeypatch, Fernet.generate_key().decode())
    for bad in (token, "not-base64-%%%", base64.b64encode(b"garbage").decode(), "café"):
        with pytest.raises(sss.SettingDecryptError) as excinfo:
            other.decrypt_strict(bad)
        assert bad not in str(excinfo.value) and SET_ADDRESS not in str(excinfo.value)
        assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__

    values = {
        csc.KEY_CLIENT_MANAGED_CONFIG: "false",
        csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS,
        csc.KEY_LOGS_SERVER_ADDRESS: "none",
        csc.KEY_HEART_BEAT_SERVER_ADDRESS: "ws://127.0.0.1:19999/hb",
        csc.KEY_ACK_ADDRESS: "",
        csc.KEY_HEARTBEAT_INTERVAL_SEC: "15",
        csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: _admin_hash(),
    }
    _set(db, service, values)
    stored = {k: _stored(db, k) for k in ENCRYPTED}
    for k in ENCRYPTED - {csc.KEY_ACK_ADDRESS}:
        assert stored[k] and stored[k] != values[k]  # ciphertext at rest
    expected = {
        csc.KEY_CLIENT_MANAGED_CONFIG: False,
        csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS,
        csc.KEY_LOGS_SERVER_ADDRESS: "none",
        csc.KEY_HEART_BEAT_SERVER_ADDRESS: "ws://127.0.0.1:19999/hb",
        csc.KEY_ACK_ADDRESS: "",
        csc.KEY_HEARTBEAT_INTERVAL_SEC: 15,
        csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: _admin_hash(),
    }
    result = _load(db, service)
    assert (dict(result.values), result.errors) == (expected, ())
    assert result.revision == _expected_revision(expected) == csc.revision(expected)
    assert len(result.revision) == 64 and int(result.revision, 16) >= 0

    # Re-saving the same values re-encrypts them (new ciphertext), same revision.
    _set(db, service, values)
    assert {k: _stored(db, k) for k in ENCRYPTED - {csc.KEY_ACK_ADDRESS}} != {
        k: stored[k] for k in ENCRYPTED - {csc.KEY_ACK_ADDRESS}}
    assert _load(db, service).revision == result.revision
    # Changing one value changes it.
    _set(db, service, {csc.KEY_HEARTBEAT_INTERVAL_SEC: "16"})
    changed = _load(db, service)
    assert changed.revision != result.revision and changed.values[csc.KEY_HEARTBEAT_INTERVAL_SEC] == 16
    _set(db, service, {csc.KEY_HEARTBEAT_INTERVAL_SEC: "15"})
    assert _load(db, service).revision == result.revision

    # Another (valid) key: the set values fail to decrypt and are left out; '' stays ''.
    wrong = _load(db, other)
    assert dict(wrong.errors) == {k: "decrypt_failed" for k in ENCRYPTED - {csc.KEY_ACK_ADDRESS}}
    assert dict(wrong.values) == {csc.KEY_CLIENT_MANAGED_CONFIG: False, csc.KEY_ACK_ADDRESS: "",
                                  csc.KEY_HEARTBEAT_INTERVAL_SEC: 15}
    assert wrong.revision == csc.revision(wrong.values) != result.revision
    # No key, or an invalid one: every encrypted key is left out, even an empty one.
    for status in ("no_key", "invalid_key"):
        result_nk = csc.load_station_config(db, key_status=status, decrypt=None)
        assert dict(result_nk.errors) == {k: status for k in ENCRYPTED}
        assert set(result_nk.values) == {csc.KEY_CLIENT_MANAGED_CONFIG, csc.KEY_HEARTBEAT_INTERVAL_SEC}
    # A service without a key is no_key (C18/D28's key_status).
    assert _load(db, _service(monkeypatch, None)).errors == tuple((k, "no_key") for k in csc.KEYS if k in ENCRYPTED)

    # Missing rows, tampered ciphertext and stored values that break the rules.
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_ACK_ADDRESS).delete()
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_LOGS_SERVER_ADDRESS).update(
        {"value": "tampered" + stored[csc.KEY_LOGS_SERVER_ADDRESS][8:]})
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_HEARTBEAT_INTERVAL_SEC).update({"value": "4"})
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_CLIENT_MANAGED_CONFIG).update({"value": "yes"})
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_HEART_BEAT_SERVER_ADDRESS).update(
        {"value": service._encrypt_value("http://198.51.100.7/hb")})
    db.commit()
    broken = _load(db, service)
    assert dict(broken.errors) == {
        csc.KEY_ACK_ADDRESS: "missing", csc.KEY_LOGS_SERVER_ADDRESS: "decrypt_failed",
        csc.KEY_HEARTBEAT_INTERVAL_SEC: "invalid_value", csc.KEY_CLIENT_MANAGED_CONFIG: "invalid_value",
        csc.KEY_HEART_BEAT_SERVER_ADDRESS: "invalid_value",
    }
    assert dict(broken.values) == {csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS,
                                   csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: _admin_hash()}
    # The errors list follows the key order and names keys and codes only.
    assert [k for k, _ in broken.errors] == [k for k in csc.KEYS if k in dict(broken.errors)]
    text = json.dumps([broken.ack(), wrong.ack()])
    for ciphertext in stored.values():
        if ciphertext:
            assert ciphertext not in text


def test_get_setting_reads_core_keys_strictly_and_uncached(db, monkeypatch, caplog):
    """NEW-48 / NEW-47 remainder: core.* reads bypass the per-process cache and
    never return ciphertext; other keys keep today's behaviour."""
    settings_seeder.seed_settings(db)
    key = Fernet.generate_key().decode()
    service = _service(monkeypatch, key)
    _set(db, service, {csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS, csc.KEY_HEARTBEAT_INTERVAL_SEC: "15"})
    assert service.get_setting(db, csc.KEY_NOTIFICATION_ADDRESS) == SET_ADDRESS
    assert service.get_setting(db, csc.KEY_HEARTBEAT_INTERVAL_SEC) == 15
    # Another process writes: the next read sees it, even with use_cache=True.
    db.query(SystemSetting).filter(SystemSetting.key == csc.KEY_HEARTBEAT_INTERVAL_SEC).update({"value": "30"})
    db.commit()
    assert service.get_setting(db, csc.KEY_HEARTBEAT_INTERVAL_SEC, use_cache=True) == 30
    assert csc.KEY_HEARTBEAT_INTERVAL_SEC not in service._cache
    # A process with another key gets the default, not the ciphertext, and logs the key name only.
    other = _service(monkeypatch, Fernet.generate_key().decode())
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert other.get_setting(db, csc.KEY_NOTIFICATION_ADDRESS, default="fallback") == "fallback"
    assert csc.KEY_NOTIFICATION_ADDRESS in caplog.text
    assert _stored(db, csc.KEY_NOTIFICATION_ADDRESS) not in caplog.text and SET_ADDRESS not in caplog.text
    # A non-core key is still cached (unchanged behaviour).
    db.query(SystemSetting).filter(SystemSetting.key == "smtp.port").update({"value": "25"})
    db.commit()
    assert service.get_setting(db, "smtp.port") == 25
    db.query(SystemSetting).filter(SystemSetting.key == "smtp.port").update({"value": "26"})
    db.commit()
    assert service.get_setting(db, "smtp.port") == 25


# --------------------------------------------------------------------------- #
# Key parity
# --------------------------------------------------------------------------- #


def test_key_parity(db, monkeypatch):
    # One key set: specs, seeds, the loader's output and the validators.
    assert set(csc.SPECS) == set(csc.KEYS) == {s["key"] for s in csc.seed_entries()}
    assert len(csc.KEYS) == 7 and all(k.startswith("core.") for k in csc.KEYS)
    assert {k[len("core."):] for k in csc.ADDRESS_KEYS} == CORE_ADDRESS_KEYS
    assert {k for k, s in csc.SPECS.items() if s.encrypted} == ENCRYPTED
    assert {k: s.data_type for k, s in csc.SPECS.items() if not s.encrypted} == {
        csc.KEY_CLIENT_MANAGED_CONFIG: "boolean", csc.KEY_HEARTBEAT_INTERVAL_SEC: "integer"}
    for spec in csc.CORE_SETTINGS:
        assert spec.description and csc.check_value(spec.key, spec.default) is None, spec.key
    settings_seeder.seed_settings(db)

    # Writer (the API's service) and reader (the relay's process decryptor)
    # share SETTINGS_ENCRYPTION_KEY from the environment (D28).
    key = Fernet.generate_key().decode()
    api_side = _service(monkeypatch, key)
    _set(db, api_side, {csc.KEY_ACK_ADDRESS: SET_ADDRESS, csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: _admin_hash()})
    monkeypatch.setattr(sss, "system_settings_service", _service(monkeypatch, key))
    status, decrypt = csc.process_decryptor()
    assert status == "ok" and decrypt == sss.system_settings_service.decrypt_strict
    relay_side = csc.load_station_config(db, key_status=status, decrypt=decrypt)
    assert relay_side.errors == ()
    assert relay_side.values[csc.KEY_ACK_ADDRESS] == SET_ADDRESS
    assert relay_side.values[csc.KEY_CLIENT_ADMIN_PASSWORD_HASH] == _admin_hash()
    assert set(relay_side.values) == set(csc.KEYS)

    # A relay with another key cannot read them; without a key, nothing encrypted is served.
    monkeypatch.setattr(sss, "system_settings_service", _service(monkeypatch, Fernet.generate_key().decode()))
    status, decrypt = csc.process_decryptor()
    other = csc.load_station_config(db, key_status=status, decrypt=decrypt)
    assert dict(other.errors) == {csc.KEY_ACK_ADDRESS: "decrypt_failed",
                                  csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: "decrypt_failed"}
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    assert csc.process_decryptor() == ("no_key", None)

    # An invalid key never reaches the settings service (it would raise at
    # construction and take the relay down): the relay keeps serving plain keys.
    class _Explodes:
        def __getattr__(self, name):
            raise AssertionError("the settings service must not be used with an invalid key")

    monkeypatch.setattr(sss, "system_settings_service", _Explodes())
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "not-a-fernet-key")
    assert csc.process_decryptor() == ("invalid_key", None)


def test_admin_hash_reference_encoding():
    """The documented encoding (C11 computes it in WebCrypto, the Client
    verifies it): RFC 7914's PBKDF2-HMAC-SHA256 vector (P=passwd, S=salt, c=1),
    first 32 bytes."""
    rfc_7914 = bytes.fromhex("55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc")
    assert csc.make_admin_password_hash("passwd", salt="salt", iterations=1) == (
        "pbkdf2_sha256$1$salt$" + base64.b64encode(rfc_7914).decode())
    encoded = csc.make_admin_password_hash("fixture-pässword")
    algorithm, iterations, salt, digest = encoded.split("$")
    assert (algorithm, iterations, len(salt)) == ("pbkdf2_sha256", "200000", 22)
    assert base64.b64decode(digest) == hashlib.pbkdf2_hmac("sha256", "fixture-pässword".encode(), salt.encode(),
                                                           200000, 32)
    assert csc.check_value(csc.KEY_CLIENT_ADMIN_PASSWORD_HASH, encoded) is None
    assert csc.make_admin_password_hash("x") != csc.make_admin_password_hash("x")  # random salt


# --------------------------------------------------------------------------- #
# Relay handler
# --------------------------------------------------------------------------- #


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "z" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    harness = RelayHarness(relay, monkeypatch)
    _create_settings_table(harness.engine)
    with harness.Session() as session:
        settings_seeder.seed_settings(session)
    harness.service = _service(monkeypatch, Fernet.generate_key().decode())
    monkeypatch.setattr(csc, "process_decryptor", lambda: ("ok", harness.service.decrypt_strict))
    harness.cache = csc.StationConfigCache(ttl_seconds=60)
    monkeypatch.setattr(relay, "station_config_cache", harness.cache)
    relay.enable_feature(sp.FEATURE_STATION_CONFIG)
    harness.selects = []

    def _count(conn, cursor, statement, params, context, executemany):
        # The loader's read: the key/value/is_encrypted columns of system_settings
        # (the settings service's own ORM queries, used to write, select the id too).
        if (statement.lstrip().upper().startswith("SELECT") and "FROM system_settings" in statement
                and "system_settings.id" not in statement):
            harness.selects.append(statement)

    event.listen(harness.engine, "before_cursor_execute", _count)
    yield harness
    event.remove(harness.engine, "before_cursor_execute", _count)
    relay.connected_pcs.clear()
    relay.session_data.clear()


def _set_relay_values(h, values):
    with h.Session() as session:
        h.service.update_settings_bulk(session, values, None)


def _online(h, pc_id="PC_A", sid="pc-a"):
    token = h.pc_row(pc_id) and h.pc_row(pc_id).auth_token or h.add_pc(pc_id)
    h.connect(sid)
    h.register(sid, pc_id, token, capabilities=[sp.FEATURE_DEPLOY_RESULT])
    assert relay._registered_pc(sid) == pc_id, h.errors(sid)


REJECTED = {"status": "rejected", "reason": "not registered", "revision": None, "values": {}, "errors": []}


def test_relay_get_station_config(h, caplog):
    _set_relay_values(h, {csc.KEY_ACK_ADDRESS: SET_ADDRESS, csc.KEY_HEARTBEAT_INTERVAL_SEC: "15"})
    caplog.set_level(logging.DEBUG, logger=relay.logger.name)
    _online(h)
    [registered] = h.received("pc-a", "registered")
    assert sp.FEATURE_STATION_CONFIG in registered["features"]

    # The payload is ignored: another pc_id, odd shapes; the session decides.
    for payload in ({"pc_id": "PC_OTHER", "config_status": {"core_revision": "x"}}, None, "text", [1, 2], 5):
        ack = relay.get_station_config("pc-a", payload)
        assert ack["status"] == "ok" and ack["errors"] == []
        assert ack["values"] == {**UNSET, csc.KEY_ACK_ADDRESS: SET_ADDRESS, csc.KEY_HEARTBEAT_INTERVAL_SEC: 15}
        assert ack["revision"] == csc.revision(ack["values"])
        assert json.loads(json.dumps(ack)) == ack
    assert relay.get_station_config("pc-a") == ack  # no payload at all
    assert "served to PC PC_A" in caplog.text and "PC_OTHER" not in caplog.text

    # Refused: never connected, connected but never registered, a sender, a
    # refused registration (deleted PC), and a stale (replaced) session.
    h.connect("pc-anon")
    h.connect_sender()
    h.connect("pc-gone")
    h.register("pc-gone", "PC_GONE", make_pc_token("PC_GONE", h.secret))
    assert h.errors("pc-gone")
    _online(h, "PC_A", "pc-a2")
    for sid in ("nobody", "pc-anon", "sender-1", "pc-gone", "pc-a"):
        assert relay.get_station_config(sid, {"pc_id": "PC_A"}) == REJECTED, sid
    assert relay.get_station_config("pc-a2", {})["status"] == "ok"
    # A session that still names a pc_id the presence map no longer gives it
    # (D12's _registered_pc: connected_pcs[pc_id] must be this sid).
    relay.session_data["pc-a"]["pc_id"] = "PC_A"
    assert relay.get_station_config("pc-a", {}) == REJECTED
    relay.connected_pcs.pop("PC_A")
    relay.session_data["pc-a2"]["pc_id"] = "PC_A"
    assert relay.get_station_config("pc-a2", {}) == REJECTED
    relay.connected_pcs["PC_A"] = "pc-a2"
    assert relay.relay_counters["station_config_refused"] == 7
    assert relay.relay_counters["station_config_served"] == 7

    # Nothing secret in the relay's log: no plaintext value, no ciphertext.
    with h.Session() as session:
        ciphertext = session.query(SystemSetting).filter(SystemSetting.key == csc.KEY_ACK_ADDRESS).one().value
    assert SECRET_QUERY not in caplog.text and ciphertext not in caplog.text
    assert ciphertext not in json.dumps(ack)


def test_relay_station_config_database_failure(h, caplog):
    clock = [1000.0]
    h.cache = csc.StationConfigCache(ttl_seconds=60, error_ttl_seconds=5, clock=lambda: clock[0])
    h.monkeypatch.setattr(relay, "station_config_cache", h.cache)
    _online(h)
    h.db_fails = True
    with caplog.at_level(logging.ERROR):
        ack = relay.get_station_config("pc-a", {})
    assert ack == {"status": "error", "reason": "unavailable", "revision": None, "values": {}, "errors": []}
    assert "Could not read the station config for PC PC_A: RuntimeError" in caplog.text
    # The failure is remembered for 5 s: no second attempt while the database is down.
    h.db_fails = False
    assert relay.get_station_config("pc-a", {})["status"] == "error"
    assert h.selects == []
    clock[0] += 5.1
    assert relay.get_station_config("pc-a", {})["status"] == "ok"
    assert len(h.selects) == 1
    assert relay.relay_counters["station_config_errors"] == 2


def test_relay_station_config_reports_errors(h, monkeypatch, caplog):
    """A relay without a valid key serves the plain keys and lists the rest."""
    monkeypatch.setattr(csc, "process_decryptor", lambda: ("no_key", None))
    _online(h)
    with caplog.at_level(logging.WARNING):
        ack = relay.get_station_config("pc-a", {})
    assert ack["status"] == "ok"
    assert ack["values"] == {csc.KEY_CLIENT_MANAGED_CONFIG: True, csc.KEY_HEARTBEAT_INTERVAL_SEC: 0}
    assert ack["errors"] == [{"key": k, "error": "no_key"} for k in csc.KEYS if k in ENCRYPTED]
    assert "Station config loaded: revision=" in caplog.text and "core.ack_address:no_key" in caplog.text


def test_the_module_serves_get_station_config():
    """Outside the harness: the handler is registered and its feature enabled
    at import, so ``registered`` advertises station_config.v1 (X-17)."""
    assert "get_station_config" in relay.sio.handlers["/"]
    assert sp.FEATURE_STATION_CONFIG in relay._enabled_features
    assert relay.FEATURE_EVENTS[sp.FEATURE_STATION_CONFIG] == csc.EVENT_GET_STATION_CONFIG == "get_station_config"
    assert relay.STATION_CONFIG_CACHE_SECONDS == 60.0


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def test_get_station_config_cached(h):
    _online(h)
    started = time.perf_counter()
    acks = [relay.get_station_config("pc-a", {}) for _ in range(200)]
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, elapsed
    assert len(h.selects) == 1
    assert all(ack == acks[0] for ack in acks) and acks[0]["status"] == "ok"
    assert relay.relay_counters["station_config_loads"] == 1 and h.cache.loads == 1


def test_concurrent_calls_share_one_read(h, monkeypatch):
    _online(h)
    real = csc.load_station_config

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(csc, "load_station_config", slow)
    barrier = threading.Barrier(50)
    acks = []

    def caller():
        barrier.wait()
        acks.append(relay.get_station_config("pc-a", {}))

    threads = [threading.Thread(target=caller) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(acks) == 50 and all(ack["status"] == "ok" for ack in acks)
    assert len({ack["revision"] for ack in acks}) == 1
    assert len(h.selects) == 1 and h.cache.loads == 1


def test_cache_ttl_and_revision(h):
    clock = [1000.0]
    h.cache = csc.StationConfigCache(ttl_seconds=60, clock=lambda: clock[0])
    h.monkeypatch.setattr(relay, "station_config_cache", h.cache)
    _online(h)
    first = relay.get_station_config("pc-a", {})
    _set_relay_values(h, {csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS})
    clock[0] += 59
    assert relay.get_station_config("pc-a", {}) == first  # within the TTL: the cached answer
    clock[0] += 1.5
    second = relay.get_station_config("pc-a", {})
    assert second["values"][csc.KEY_NOTIFICATION_ADDRESS] == SET_ADDRESS
    assert second["revision"] != first["revision"]
    # The same value saved again (new ciphertext): a reload gives the same revision.
    _set_relay_values(h, {csc.KEY_NOTIFICATION_ADDRESS: SET_ADDRESS})
    clock[0] += 61
    assert relay.get_station_config("pc-a", {})["revision"] == second["revision"]
    assert len(h.selects) == 3


def test_cache_failure_and_follower_timeout():
    clock = [0.0]
    cache = csc.StationConfigCache(ttl_seconds=60, error_ttl_seconds=5, clock=lambda: clock[0])
    calls = []

    def failing():
        calls.append(1)
        raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        cache.get(failing)
    assert cache.get(failing) is None and len(calls) == 1 and cache.failures == 1
    clock[0] += 6
    ok = csc.StationConfig(values={}, errors=(), revision=csc.revision({}))
    assert cache.get(lambda: ok) is ok and cache.loads == 1

    # A follower waits for the load in flight, at most wait_seconds.
    slow = csc.StationConfigCache(ttl_seconds=60, wait_seconds=0.2)
    release = threading.Event()
    leader_result = []

    def blocked():
        release.wait(5)
        return ok

    leader = threading.Thread(target=lambda: leader_result.append(slow.get(blocked)))
    leader.start()
    deadline = time.monotonic() + 5
    while slow._inflight is None and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    assert slow.get(lambda: pytest.fail("a second load while one is in flight")) is None
    assert 0.15 < time.monotonic() - started < 2 and slow.timeouts == 1
    release.set()
    leader.join(5)
    assert leader_result == [ok] and slow.get(lambda: pytest.fail("cached")) is ok
    slow.invalidate()
    assert slow.get(lambda: ok) is ok and slow.loads == 2


# --------------------------------------------------------------------------- #
# Validation rules
# --------------------------------------------------------------------------- #

VALID = {
    csc.KEY_NOTIFICATION_ADDRESS: [
        "", "none", "ws://127.0.0.1:9000", "wss://pulse.example.com/ws/{pc_id}?name={pc_name_urlenc}",
        "wss://{pc_id}.example.com", "wss://h/{pc_id}{pc_id}", "ws://[2001:db8::1]:8080/x", "wss://x/" + "a" * 2040,
    ],
    csc.KEY_HEARTBEAT_INTERVAL_SEC: ["0", "5", "15", "3600"],
    csc.KEY_CLIENT_MANAGED_CONFIG: ["true", "false"],
    csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: [
        "", _admin_hash(),
        csc.make_admin_password_hash("x", salt="abcdEFGH", iterations=100_000),
        csc.make_admin_password_hash("x", salt="a+b/c=d.e_f-g", iterations=10_000_000),
    ],
}
INVALID = {
    csc.KEY_NOTIFICATION_ADDRESS: [
        "None", "NONE", "http://198.51.100.7/x", "ws:/x", "ws://", "WSS://x", "wss://a b", "wss://x/\t",
        " wss://x", "wss://x ", "wss://x/\x00", "wss://x/{name}", "wss://x/{pc_id", "wss://x/pc_id}", "wss://x/{}",
        "wss://fixture-user:fixture-pass@198.51.100.7/x", "wss://x/@", "wss://x/" + "a" * 2041, "********",
        "rtsp://198.51.100.7/x", "{pc_id}", "wss://x\n", "wss://x/\x85",
    ],
    # "5\n" and "0\n" passed before C11 (a "$" anchor matches before a trailing newline).
    csc.KEY_HEARTBEAT_INTERVAL_SEC: ["1", "4", "3601", "-1", " 5", "5 ", "05", "abc", "", "1e3", "5.0", "00",
                                     "99999", "********", "5\n", "0\n", "٥"],
    csc.KEY_CLIENT_MANAGED_CONFIG: ["True", "FALSE", "yes", "1", "0", "", "on", "********", "true\n"],
    csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: [
        "none", "fixture-plain-password", "********", _admin_hash() + "\n",
        csc.make_admin_password_hash("x", salt="abcdEFGH", iterations=99_999),
        csc.make_admin_password_hash("x", salt="abcdEFGH", iterations=10_000_001),
        "pbkdf2_sha256$200000$short$" + base64.b64encode(b"\0" * 32).decode(),
        "pbkdf2_sha256$200000$abcdEFGH$" + base64.b64encode(b"\0" * 31).decode() + "A",
        "pbkdf2_sha256$200000$abcdEFGH$" + base64.b64encode(b"\0" * 16).decode(),
        "pbkdf2_sha1$200000$abcdEFGH$" + base64.b64encode(b"\0" * 32).decode(),
        "pbkdf2_sha256$200000$abc$EFGH$" + base64.b64encode(b"\0" * 32).decode(),
    ],
}


def test_validation_rules():
    for key in csc.ADDRESS_KEYS:
        for value in VALID[csc.KEY_NOTIFICATION_ADDRESS]:
            assert csc.check_value(key, value) is None, (key, value)
        for value in INVALID[csc.KEY_NOTIFICATION_ADDRESS]:
            reason = csc.check_value(key, value)
            assert reason, (key, value)
            if len(value.strip()) > 5:  # longer than the fixed words of the messages ("ws://")
                assert value not in reason
    for key in (csc.KEY_HEARTBEAT_INTERVAL_SEC, csc.KEY_CLIENT_MANAGED_CONFIG, csc.KEY_CLIENT_ADMIN_PASSWORD_HASH):
        for value in VALID[key]:
            assert csc.check_value(key, value) is None, (key, value)
        for value in INVALID[key]:
            reason = csc.check_value(key, value)
            assert reason, (key, value)
            if len(value) > 4:
                assert value not in reason
    for key in csc.KEYS:
        assert csc.check_value(key, 15) == "must be a string"
        assert csc.check_value(key, None) == "must be a string"
    # Keys outside the fleet set are not this module's business.
    assert csc.check_value("smtp.password", "anything") is None
    assert csc.check_value("core.unknown_key", "x y") is None
    assert csc.parse_value(csc.KEY_HEARTBEAT_INTERVAL_SEC, "15") == 15
    assert csc.parse_value(csc.KEY_CLIENT_MANAGED_CONFIG, "false") is False
    with pytest.raises(ValueError) as excinfo:
        csc.parse_value(csc.KEY_ACK_ADDRESS, "http://fixture-host/x")
    assert "fixture-host" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Settings API: masking and validation
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(db, monkeypatch):
    from app.api.deps import get_super_admin_user
    from app.api.v1 import settings as settings_api
    from app.database import get_db
    from app.main import app

    settings_seeder.seed_settings(db)

    def _db():
        yield db

    admin = SimpleNamespace(user_id=uuid.uuid4(), username="fixture-admin", role="super_admin", is_active=True)
    service = _service(monkeypatch, Fernet.generate_key().decode())
    monkeypatch.setattr(settings_api, "system_settings_service", service)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_super_admin_user] = lambda: admin
    try:
        yield SimpleNamespace(client=TestClient(app), service=service, settings_api=settings_api, admin=admin)
    finally:
        app.dependency_overrides.clear()


def _core_values(response):
    return {s["key"]: s["value"] for s in response.json()["core"]}


def test_masking_and_validation(api, db, monkeypatch):
    client = api.client
    shown = _core_values(client.get("/api/v1/settings/"))
    assert shown == {k: ("true" if k == csc.KEY_CLIENT_MANAGED_CONFIG else "0" if k == csc.KEY_HEARTBEAT_INTERVAL_SEC
                         else "") for k in csc.KEYS}

    # Set values are masked; '' and none are not (core keys only).
    for key, value in ((csc.KEY_NOTIFICATION_ADDRESS, SET_ADDRESS), (csc.KEY_LOGS_SERVER_ADDRESS, "none"),
                       (csc.KEY_CLIENT_ADMIN_PASSWORD_HASH, _admin_hash()), (csc.KEY_HEARTBEAT_INTERVAL_SEC, "15")):
        response = client.put(f"/api/v1/settings/{key}", json={"value": value})
        assert response.status_code == 200, response.text
        expected = "********" if key in (csc.KEY_NOTIFICATION_ADDRESS, csc.KEY_CLIENT_ADMIN_PASSWORD_HASH) else value
        assert response.json()["value"] == expected
    response = client.put("/api/v1/settings/smtp.password", json={"value": "none"})
    assert response.status_code == 200 and response.json()["value"] == "********"
    expected = {**shown, csc.KEY_NOTIFICATION_ADDRESS: "********", csc.KEY_LOGS_SERVER_ADDRESS: "none",
                csc.KEY_CLIENT_ADMIN_PASSWORD_HASH: "********", csc.KEY_HEARTBEAT_INTERVAL_SEC: "15"}
    responses = [client.get("/api/v1/settings/"), client.get("/api/v1/settings/category/core"),
                 client.get(f"/api/v1/settings/{csc.KEY_NOTIFICATION_ADDRESS}"),
                 client.get(f"/api/v1/settings/{csc.KEY_LOGS_SERVER_ADDRESS}"),
                 client.get("/api/v1/settings/smtp.password")]
    assert _core_values(responses[0]) == expected
    assert {s["key"]: s["value"] for s in responses[1].json()["settings"]} == expected
    assert responses[2].json()["value"] == "********" and responses[3].json()["value"] == "none"
    assert responses[4].json()["value"] == "********"
    assert {s["key"]: s["value"] for s in responses[0].json()["smtp"]}["smtp.password"] == "********"

    # No response carries ciphertext or a set value, and no row was changed to
    # mask it (the test shares one session across requests, so a row masked in
    # place would be written by the next commit).
    assert client.put(f"/api/v1/settings/{csc.KEY_HEARTBEAT_INTERVAL_SEC}", json={"value": "20"}).status_code == 200
    rows = _rows(db)
    ciphertexts = [rows[k].value for k in (csc.KEY_NOTIFICATION_ADDRESS, csc.KEY_LOGS_SERVER_ADDRESS,
                                           csc.KEY_CLIENT_ADMIN_PASSWORD_HASH, "smtp.password")]
    assert all(c and c != "********" for c in ciphertexts)
    text = "".join(r.text for r in responses)
    for secret in ciphertexts + [SECRET_QUERY, _admin_hash()]:
        assert secret not in text
    assert api.service.decrypt_strict(rows[csc.KEY_LOGS_SERVER_ADDRESS].value) == "none"

    # A process that cannot decrypt shows set values masked, never ciphertext.
    monkeypatch.setattr(api.settings_api, "system_settings_service",
                        _service(monkeypatch, Fernet.generate_key().decode()))
    blind = _core_values(client.get("/api/v1/settings/"))
    assert blind[csc.KEY_LOGS_SERVER_ADDRESS] == "********" and blind[csc.KEY_ACK_ADDRESS] == ""


def test_api_validates_core_writes(api, db):
    client = api.client
    before = {k: r.value for k, r in _rows(db).items()}
    for key in (csc.KEY_ACK_ADDRESS, csc.KEY_HEARTBEAT_INTERVAL_SEC, csc.KEY_CLIENT_MANAGED_CONFIG,
                csc.KEY_CLIENT_ADMIN_PASSWORD_HASH):
        group = csc.KEY_NOTIFICATION_ADDRESS if key in csc.ADDRESS_KEYS else key
        for value in INVALID[group]:
            response = client.put(f"/api/v1/settings/{key}", json={"value": value})
            assert response.status_code == 422, (key, value, response.text)
            detail = response.json()["detail"]
            assert key in detail and "Nothing was changed" in detail
            if len(value.strip()) > 5:  # longer than the fixed words of the messages ("ws://")
                assert value not in detail
    assert {k: r.value for k, r in _rows(db).items()} == before

    # A bulk update with one invalid value writes nothing (422 before any write).
    response = client.put("/api/v1/settings/bulk", json={"settings": {
        csc.KEY_HEARTBEAT_INTERVAL_SEC: "30", "smtp.host": "198.51.100.25", csc.KEY_ACK_ADDRESS: "http://x/y"}})
    assert response.status_code == 422 and csc.KEY_ACK_ADDRESS in response.json()["detail"]
    assert csc.KEY_HEARTBEAT_INTERVAL_SEC not in response.json()["detail"]
    assert {k: r.value for k, r in _rows(db).items()} == before

    # Valid writes, including only the changed key (C11 sends only what changed).
    for key in (csc.KEY_ACK_ADDRESS, csc.KEY_HEARTBEAT_INTERVAL_SEC, csc.KEY_CLIENT_MANAGED_CONFIG,
                csc.KEY_CLIENT_ADMIN_PASSWORD_HASH):
        group = csc.KEY_NOTIFICATION_ADDRESS if key in csc.ADDRESS_KEYS else key
        for value in VALID[group]:
            response = client.put(f"/api/v1/settings/{key}", json={"value": value})
            assert response.status_code == 200, (key, value, response.text)
    response = client.put("/api/v1/settings/bulk", json={"settings": {csc.KEY_HEARTBEAT_INTERVAL_SEC: "60"}})
    assert response.status_code == 200 and [s["key"] for s in response.json()] == [csc.KEY_HEARTBEAT_INTERVAL_SEC]
    response = client.put("/api/v1/settings/bulk", json={"settings": {
        csc.KEY_ACK_ADDRESS: SET_ADDRESS, csc.KEY_CLIENT_MANAGED_CONFIG: "false"}})
    assert response.status_code == 200
    assert {s["key"]: s["value"] for s in response.json()} == {csc.KEY_ACK_ADDRESS: "********",
                                                               csc.KEY_CLIENT_MANAGED_CONFIG: "false"}
    assert SECRET_QUERY not in response.text
    rows = _rows(db)
    assert rows[csc.KEY_HEARTBEAT_INTERVAL_SEC].value == "60" and rows[csc.KEY_CLIENT_MANAGED_CONFIG].value == "false"
    assert rows[csc.KEY_ACK_ADDRESS].updated_by == api.admin.user_id
    # Unknown core.* keys and other categories are not validated by these rules.
    assert client.put("/api/v1/settings/smtp.host", json={"value": "not a url"}).status_code == 200


def test_validation_comes_before_the_missing_key_503(api, db, monkeypatch):
    """Without SETTINGS_ENCRYPTION_KEY: an invalid value is still 422, a valid
    encrypted core value 503 (C18), a valid plain core value 200."""
    monkeypatch.setattr(api.settings_api, "system_settings_service", _service(monkeypatch, None))
    client = api.client
    assert client.put(f"/api/v1/settings/{csc.KEY_ACK_ADDRESS}", json={"value": "http://x/y"}).status_code == 422
    assert client.put(f"/api/v1/settings/{csc.KEY_ACK_ADDRESS}", json={"value": SET_ADDRESS}).status_code == 503
    assert client.put("/api/v1/settings/bulk", json={"settings": {
        csc.KEY_ACK_ADDRESS: "wss://x/{bad}", csc.KEY_HEARTBEAT_INTERVAL_SEC: "15"}}).status_code == 422
    assert client.put(f"/api/v1/settings/{csc.KEY_HEARTBEAT_INTERVAL_SEC}", json={"value": "15"}).status_code == 200
    assert _stored(db, csc.KEY_ACK_ADDRESS) == ""
    assert client.put("/api/v1/settings/core.not_a_key", json={"value": "x"}).status_code == 404
