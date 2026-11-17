"""
Relay trust baseline and feature advertisement (plan 06 D12, OD-23).

Card tests: "relay register, identity and registered payload", "sender auth and
legacy-open", "broadcast scope", "token exposure" and
test_require_current_token_rejects_minted_token, plus "packet logging is off".
The relay handlers are called directly, with sio.emit and get_db
monkeypatched (tests/relay_testkit.py); only the pcs table exists, on SQLite.
The real eventlet relay with a real 1.1.1 Client is in
tests/integration/test_relay_phase0.py.

    python -m pytest tests/unit/test_relay_trust.py -o addopts='' -p no:cacheprovider
"""

import logging
import secrets
from types import SimpleNamespace

import pytest
import socketio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.schemas import station_protocol as sp
from app.services import relay_auth
from tests.relay_testkit import RelayHarness, import_relay, make_pc_token

relay = import_relay()

LAYOUT = {"screens": [{"id": "screen-1", "views": []}], "app_id": "fixture"}


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "x" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    harness = RelayHarness(relay, monkeypatch)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()


@pytest.fixture
def legacy(monkeypatch, h):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "")
    return h


def _registered(h, sid):
    payloads = h.received(sid, "registered")
    assert len(payloads) == 1, h.events_for(sid)
    return payloads[0]


# --------------------------------------------------------------------------- #
# Packet logging (NEW-56)
# --------------------------------------------------------------------------- #


def test_packet_logging_is_off_by_default():
    assert Settings.model_fields["RELAY_DEBUG_LOGGING"].default is False
    assert relay.settings.RELAY_DEBUG_LOGGING is False
    for logger_ in (relay.sio.logger, relay.sio.eio.logger):
        assert not logger_.isEnabledFor(logging.INFO)
        assert logger_.isEnabledFor(logging.WARNING)


def test_packet_logging_follows_the_flag_whatever_was_created_before():
    try:
        noisy = relay._make_server(True, "threading")
        assert noisy.logger.isEnabledFor(logging.INFO) and noisy.eio.logger.isEnabledFor(logging.INFO)
        quiet = relay._make_server(False, "threading")
        assert not quiet.logger.isEnabledFor(logging.INFO)
        assert not quiet.eio.logger.isEnabledFor(logging.INFO)
    finally:
        relay._make_server(relay.settings.RELAY_DEBUG_LOGGING, "threading")


def test_async_mode_defaults_to_eventlet_and_run_server_refuses_threading(monkeypatch):
    import ast
    from pathlib import Path

    source = Path(relay.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get(ASYNC_MODE_ENV, "eventlet")' in source
    ast.parse(source)
    monkeypatch.setattr(relay, "ASYNC_MODE", "threading")
    with pytest.raises(RuntimeError):
        relay.run_server("127.0.0.1", 0)


# --------------------------------------------------------------------------- #
# Register, identity and the registered payload
# --------------------------------------------------------------------------- #


def test_register_valid_pc_token(h):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token, name="Wall A")

    payload = _registered(h, "pc-a")
    model = sp.validate("RegisteredV1", payload)
    assert model.pc_id == "PC_A"
    assert payload["message"] == "Successfully registered"
    assert payload["protocol_hash"] == sp.protocol_hash()
    assert payload["features"] == []  # no report handler exists yet (see the feature tests)
    assert relay.connected_pcs == {"PC_A": "pc-a"}
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.session_data["pc-a"]["name"] == "Wall A"
    assert relay.session_data["pc-a"]["flags"] == {}
    assert h.errors("pc-a") == []
    row = h.pc_row("PC_A")
    assert row.last_connected and row.last_seen == row.last_connected


def test_a_1_1_x_register_payload_registers(h):
    """Client 1.1.x sends exactly {pc_id, auth_token} (golden payload, D1)."""
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    relay.register("pc-a", {"pc_id": "PC_A", "auth_token": token})
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.session_data["pc-a"]["name"] == ""


def test_token_pc_id_is_authoritative(h, caplog):
    token = h.add_pc("PC_A")
    h.add_pc("PC_B")
    h.connect("pc-a")
    with caplog.at_level(logging.WARNING):
        h.register("pc-a", "PC_B", token)
    assert relay.connected_pcs == {"PC_A": "pc-a"}
    assert _registered(h, "pc-a")["pc_id"] == "PC_A"
    assert "PC ID mismatch" in caplog.text


@pytest.mark.parametrize(
    "data",
    [None, "PC_A", ["PC_A"], {}, {"pc_id": "PC_A"}, {"auth_token": "x"}, {"pc_id": 7, "auth_token": "x"},
     {"pc_id": "PC_A", "auth_token": ["x"]}],
    ids=["none", "string", "list", "empty", "no-token", "no-pc-id", "int-pc-id", "list-token"],
)
def test_register_rejects_malformed_payloads(h, data):
    h.add_pc("PC_A")
    h.connect("pc-a")
    relay.register("pc-a", data)
    assert h.errors("pc-a") == [relay.MSG_MISSING]
    assert relay.connected_pcs == {}
    assert h.received("pc-a", "registered") == []


def test_register_rejects_bad_signatures_and_expired_tokens(h):
    h.add_pc("PC_A")
    h.connect("pc-a")
    for token in (
        make_pc_token("PC_A", "another-secret-" + "y" * 20),
        make_pc_token("PC_A", h.secret, exp_delta=-10),
        "not-a-jwt",
        "x" * (relay.MAX_TOKEN_LENGTH + 1),
    ):
        h.register("pc-a", "PC_A", token)
    assert h.errors("pc-a") == [relay.MSG_INVALID_TOKEN] * 4
    assert relay.connected_pcs == {} and relay._registered_pc("pc-a") is None


@pytest.mark.parametrize(
    "token_type", ["access", "refresh", None, "PC_AUTH"], ids=["access", "refresh", "no-type", "wrong-case"]
)
def test_register_rejects_non_pc_tokens(h, token_type):
    """NEW-42/NEW-72: a token signed with the right secret but not of type
    pc_auth (a user token, or the shape the Client mints locally) is refused."""
    h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", make_pc_token("PC_A", h.secret, token_type=token_type))
    assert h.errors("pc-a") == [relay.MSG_NOT_PC_TOKEN]
    assert relay.connected_pcs == {}


def test_register_rejects_a_token_without_pc_id(h):
    h.connect("pc-a")
    token = make_pc_token("PC_A", h.secret, extra={"pc_id": None})
    h.register("pc-a", "PC_A", token)
    assert h.errors("pc-a") == [relay.MSG_INVALID_TOKEN]


def test_register_rejects_deleted_pcs(h):
    token = make_pc_token("PC_GONE", h.secret)  # valid token, no row
    h.connect("pc-x")
    h.register("pc-x", "PC_GONE", token)
    assert h.errors("pc-x") == [relay.MSG_UNKNOWN_PC]
    assert relay.connected_pcs == {} and relay._registered_pc("pc-x") is None
    assert relay.relay_counters["rejected_registrations"] == 1


def test_outdated_token_registers_with_flag(h, caplog):
    """OD-23 (a): a valid pc_auth token that differs from pcs.auth_token."""
    h.add_pc("PC_A")  # stores one token ...
    minted = make_pc_token("PC_A", h.secret, exp_delta=7200)  # ... the station presents another
    sender = "sender-1"
    h.connect_sender(sender)
    h.connect("pc-a")
    with caplog.at_level(logging.WARNING):
        h.register("pc-a", "PC_A", minted)
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_TOKEN_OUTDATED: True}
    assert "token_outdated" in caplog.text
    assert minted not in caplog.text
    update = h.received(sender, "clients_update")[-1]
    assert update["clients"][0]["flags"] == {"token_outdated": True}


def test_null_stored_token_counts_as_outdated(h):
    h.add_pc("PC_A", token=None)
    h.connect("pc-a")
    h.register("pc-a", "PC_A", make_pc_token("PC_A", h.secret))
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_TOKEN_OUTDATED: True}


def test_require_current_token_rejects_minted_token(h, monkeypatch):
    """OD-23 (b): with RELAY_REQUIRE_CURRENT_TOKEN, a token signed with the
    right secret but different from pcs.auth_token is rejected; the stored one
    registers."""
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    stored = h.add_pc("PC_A")
    minted = make_pc_token("PC_A", h.secret, exp_delta=7200)
    assert minted != stored
    h.connect("pc-a")
    h.register("pc-a", "PC_A", minted)
    assert h.errors("pc-a") == [relay.MSG_TOKEN_REPLACED]
    assert relay.connected_pcs == {}
    assert h.pc_row("PC_A").last_connected is None  # a refused PC is not stamped
    h.register("pc-a", "PC_A", stored)
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.session_data["pc-a"]["flags"] == {}


def test_database_error_registers_fail_open_and_flags(h, caplog):
    token = make_pc_token("PC_A", h.secret)
    h.db_fails = True
    h.connect("pc-a")
    with caplog.at_level(logging.WARNING):
        h.register("pc-a", "PC_A", token)
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_DB_UNVERIFIED: True}
    assert "db_unverified" in caplog.text


def test_duplicate_registration_takes_over_and_is_flagged(h, caplog):
    token = h.add_pc("PC_A")
    h.connect("pc-old")
    h.register("pc-old", "PC_A", token)
    h.connect("pc-new")
    with caplog.at_level(logging.WARNING):
        h.register("pc-new", "PC_A", token)
    assert relay.connected_pcs == {"PC_A": "pc-new"}
    assert relay._registered_pc("pc-new") == "PC_A"
    assert relay._registered_pc("pc-old") is None  # stale: its reports are not accepted
    assert relay.session_data["pc-old"]["pc_id"] is None
    assert relay.session_data["pc-old"]["superseded_by"] == "pc-new"
    assert relay.session_data["pc-new"]["flags"] == {relay.FLAG_DUPLICATE: True}
    assert relay.relay_counters["duplicate_registrations"] == 1
    assert "Duplicate registration for PC PC_A" in caplog.text
    assert h.disconnects == []  # nobody is disconnected

    # The stale session's disconnect does not drop the live registration.
    relay.disconnect("pc-old")
    assert relay.connected_pcs == {"PC_A": "pc-new"}


def test_registered_pc_requires_the_current_sid(h):
    """_registered_pc requires connected_pcs[pc_id] == sid even when a session
    still names the pc_id (defence in depth against a missed hand-over)."""
    relay.session_data["s-old"] = relay._new_session(relay.ROLE_CLIENT)
    relay.session_data["s-old"]["pc_id"] = "PC_A"
    relay.session_data["s-new"] = relay._new_session(relay.ROLE_CLIENT)
    relay.session_data["s-new"]["pc_id"] = "PC_A"
    relay.connected_pcs["PC_A"] = "s-new"
    assert relay._registered_pc("s-old") is None
    assert relay._registered_pc("s-new") == "PC_A"
    assert relay._registered_pc("s-missing") is None
    del relay.connected_pcs["PC_A"]
    assert relay._registered_pc("s-new") is None


def test_re_register_on_the_same_session_is_not_a_duplicate(h):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.register("pc-a", "PC_A", token)
    assert relay.session_data["pc-a"]["flags"] == {}
    assert relay.relay_counters["duplicate_registrations"] == 0
    assert len(h.received("pc-a", "registered")) == 2


def test_session_registering_as_another_pc_releases_the_first(h):
    token_a = h.add_pc("PC_A")
    token_b = h.add_pc("PC_B")
    h.connect("pc-1")
    h.register("pc-1", "PC_A", token_a)
    h.register("pc-1", "PC_B", token_b)
    assert relay.connected_pcs == {"PC_B": "pc-1"}


def test_sender_session_cannot_register(h):
    token = h.add_pc("PC_A")
    h.connect_sender("sender-1")
    h.register("sender-1", "PC_A", token)
    assert h.errors("sender-1") == [relay.MSG_SENDER_REGISTER]
    assert relay.connected_pcs == {}


def test_register_never_logs_the_token(h, caplog):
    token = h.add_pc("PC_A")
    other = make_pc_token("PC_A", h.secret, exp_delta=99)
    h.connect("pc-a")
    with caplog.at_level(logging.DEBUG):
        h.register("pc-a", "PC_A", token)
        h.register("pc-a", "PC_A", other)
        h.register("pc-a", "PC_A", make_pc_token("PC_A", "wrong-secret-" + "z" * 20))
    assert token not in caplog.text and other not in caplog.text


@pytest.mark.parametrize("case", ["unregistered", "stale", "wrong_type", "deleted", "current"])
def test_reports_only_from_the_current_registered_session(h, case):
    """Acceptance: stale or unregistered sids, wrong token types and deleted
    PCs cannot produce accepted reports (handlers use requires_registered_pc)."""
    calls = []

    @relay.requires_registered_pc
    def fake_report(sid, pc_id, data):
        calls.append((sid, pc_id, data))
        return {"status": "ok"}

    token = h.add_pc("PC_A")
    h.connect("pc-a")
    if case == "stale":
        h.register("pc-a", "PC_A", token)
        h.connect("pc-b")
        h.register("pc-b", "PC_A", token)
    elif case == "wrong_type":
        h.register("pc-a", "PC_A", make_pc_token("PC_A", h.secret, token_type="access"))
    elif case == "deleted":
        h.register("pc-a", "PC_GONE", make_pc_token("PC_GONE", h.secret))
    elif case == "current":
        h.register("pc-a", "PC_A", token)

    ack = fake_report("pc-a", {"message_id": "m-1"})
    if case == "current":
        assert ack == {"status": "ok"} and calls == [("pc-a", "PC_A", {"message_id": "m-1"})]
    else:
        assert ack == {"status": "rejected", "reason": "not registered"}
        assert calls == []
        sp.validate("ReportAckV1", ack)


# --------------------------------------------------------------------------- #
# Feature advertisement
# --------------------------------------------------------------------------- #


def test_features_follow_handlers_and_the_reports_switch(h, monkeypatch):
    handlers = dict(relay.sio.handlers["/"])
    monkeypatch.setitem(relay.sio.handlers, "/", handlers)

    assert relay.advertised_features() == []
    for feature in relay.ADVERTISABLE_FEATURES:
        relay.enable_feature(feature)
    # Event-backed features need their handler; pc_token.v1 is a push.
    assert relay.advertised_features() == [sp.FEATURE_PC_TOKEN]

    for event in ("deploy_result", "station_status", "get_station_config"):
        handlers[event] = lambda *a: None
    assert relay.advertised_features() == [
        sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_STATION_CONFIG, sp.FEATURE_PC_TOKEN,
    ]
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", False)
    assert relay.advertised_features() == [
        sp.FEATURE_STATION_CONFIG, sp.FEATURE_PC_TOKEN, sp.FEATURE_STATION_REPORTS_DISABLED,
    ]
    for features in (relay.advertised_features(),):
        sp.validate("RegisteredV1", {"pc_id": "PC_A", "features": features, "protocol_hash": sp.protocol_hash()})


def test_reports_off_advertises_disabled(h, monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", False)
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    assert _registered(h, "pc-a")["features"] == [sp.FEATURE_STATION_REPORTS_DISABLED]


def test_unknown_feature_is_refused():
    with pytest.raises(ValueError):
        relay.enable_feature("deploy_result.v2")
    with pytest.raises(ValueError):
        relay.enable_feature(sp.FEATURE_STATION_REPORTS_DISABLED)


def test_every_advertised_event_feature_has_its_handler():
    """Guard for D15/C10: whatever the module enabled at import, a feature is
    only advertised with the handler that serves it."""
    import importlib

    module_features = set(importlib.import_module(relay.__name__)._enabled_features)
    for feature in module_features:
        event = relay.FEATURE_EVENTS.get(feature)
        if event is not None:
            assert event in relay.sio.handlers["/"], feature
    assert set(relay.FEATURE_EVENTS) | {sp.FEATURE_PC_TOKEN} == set(relay.ADVERTISABLE_FEATURES)
    assert set(relay.ADVERTISABLE_FEATURES) <= set(sp.KNOWN_FEATURES)


# --------------------------------------------------------------------------- #
# Sender auth and legacy-open
# --------------------------------------------------------------------------- #


def test_sender_with_the_secret_is_a_sender(h):
    h.connect_sender("sender-1")
    assert relay.session_data["sender-1"]["role"] == relay.ROLE_SENDER
    assert "sender-1" in h.rooms[relay.SENDERS_ROOM]
    assert relay._is_sender("sender-1")


@pytest.mark.parametrize("auth", [
    {"role": "sender", "secret": "wrong"},
    {"role": "sender", "secret": ""},
    {"role": "sender"},
    {"role": "sender", "secret": None},
    {"role": "sender", "secret": 12345},
], ids=["wrong", "empty", "missing", "none", "int"])
def test_sender_without_the_secret_is_refused(h, auth):
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        h.connect("sender-x", auth)
    assert "sender-x" not in relay.session_data
    assert relay.SENDERS_ROOM not in h.rooms


@pytest.mark.parametrize("auth", [None, {}, {"role": "pc"}, "sender", ["sender"]], ids=["none", "empty", "pc", "str", "list"])
def test_other_sessions_are_clients(h, auth):
    h.connect("c-1", auth)
    assert relay.session_data["c-1"]["role"] == relay.ROLE_CLIENT
    assert not relay._is_sender("c-1")


def test_sender_message_reaches_the_pc_and_reports_the_ack(h):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.connect_sender("sender-1")
    relay.message("sender-1", {"type": "config", "targetId": "PC_A", "content": LAYOUT})

    forwarded = [(e, d, r, cb) for e, d, r, cb in h.emits if e == "config"]
    assert len(forwarded) == 1
    event, data, room, callback = forwarded[0]
    assert room == "pc-a" and data == {"type": "config", "content": LAYOUT, "from": "portal"}
    assert h.received("sender-1", "message_sent") == [{"targetId": "PC_A", "status": "delivered"}]
    assert h.received("sender-1", "message_ack") == []
    callback()  # what Client 1.1.x's ACK triggers (its handler returns None)
    assert h.received("sender-1", "message_ack") == [{"targetId": "PC_A", "status": "received"}]


def test_non_sender_message_is_rejected_without_disconnecting_anyone(h):
    """Acceptance: with the secret set, a non-sender `message` is rejected
    without disconnecting anyone."""
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.connect("intruder")  # no auth
    relay.message("intruder", {"type": "config", "targetId": "PC_A", "content": LAYOUT})
    relay.message("pc-a", {"type": "config", "targetId": "PC_A", "content": LAYOUT})  # a PC is no sender

    assert h.received("pc-a", "config") == []
    assert h.errors("intruder") == [relay.MSG_NOT_AUTHORIZED]
    assert h.errors("pc-a") == [relay.MSG_NOT_AUTHORIZED]
    assert h.disconnects == []
    assert relay._registered_pc("pc-a") == "PC_A"
    assert relay.relay_counters["rejected_messages"] == 2


@pytest.mark.parametrize("data", [
    {"type": "command", "targetId": "PC_A", "content": {}},
    {"type": "registered", "targetId": "PC_A", "content": {"pc_id": "PC_B"}},
    {"type": "clients_update", "targetId": "PC_A", "content": {}},
    {"type": None, "targetId": "PC_A", "content": LAYOUT},
    {"targetId": "PC_A", "content": LAYOUT},
], ids=["command", "registered", "clients_update", "none", "missing"])
def test_message_type_allow_list(h, data):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.connect_sender("sender-1")
    before = len(h.received("pc-a"))
    relay.message("sender-1", data)
    assert len(h.received("pc-a")) == before
    assert h.errors("sender-1") == ["Message type not allowed"]


@pytest.mark.parametrize("data,error", [
    ("config", "Invalid message"),
    ({"type": "config", "content": LAYOUT}, "Missing targetId"),
    ({"type": "config", "targetId": 5, "content": LAYOUT}, "Missing targetId"),
    ({"type": "config", "targetId": "PC_A", "content": "layout"}, "Invalid message content"),
    ({"type": "config", "targetId": "PC_A", "content": None}, "Invalid message content"),
    ({"type": "config", "targetId": "PC_OFF", "content": LAYOUT}, "Target PC PC_OFF is not online"),
], ids=["not-dict", "no-target", "int-target", "str-content", "none-content", "offline"])
def test_sender_message_validation(h, data, error):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.connect_sender("sender-1")
    relay.message("sender-1", data)
    assert h.errors("sender-1") == [error]
    assert h.received("pc-a", "config") == []


def test_legacy_open_still_delivers(legacy, caplog):
    h = legacy
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    h.connect("old-portal")  # an API image without sender auth
    with caplog.at_level(logging.WARNING):
        relay.message("old-portal", {"type": "config", "targetId": "PC_A", "content": LAYOUT})
        relay.message("old-portal", {"type": "config", "targetId": "PC_A", "content": LAYOUT})
    assert len(h.received("pc-a", "config")) == 2
    assert h.received("old-portal", "message_sent") == [{"targetId": "PC_A", "status": "delivered"}] * 2
    assert caplog.text.count("legacy-open") == 1  # warned once, not per message


def test_legacy_open_keeps_pcs_and_the_allow_list(legacy):
    h = legacy
    token = h.add_pc("PC_A")
    h.add_pc("PC_B")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    relay.message("pc-a", {"type": "config", "targetId": "PC_A", "content": LAYOUT})
    assert h.errors("pc-a") == [relay.MSG_NOT_AUTHORIZED]
    h.connect("old-portal")
    relay.message("old-portal", {"type": "command", "targetId": "PC_A", "content": {}})
    assert h.errors("old-portal") == ["Message type not allowed"]
    assert h.received("pc-a", "config") == []


def test_legacy_open_accepts_a_sender_claim_without_a_secret(legacy):
    h = legacy
    h.connect("sender-1", relay_auth.sender_auth(""))
    assert relay.session_data["sender-1"]["role"] == relay.ROLE_SENDER


def test_sender_auth_payload_and_secret_compare():
    assert relay_auth.sender_auth("abc") == {"role": "sender", "secret": "abc"}
    assert relay_auth.sender_auth(None) == {"role": "sender", "secret": ""}
    assert relay_auth.secret_matches("abc", "abc")
    assert not relay_auth.secret_matches("abc", "abd")
    assert not relay_auth.secret_matches("", "")
    assert not relay_auth.secret_matches("abc", "")
    assert not relay_auth.secret_matches(None, "abc")
    assert relay_auth.same_token("t1", "t1") and not relay_auth.same_token("t1", "t2")
    assert not relay_auth.same_token("t1", None) and not relay_auth.same_token(None, None)


# The API's sender path (deploy_service.socketio_sender, D14) authenticates with
# the CONNECT auth payload: test_api_sender_connects_with_the_secret moved to
# tests/unit/test_deploy_path.py with the function it tests.


# --------------------------------------------------------------------------- #
# Broadcast scope (NEW-57)
# --------------------------------------------------------------------------- #


def test_presence_goes_to_senders_only(h):
    token_a = h.add_pc("PC_A")
    token_b = h.add_pc("PC_B")
    h.connect_sender("sender-1")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token_a)
    h.connect("pc-b")
    h.register("pc-b", "PC_B", token_b)
    h.connect("watcher")  # unauthenticated, not a PC
    relay.disconnect("pc-b")

    for sid in ("pc-a", "pc-b", "watcher"):
        assert "clients_update" not in h.events_for(sid), sid
    updates = h.received("sender-1", "clients_update")
    assert [sorted(c["pc_id"] for c in u["clients"]) for u in updates] == [["PC_A"], ["PC_A", "PC_B"], ["PC_A"]]
    assert all(set(c) == {"pc_id", "name", "connected_at", "flags"} for u in updates for c in u["clients"])
    # No broadcast without a room: every clients_update is addressed to the senders.
    assert {room for e, _d, room, _cb in h.emits if e == "clients_update"} == {relay.SENDERS_ROOM}


def test_get_clients_is_sender_only(h):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    relay.get_clients("pc-a")
    assert h.received("pc-a", "clients_list") == [] and h.errors("pc-a") == [relay.MSG_NOT_AUTHORIZED]
    h.connect("watcher")
    relay.get_clients("watcher", {"any": "payload"})
    assert h.received("watcher", "clients_list") == []
    h.connect_sender("sender-1")
    relay.get_clients("sender-1")
    assert [c["pc_id"] for c in h.received("sender-1", "clients_list")[0]["clients"]] == ["PC_A"]


def test_legacy_open_presence_never_reaches_pcs(legacy):
    h = legacy
    token = h.add_pc("PC_A")
    h.connect("old-portal")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    assert "clients_update" not in h.events_for("pc-a")
    relay.get_clients("pc-a")
    assert h.received("pc-a", "clients_list") == []
    relay.get_clients("old-portal")  # the legacy sender may still ask
    assert len(h.received("old-portal", "clients_list")) == 1


def test_connected_pcs_http_endpoint_is_unchanged(h):
    """Q21 is open: /api/connected-pcs keeps its shape and carries no flags."""
    import json

    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token, name="Wall A")
    status = {}
    body = relay.app.get_connected_pcs({}, lambda s, headers: status.update(s=s))
    data = json.loads(b"".join(body))
    assert status["s"] == "200 OK"
    assert data["total_connected"] == 1
    assert set(data["connected_pcs"][0]) == {"pc_id", "name", "connected_at", "is_connected"}


# --------------------------------------------------------------------------- #
# Token exposure (NEW-40)
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(monkeypatch):
    from app.api import deps
    from app.api.v1 import pcs as pcs_api
    from app.database import get_db
    from app.models.pc import PC
    from app.models.team import Team

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    meta = MetaData()
    for model in (Team, PC):
        table = model.__table__.to_metadata(meta)
        for constraint in list(table.foreign_key_constraints):
            table.constraints.discard(constraint)
        table.create(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()
    db.add(Team(id="TEAM_test", name="Team"))
    token = make_pc_token("PC_A", "fixture-" + secrets.token_hex(16))
    db.add(PC(id="PC_A", name="Wall A", role="controller", team_id="TEAM_test", auth_token=token,
              token_expiry=4102444000))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(pcs_api.router, prefix="/pcs")
    user = SimpleNamespace(role="user", is_active=True, username="viewer")

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    return SimpleNamespace(client=TestClient(app), user=user, token=token)


@pytest.mark.parametrize("role,visible", [("user", False), ("admin", True), ("super_admin", True)])
def test_token_exposure(api, role, visible):
    api.user.role = role
    for path in ("/pcs", "/pcs/PC_A", "/pcs/PC_A/screens"):
        response = api.client.get(path)
        assert response.status_code == 200, (path, response.text)
        body = response.json()
        row = body[0] if isinstance(body, list) else body
        assert row["id"] == "PC_A"
        assert row["token_expiry"] is not None
        if visible:
            assert row["auth_token"] == api.token, path
        else:
            assert row["auth_token"] is None, path
            assert api.token not in response.text, path


def test_websocket_client_module_is_gone():
    import importlib.util

    assert importlib.util.find_spec("app.services.websocket_client") is None
