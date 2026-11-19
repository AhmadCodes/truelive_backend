"""
Relay side of stream providers (LUM-SRV): the ``refresh_config`` sender event
(delivered, offline, not a sender, the ACK key, no pc_deployments row),
``track_ack`` on a deploy, and the connect and failure-report triggers with
the failure trigger's 60 s limit. Handlers are called directly
(tests/relay_testkit.py) with a FakeRedis; the Celery queue is recorded.

    python -m pytest tests/unit/test_relay_stream_providers.py -o addopts='' -p no:cacheprovider
"""

import logging
import secrets
import time
import uuid

import pytest
import redis

from app.models.pc_tracking import PcDeployment
from tests.fake_redis import FakeRedis
from tests.relay_testkit import RelayHarness, import_relay

relay = import_relay()

MARKER = "relay-sp-marker-" + secrets.token_hex(4)
ACK = "truelive:sp:ack:PC_A"


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "y" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    monkeypatch.setattr(relay.settings, "STREAM_PROVIDERS_ENABLED", True)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    harness.fake_redis = FakeRedis()
    harness.redis = redis.Redis.from_url(harness.fake_redis.url)
    monkeypatch.setattr(relay, "_provider_redis_client", harness.redis)
    harness.tasks = []
    monkeypatch.setattr(relay.catchup, "send_task",
                        lambda name, args, countdown=None: harness.tasks.append((name, args)) or "task-1")
    relay._provider_queue.clear()
    relay.provider_breaker.reset()
    monkeypatch.setattr(relay, "_has_provider_cameras", lambda: True)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()
    relay.pending_deploy_events.clear()
    relay._provider_queue.clear()
    harness.redis.close()
    harness.fake_redis.stop()


def _online(h, pc_id="PC_A", sid="pc-a", **extra):
    token = h.add_pc(pc_id)
    h.connect(sid)
    h.register(sid, pc_id, token, **extra)
    assert h.received(sid, "registered")


def _layout(config_id="cfg-1"):
    return {"config_id": config_id, "screens": [{"id": "s1", "title": MARKER, "source_groups": []}]}


def _config_emits(h, sid):
    return [(data, callback) for event, data, room, callback in h.emits if event == "config" and room == sid]


def _request(refresh_id=None, **overrides):
    return {"refresh_id": refresh_id or str(uuid.uuid4()), "targetId": "PC_A", "content": _layout(), **overrides}


# --------------------------------------------------------------------------- #
# refresh_config
# --------------------------------------------------------------------------- #


def test_refresh_is_delivered_as_config_and_is_not_a_deploy(h, caplog):
    caplog.set_level(logging.DEBUG)
    _online(h)
    h.connect_sender()
    request = _request("refresh-1")
    assert relay.refresh_config("sender-1", request) == {"status": "delivered"}
    [(data, callback)] = _config_emits(h, "pc-a")
    assert data == {"type": "config", "content": request["content"], "from": "portal"}  # as a deploy looks
    assert data["content"]["config_id"] == "cfg-1"  # the config_id of the PC's deploy, unchanged
    db = h.Session()
    try:
        assert db.query(PcDeployment).count() == 0  # no deploy row, so no history line
    finally:
        db.close()
    assert h.audits == []
    assert relay.relay_counters["provider_refresh_forwarded"] == 1 and relay.relay_counters["deploys_forwarded"] == 0
    assert h.redis.get(ACK) is None
    callback()  # the station's Socket.IO ACK
    assert h.redis.get(ACK) == b"refresh-1"
    assert 29 * 86400 < h.fake_redis.ttl(ACK) <= 30 * 86400
    assert MARKER not in caplog.text  # the layout is never logged


def test_refresh_to_an_offline_pc(h):
    h.add_pc("PC_A")
    h.connect_sender()
    assert relay.refresh_config("sender-1", _request()) == {"status": "offline"}
    assert relay.relay_counters["provider_refresh_offline"] == 1 and not _config_emits(h, "pc-a")


def test_refresh_is_for_senders_only(h):
    _online(h)
    _online(h, "PC_B", "pc-b")
    h.connect("stranger")
    for sid in ("pc-b", "stranger", "nobody"):
        assert relay.refresh_config(sid, _request()) == {"status": "error", "reason": "not authorized"}
    assert _config_emits(h, "pc-a") == []


@pytest.mark.parametrize("bad,reason", [
    (None, "invalid request"),
    ({"refresh_id": "", "targetId": "PC_A", "content": {}}, "invalid refresh_id"),
    ({"refresh_id": "x" * 65, "targetId": "PC_A", "content": {}}, "invalid refresh_id"),
    ({"refresh_id": "r1", "targetId": "", "content": {}}, "invalid targetId"),
    ({"refresh_id": "r1", "targetId": "PC_A", "content": "layout"}, "invalid content"),
])
def test_refresh_validates_its_request(h, bad, reason):
    _online(h)
    h.connect_sender()
    assert relay.refresh_config("sender-1", bad) == {"status": "error", "reason": reason}
    assert _config_emits(h, "pc-a") == []


def test_ack_write_failure_is_only_logged(h):
    _online(h)
    h.connect_sender()
    relay.refresh_config("sender-1", _request("refresh-2"))
    h.fake_redis.stop()
    [(_data, callback)] = _config_emits(h, "pc-a")
    callback()  # must not raise into the hub


# --------------------------------------------------------------------------- #
# track_ack on a deploy
# --------------------------------------------------------------------------- #


def _deploy(h, config_id, **extra):
    db = h.Session()
    try:
        db.add(PcDeployment(id=config_id, pc_id="PC_A", trigger="pc_deploy", status="pending"))
        db.commit()
    finally:
        db.close()
    return relay.deploy_config("sender-1", {"config_id": config_id, "targetId": "PC_A",
                                           "content": _layout(config_id), **extra})


def test_track_ack_writes_the_ack_key_with_the_config_id(h):
    _online(h)
    h.connect_sender()
    assert _deploy(h, "cfg-plain")["status"] == "delivered"
    _config_emits(h, "pc-a")[-1][1]()
    # A deploy without provider cameras writes nothing to Redis.
    assert not any(name == "SET" for name, _key in h.fake_redis.commands)
    assert _deploy(h, "cfg-tracked", track_ack=True)["status"] == "delivered"
    _config_emits(h, "pc-a")[-1][1]()
    assert h.redis.get(ACK) == b"cfg-tracked"
    db = h.Session()
    try:
        assert db.get(PcDeployment, "cfg-tracked").status == "received"  # the deploy path is unchanged
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


def _status(failed=None):
    wall = {"running_config_id": "cfg-1", "screens": 1, "streams_total": 4, "streams_playing": 4 - len(failed or [])}
    if failed:
        wall["streams_failed"] = failed
    return {"message_id": "m-" + secrets.token_hex(6), "ts": time.time(), "health": "ok", "wall": wall}


def test_register_queues_the_connect_trigger_only_for_provider_stations(h):
    _online(h)
    assert list(relay._provider_queue) == [("connect", "PC_A")]
    assert relay.process_provider_triggers_once() == 1
    assert h.tasks == []  # no record: this PC has no provider camera, nothing is queued
    h.redis.set("truelive:sp:pc:PC_A", "record")
    h.register("pc-a", "PC_A", h.pc_row("PC_A").auth_token)  # a reconnect
    relay.process_provider_triggers_once()
    assert h.tasks == [("app.tasks.stream_provider_tasks.refresh_pc", ["PC_A", "connect"])]
    assert relay.relay_counters["provider_triggers_queued"] == 1


def test_failure_report_queues_the_trigger_once_per_minute(h):
    _online(h)
    relay._provider_queue.clear()
    h.redis.set("truelive:sp:pc:PC_A", "record")
    assert relay.station_status("pc-a", _status())["status"] == "ok"
    assert not relay._provider_queue  # nothing failed
    assert relay.station_status("pc-a", _status(["src-0", "src-9"]))["status"] == "ok"
    relay.process_provider_triggers_once()
    assert h.tasks == [("app.tasks.stream_provider_tasks.station_failed", ["PC_A", ["src-0", "src-9"]])]
    # A broken camera reports every few seconds: one task per 60 s per PC.
    for _ in range(5):
        relay.station_status("pc-a", _status(["src-0"]))
        relay.process_provider_triggers_once()
    assert len(h.tasks) == 1
    assert 55 < h.fake_redis.ttl("truelive:sp:rl:fail:PC_A") <= 60
    h.redis.delete("truelive:sp:rl:fail:PC_A")  # the minute is over
    relay.station_status("pc-a", _status(["src-0"]))
    relay.process_provider_triggers_once()
    assert len(h.tasks) == 2


def test_failure_report_of_a_station_without_provider_cameras_queues_nothing(h):
    _online(h)
    relay.station_status("pc-a", _status(["src-0"]))
    relay.process_provider_triggers_once()
    assert h.tasks == [] and not h.redis.exists("truelive:sp:rl:fail:PC_A")


def test_trigger_of_a_session_that_is_gone_is_dropped(h):
    _online(h)
    h.redis.set("truelive:sp:pc:PC_A", "record")
    relay.disconnect("pc-a")
    relay.process_provider_triggers_once()
    assert h.tasks == []


def test_redis_outage_pauses_the_triggers_and_never_raises(h):
    _online(h)
    h.fake_redis.stop()
    h.redis.close()
    relay._provider_redis_client = redis.Redis.from_url(h.fake_redis.url, socket_connect_timeout=0.5,
                                                        socket_timeout=0.5)
    assert relay.process_provider_triggers_once() == 1
    assert relay.relay_counters["provider_trigger_errors"] == 1 and relay.provider_breaker.is_open()
    relay.schedule_provider_trigger("connect", "pc-a", "PC_A")
    relay.process_provider_triggers_once()
    assert relay.relay_counters["provider_trigger_errors"] == 1 and h.tasks == []  # paused: no second timeout


def test_kill_switch_queues_nothing(h, monkeypatch):
    monkeypatch.setattr(relay.settings, "STREAM_PROVIDERS_ENABLED", False)
    _online(h)
    relay.station_status("pc-a", _status(["src-0"]))
    assert not relay._provider_queue


def test_without_any_provider_camera_the_triggers_touch_nothing(h, monkeypatch):
    monkeypatch.setattr(relay, "_has_provider_cameras", lambda: False)
    h.redis.set("truelive:sp:pc:PC_A", "record")
    h.fake_redis.commands.clear()
    _online(h)
    relay.station_status("pc-a", _status(["src-0"]))
    assert relay.process_provider_triggers_once() == 2
    assert h.tasks == [] and h.fake_redis.commands == []
