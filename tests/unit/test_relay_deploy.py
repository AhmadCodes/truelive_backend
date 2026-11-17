"""
Relay side of the deploy path (plan 06 D14): "relay deploy_config ACK and
callback".

The relay's handlers are called directly (tests/relay_testkit.py: emits are
recorded, background tasks run inline, get_db serves SQLite holding pcs and
pc_deployments):

* deploy_config is for sender sessions only and validates its request;
* an unregistered or replaced target is ``offline``; a registered one gets
  the layout as the ``config`` event, unchanged, with a callback, and the
  sender gets ``delivered`` plus ``confirmable``;
* confirmable needs all three: the station declared ``deploy_result.v1``,
  ``registered`` advertised it, and the payload carries the config_id;
* the PC's ACK moves the row to received; for a non-confirmable row (Client
  1.1.x) it stamps pcs.last_applied, for a confirmable one it does not;
* an ACK whose row does not exist yet, or whose write fails, is kept and
  applied by the retry; a row of another PC is never touched;
* the layout is never logged.

    python -m pytest tests/unit/test_relay_deploy.py -o addopts='' -p no:cacheprovider
"""

import logging
import secrets
import uuid

import pytest

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.schemas import station_protocol as sp
from tests.relay_testkit import RelayHarness, import_relay

relay = import_relay()

MARKER = "relay-deploy-marker-" + secrets.token_hex(4)


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "y" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()
    relay.pending_deploy_events.clear()


def _layout(config_id=None):
    content = {"screens": [{"id": "screen-1", "title": MARKER, "source_groups": []}], "app_id": "fixture"}
    if config_id is not None:
        content["config_id"] = config_id
    return content


def _row(h, config_id, pc_id="PC_A", status="pending"):
    db = h.Session()
    try:
        db.add(PcDeployment(id=config_id, pc_id=pc_id, trigger="pc_deploy", status=status))
        db.commit()
    finally:
        db.close()


def _get(h, config_id):
    db = h.Session()
    try:
        return db.get(PcDeployment, config_id)
    finally:
        db.close()


def _pc(h, pc_id):
    db = h.Session()
    try:
        return db.get(PC, pc_id)
    finally:
        db.close()


def _online(h, pc_id="PC_A", sid="pc-a", **register_extra):
    token = h.add_pc(pc_id)
    h.connect(sid)
    h.register(sid, pc_id, token, **register_extra)
    assert relay._registered_pc(sid) == pc_id
    return token


def _call(sid, config_id, target="PC_A", content="default"):
    if content == "default":
        content = _layout(config_id)
    return relay.deploy_config(sid, {"config_id": config_id, "targetId": target, "content": content})


def _config_emits(h, sid):
    return [(data, cb) for name, data, target, cb in h.emits if name == "config" and target == sid]


def _enable_deploy_result(monkeypatch):
    """Make the relay advertise deploy_result.v1 (its handler arrives in D15)."""
    handlers = dict(relay.sio.handlers["/"])
    handlers.setdefault("deploy_result", lambda *a: None)
    monkeypatch.setitem(relay.sio.handlers, "/", handlers)
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)


# --------------------------------------------------------------------------- #
# Who may call it, and what it validates
# --------------------------------------------------------------------------- #


def test_deploy_config_is_registered_as_an_event():
    assert relay.sio.handlers["/"]["deploy_config"] is relay.deploy_config


@pytest.mark.parametrize("who", ["anonymous", "pc"])
def test_only_senders_may_deploy(h, who):
    _online(h)
    if who == "anonymous":
        h.connect("intruder")
        sid = "intruder"
    else:
        sid = "pc-a"
    answer = _call(sid, "cfg-1")
    assert answer == {"status": "error", "config_id": None, "confirmable": False, "reason": "not authorized"}
    assert _config_emits(h, "pc-a") == []
    assert h.disconnects == []
    assert relay.relay_counters["deploys_rejected"] == 1


@pytest.mark.parametrize(
    "data,reason",
    [
        (None, "invalid request"),
        ("text", "invalid request"),
        ({"targetId": "PC_A", "content": {}}, "invalid config_id"),
        ({"config_id": "", "targetId": "PC_A", "content": {}}, "invalid config_id"),
        ({"config_id": "x" * 65, "targetId": "PC_A", "content": {}}, "invalid config_id"),
        ({"config_id": "has space", "targetId": "PC_A", "content": {}}, "invalid config_id"),
        ({"config_id": 7, "targetId": "PC_A", "content": {}}, "invalid config_id"),
        ({"config_id": "cfg", "content": {}}, "invalid targetId"),
        ({"config_id": "cfg", "targetId": "P" * 65, "content": {}}, "invalid targetId"),
        ({"config_id": "cfg", "targetId": "PC_A"}, "invalid content"),
        ({"config_id": "cfg", "targetId": "PC_A", "content": [1]}, "invalid content"),
        ({"config_id": "cfg", "targetId": "PC_A", "content": {"config_id": "other"}}, "config_id mismatch"),
    ],
)
def test_requests_are_validated(h, data, reason):
    _online(h)
    h.connect_sender()
    answer = relay.deploy_config("sender-1", data)
    assert answer["status"] == "error" and answer["reason"] == reason
    assert _config_emits(h, "pc-a") == []


def test_legacy_open_sender_may_deploy(h, monkeypatch, caplog):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "")
    _online(h)
    h.connect("old-portal")
    with caplog.at_level(logging.WARNING):
        answer = _call("old-portal", "cfg-1")
    assert answer["status"] == "delivered"
    assert "legacy-open" in caplog.text


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def test_offline_targets(h):
    h.connect_sender()
    assert _call("sender-1", "cfg-1", target="PC_NONE") == {"status": "offline", "config_id": "cfg-1",
                                                           "confirmable": False}
    # A PC whose session was replaced is reachable only through the new one.
    token = _online(h)
    h.connect("pc-a2")
    h.register("pc-a2", "PC_A", token)
    answer = _call("sender-1", "cfg-2")
    assert answer["status"] == "delivered"
    assert _config_emits(h, "pc-a") == [] and len(_config_emits(h, "pc-a2")) == 1
    assert relay.relay_counters["deploys_offline"] == 1
    # A presence entry without a live registered session (a disconnect in
    # progress) is offline too: nothing is emitted to a ghost sid.
    relay.connected_pcs["PC_GHOST"] = "ghost-sid"
    assert _call("sender-1", "cfg-3", target="PC_GHOST")["status"] == "offline"
    assert _config_emits(h, "ghost-sid") == []


def test_delivered_layout_reaches_the_pc_unchanged_with_a_callback(h):
    _online(h)
    h.connect_sender()
    content = _layout("cfg-1")
    answer = relay.deploy_config("sender-1", {"config_id": "cfg-1", "targetId": "PC_A", "content": content})
    assert answer == {"status": "delivered", "config_id": "cfg-1", "confirmable": False}
    [(data, callback)] = _config_emits(h, "pc-a")
    assert data == {"type": "config", "content": content, "from": "portal"}  # what a 1.1.x Client reads
    assert callable(callback)
    assert relay.relay_counters["deploys_forwarded"] == 1
    # No other session got the layout.
    assert [target for name, _d, target, _cb in h.emits if name == "config"] == ["pc-a"]


def test_a_1_1_x_ack_marks_received_and_stamps_last_applied(h):
    _online(h)  # 1.1.x register payload: no capabilities
    h.connect_sender()
    config_id = str(uuid.uuid4())
    _row(h, config_id)
    assert _call("sender-1", config_id)["confirmable"] is False
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback()  # python-socketio calls it with the Client's ACK args (none for 1.1.x)
    row = _get(h, config_id)
    assert row.status == "received" and row.received_at and row.delivered_at and row.confirmable is False
    assert _pc(h, "PC_A").last_applied == row.received_at
    assert relay.relay_counters["deploy_receipts"] == 1


def test_confirmable_needs_capability_feature_and_root_config_id(h, monkeypatch):
    _enable_deploy_result(monkeypatch)
    h.connect_sender()
    _online(h, "PC_A", "pc-a", capabilities=[sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS])
    _online(h, "PC_B", "pc-b")  # 1.1.x: no capabilities
    _online(h, "PC_C", "pc-c", capabilities=[sp.FEATURE_DEPLOY_RESULT, "bad capability!"])  # malformed list
    assert relay.session_data["pc-a"]["features"] == [sp.FEATURE_DEPLOY_RESULT]
    assert relay.session_confirmable("pc-a") and not relay.session_confirmable("pc-b")
    assert not relay.session_confirmable("pc-c")
    assert _call("sender-1", "cfg-a", "PC_A")["confirmable"] is True
    assert _call("sender-1", "cfg-a2", "PC_A", content=_layout())["confirmable"] is False  # no root id
    assert _call("sender-1", "cfg-b", "PC_B")["confirmable"] is False
    assert _call("sender-1", "cfg-c", "PC_C")["confirmable"] is False


def test_confirmable_needs_the_feature_advertised_at_register(h, monkeypatch):
    # The station declares the capability, but this relay never advertised it.
    _online(h, capabilities=[sp.FEATURE_DEPLOY_RESULT])
    h.connect_sender()
    assert relay.session_data["pc-a"]["features"] == []
    assert _call("sender-1", "cfg-1")["confirmable"] is False
    # Reports switched off: not advertised either.
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", False)
    _enable_deploy_result(monkeypatch)
    _online(h, "PC_B", "pc-b", capabilities=[sp.FEATURE_DEPLOY_RESULT])
    assert relay.session_data["pc-b"]["features"] == [sp.FEATURE_STATION_REPORTS_DISABLED]
    assert _call("sender-1", "cfg-2", "PC_B")["confirmable"] is False


def test_a_confirmable_ack_marks_received_without_stamping(h, monkeypatch):
    _enable_deploy_result(monkeypatch)
    _online(h, capabilities=[sp.FEATURE_DEPLOY_RESULT])
    h.connect_sender()
    _row(h, "cfg-1")
    assert _call("sender-1", "cfg-1")["confirmable"] is True
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback({"anything": "the Client says"})
    row = _get(h, "cfg-1")
    assert row.status == "received" and row.confirmable is True
    assert _pc(h, "PC_A").last_applied is None  # D15 stamps it on applied


def test_ack_of_a_superseded_row_changes_nothing(h):
    _online(h)
    h.connect_sender()
    _row(h, "cfg-old", status="superseded")
    _call("sender-1", "cfg-old")
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback()
    assert _get(h, "cfg-old").status == "superseded"
    assert _pc(h, "PC_A").last_applied is None
    assert len(relay.pending_deploy_events) == 0


def test_ack_for_a_row_of_another_pc_is_ignored(h, caplog):
    _online(h)
    h.add_pc("PC_B")
    h.connect_sender()
    _row(h, "cfg-b", pc_id="PC_B")
    _call("sender-1", "cfg-b", target="PC_A")
    [(_data, callback)] = _config_emits(h, "pc-a")
    with caplog.at_level(logging.WARNING):
        callback()
    assert _get(h, "cfg-b").status == "pending"
    assert _pc(h, "PC_A").last_applied is None and _pc(h, "PC_B").last_applied is None
    assert "does not match" in caplog.text and len(relay.pending_deploy_events) == 0


# --------------------------------------------------------------------------- #
# Events that arrive early, database errors
# --------------------------------------------------------------------------- #


def test_an_ack_that_arrives_before_its_row_is_kept_and_applied(h):
    _online(h)
    h.connect_sender()
    _call("sender-1", "cfg-early")
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback()
    assert _get(h, "cfg-early") is None and len(relay.pending_deploy_events) == 1
    relay.retry_pending_deploy_events()
    assert len(relay.pending_deploy_events) == 1  # still no row
    _row(h, "cfg-early")
    relay.retry_pending_deploy_events()
    assert _get(h, "cfg-early").status == "received" and len(relay.pending_deploy_events) == 0
    assert _pc(h, "PC_A").last_applied is not None


def test_a_database_error_keeps_the_receipt_for_retry(h, caplog):
    _online(h)
    h.connect_sender()
    _row(h, "cfg-1")
    _call("sender-1", "cfg-1")
    [(_data, callback)] = _config_emits(h, "pc-a")
    h.db_fails = True
    with caplog.at_level(logging.ERROR):
        callback()
        relay.retry_pending_deploy_events()  # still failing: kept
    assert len(relay.pending_deploy_events) == 1 and "kept for retry" in caplog.text
    h.db_fails = False
    relay.retry_pending_deploy_events()
    assert _get(h, "cfg-1").status == "received" and len(relay.pending_deploy_events) == 0


def test_the_ack_callback_spawns_the_write(h, monkeypatch):
    """The callback runs in the socket's read loop: the DB write goes to a
    background task, never inline."""
    spawned = []
    monkeypatch.setattr(relay, "_spawn", lambda fn, *args: spawned.append((fn, args)))
    _online(h)
    h.connect_sender()
    _row(h, "cfg-1")
    _call("sender-1", "cfg-1")
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback()
    assert _get(h, "cfg-1").status == "pending"
    [(fn, args)] = spawned
    assert fn is relay._record_receipt and args[:3] == ("cfg-1", "PC_A", False)
    fn(*args)
    assert _get(h, "cfg-1").status == "received"


def test_run_blocking_uses_tpool_only_under_eventlet(monkeypatch):
    assert relay._run_blocking(lambda a, b: a + b, 2, 3) == 5  # threading mode: inline
    import sys
    import types

    calls = []
    fake_eventlet = types.ModuleType("eventlet")
    fake_tpool = types.ModuleType("eventlet.tpool")
    fake_tpool.execute = lambda fn, *args: calls.append(fn) or fn(*args)
    fake_eventlet.tpool = fake_tpool
    monkeypatch.setitem(sys.modules, "eventlet", fake_eventlet)
    monkeypatch.setitem(sys.modules, "eventlet.tpool", fake_tpool)
    monkeypatch.setattr(relay, "ASYNC_MODE", "eventlet")
    assert relay._run_blocking(lambda: 7) == 7 and len(calls) == 1


# --------------------------------------------------------------------------- #
# Register keeps what confirmable needs; nothing leaks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "caps,expected",
    [
        (None, []),
        ("deploy_result.v1", []),
        ([], []),
        (["deploy_result.v1", "deploy_result.v1"], ["deploy_result.v1"]),
        (["a"] * 17, []),
        (["x" * 33], []),
        ([1], []),
        (["ok", "not ok"], []),
        (["deploy_result.v1", "station_status.v1", "pc_token.v1"],
         ["deploy_result.v1", "station_status.v1", "pc_token.v1"]),
    ],
)
def test_parse_capabilities(caps, expected):
    data = {} if caps is None else {"capabilities": caps}
    assert relay.parse_capabilities(data) == expected


def test_registered_payload_is_unchanged_for_1_1_x(h):
    _online(h)
    [payload] = h.received("pc-a", "registered")
    assert set(payload) == {"pc_id", "message", "features", "protocol_hash"}
    sp.validate("RegisteredV1", payload)


def test_the_layout_is_never_logged(h, caplog):
    _online(h)
    h.connect_sender()
    _row(h, "cfg-1")
    with caplog.at_level(logging.DEBUG):
        _call("sender-1", "cfg-1")
        [(_data, callback)] = _config_emits(h, "pc-a")
        callback()
    assert MARKER not in caplog.text
    assert "cfg-1" in caplog.text  # ids are logged
