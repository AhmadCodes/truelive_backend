"""
Relay ingestion of station reports (plan 06 D15; C23, E20, NEW-44, NEW-52).

Card tests run here with the relay's handlers called directly
(tests/relay_testkit.py: emits recorded, background tasks inline, get_db on
SQLite holding pcs, pc_deployments and pc_health):

* "handlers in isolation": only the PC's current session; D1 validation,
  64 KB bound, credentials rejected; dedupe by (pc_id, message_id);
  monotonic transitions, late upgrades of unconfirmed, applied -> failed;
  applied stamps last_applied; mismatched rows rejected; an unknown id updates
  only running_config_id; RELAY_STATION_REPORTS=off answers disabled.
* "unknown-event regression": an event with no handler gets no ACK (the
  caller times out), so a Client never mistakes silence for an answer.
* test_relay_allow_list (C23): only allow-listed pc_health columns are kept.
* test_relay_register_versions (E20): versions checked one by one, at most 16
  capabilities of 32 characters, pc_id from the session.
* test_presence_sweep_keeps_updated_at (NEW-52).
* The health buffer and its flush: one batched upsert per interval.

The Postgres half (health flush on Postgres, relay lifecycle over sockets,
reconnect storm) is tests/integration/test_relay_reports_pg.py.

    python -m pytest tests/unit/test_relay_reports.py -o addopts='' -p no:cacheprovider
"""

import json
import logging
import time
from datetime import datetime, timezone

import pytest

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.schemas import station_protocol as sp
from app.services import station_reports
from tests.relay_testkit import RelayHarness, import_relay, make_pc_token

relay = import_relay()

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "z" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)
    relay.enable_feature(sp.FEATURE_STATION_STATUS)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()
    relay.pending_deploy_events.clear()
    relay.health_buffer.clear()
    relay.report_ids.clear()


CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
_mids = iter(range(10**6))


def _mid():
    return f"m-{next(_mids)}"


def _online(h, pc_id="PC_A", sid="pc-a", token="mint", **extra):
    token = h.add_pc(pc_id) if token == "mint" else token
    h.connect(sid)
    h.register(sid, pc_id, token, **{"capabilities": CAPS, **extra})
    assert relay._registered_pc(sid) == pc_id, h.errors(sid)
    return token


def _row(h, config_id, pc_id="PC_A", status="delivered", **fields):
    db = h.Session()
    try:
        db.add(PcDeployment(id=config_id, pc_id=pc_id, trigger="pc_deploy", status=status, **fields))
        db.commit()
    finally:
        db.close()


def _get(h, model, key):
    db = h.Session()
    try:
        return db.get(model, key)
    finally:
        db.close()


def _result(config_id, status, message_id=None, **extra):
    return {"message_id": message_id or _mid(), "config_id": config_id, "status": status, "ts": time.time(), **extra}


def _status(message_id=None, **extra):
    return {"message_id": message_id or _mid(), "ts": time.time(), "health": "ok", **extra}


def _valid_ack(ack):
    sp.validate("ReportAckV1", ack)
    return ack


# --------------------------------------------------------------------------- #
# Who may report
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", ["unregistered", "stale", "wrong_type", "deleted"])
@pytest.mark.parametrize("handler", ["deploy_result", "station_status"])
def test_reports_need_the_current_registered_session(h, case, handler):
    token = h.add_pc("PC_A")
    _row(h, "cfg-1")
    h.connect("pc-a")
    if case == "stale":
        h.register("pc-a", "PC_A", token)
        h.connect("pc-a2")
        h.register("pc-a2", "PC_A", token)
    elif case == "wrong_type":
        h.register("pc-a", "PC_A", make_pc_token("PC_A", h.secret, token_type="access"))
    elif case == "deleted":
        h.register("pc-a", "PC_GONE", make_pc_token("PC_GONE", h.secret))
    payload = _result("cfg-1", "applied") if handler == "deploy_result" else _status()
    ack = getattr(relay, handler)("pc-a", payload)
    assert _valid_ack(ack) == {"status": "rejected", "reason": "not registered"}
    assert _get(h, PcDeployment, "cfg-1").status == "delivered"
    assert "health" not in (relay.health_buffer.peek("PC_A") or {})
    assert _get(h, PC, "PC_A").last_applied is None


def test_reports_off_answer_disabled(h, monkeypatch):
    _online(h)
    _row(h, "cfg-1")
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", False)
    for sid in ("pc-a", "nobody"):
        assert _valid_ack(relay.deploy_result(sid, _result("cfg-1", "applied"))) == {"status": "disabled"}
        assert _valid_ack(relay.station_status(sid, _status())) == {"status": "disabled"}
    assert _get(h, PcDeployment, "cfg-1").status == "delivered"
    assert "health" not in (relay.health_buffer.peek("PC_A") or {})
    assert relay.advertised_features() == [sp.FEATURE_STATION_REPORTS_DISABLED]


def test_report_features_are_advertised_with_their_handlers(h):
    _online(h)
    [payload] = h.received("pc-a", "registered")
    assert payload["features"] == [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
    for event in ("deploy_result", "station_status"):
        assert event in relay.sio.handlers["/"]


def test_the_module_enables_the_report_features_at_import():
    # No harness here: this is the module's own set, as a real relay starts.
    assert {sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS} <= relay._enabled_features


# --------------------------------------------------------------------------- #
# deploy_result
# --------------------------------------------------------------------------- #


def test_full_lifecycle_for_a_current_client(h):
    _online(h, client_version="1.4.0")
    _row(h, "cfg-1", status="received", confirmable=True)
    assert _valid_ack(relay.deploy_result("pc-a", _result("cfg-1", "accepted", "m-acc")))["status"] == "ok"
    row = _get(h, PcDeployment, "cfg-1")
    assert row.status == "accepted" and row.accepted_at
    assert _get(h, PC, "PC_A").last_applied is None
    ack = relay.deploy_result("pc-a", _result("cfg-1", "applied", "m-app", mode="live", persisted=True, source="stdout",
                                              core_version="1.1.0+d53952a", stage="done"))
    assert _valid_ack(ack) == {"status": "ok", "message_id": "m-app"}
    row = _get(h, PcDeployment, "cfg-1")
    assert (row.status, row.mode, row.used_fallback, row.persisted, row.source, row.core_version,
            row.client_version, row.confirmable) == ("applied", "live", False, True, "stdout", "1.1.0+d53952a",
                                                     "1.4.0", True)
    assert row.status_detail == "stage done"
    assert _get(h, PC, "PC_A").last_applied == row.applied_at
    assert relay.health_buffer.peek("PC_A")["running_config_id"] == "cfg-1"
    # A late Failed turns applied into failed (OD-18 (b)).
    relay.deploy_result("pc-a", _result("cfg-1", "failed", detail="Core exited during the deploy"))
    row = _get(h, PcDeployment, "cfg-1")
    assert row.status == "failed" and row.failed_at >= row.applied_at
    assert row.status_detail == "Core exited during the deploy"


def test_delivered_from_the_client_counts_as_receipt(h):
    _online(h)
    _row(h, "cfg-1", status="delivered", confirmable=True)
    relay.deploy_result("pc-a", _result("cfg-1", "delivered"))
    row = _get(h, PcDeployment, "cfg-1")
    assert row.status == "received" and row.received_at
    assert _get(h, PC, "PC_A").last_applied is None  # confirmable: stamped on applied only


def test_late_events_upgrade_unconfirmed(h):
    _online(h)
    _row(h, "cfg-1", status="unconfirmed", confirmable=True)
    relay.deploy_result("pc-a", _result("cfg-1", "applied"))
    assert _get(h, PcDeployment, "cfg-1").status == "applied"


def test_transitions_are_monotonic(h):
    _online(h)
    _row(h, "cfg-1", status="applied", confirmable=True, applied_at=1_800_000_000)
    for status in ("accepted", "delivered", "unconfirmed", "superseded"):
        assert relay.deploy_result("pc-a", _result("cfg-1", status))["status"] == "ok"
        assert _get(h, PcDeployment, "cfg-1").status == "applied", status
    _row(h, "cfg-2", status="failed", confirmable=True)
    relay.deploy_result("pc-a", _result("cfg-2", "applied"))
    assert _get(h, PcDeployment, "cfg-2").status == "failed"
    # A stale "applied" does not claim the wall runs it (register left it None).
    assert relay.health_buffer.peek("PC_A")["running_config_id"] is None


def test_client_superseded_report(h):
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=True)
    relay.deploy_result("pc-a", _result("cfg-1", "superseded"))
    assert _get(h, PcDeployment, "cfg-1").status == "superseded"


def test_duplicates_are_answered_and_not_reapplied(h):
    _online(h)
    _online(h, "PC_B", "pc-b")
    _row(h, "cfg-1", status="received", confirmable=True)
    _row(h, "cfg-b", pc_id="PC_B", status="received", confirmable=True)
    first = relay.deploy_result("pc-a", _result("cfg-1", "accepted", "same-id"))
    assert first == {"status": "ok", "message_id": "same-id"}
    # The row moves on; a resend of the old report must not touch it.
    relay.deploy_result("pc-a", _result("cfg-1", "failed"))
    assert _valid_ack(relay.deploy_result("pc-a", _result("cfg-1", "accepted", "same-id"))) == {
        "status": "duplicate", "message_id": "same-id"}
    assert _get(h, PcDeployment, "cfg-1").status == "failed"
    # message_ids are per PC.
    assert relay.deploy_result("pc-b", _result("cfg-b", "accepted", "same-id"))["status"] == "ok"
    assert _valid_ack(relay.station_status("pc-a", _status("s-1")))["status"] == "ok"
    assert relay.station_status("pc-a", _status("s-1"))["status"] == "duplicate"


def test_dedup_keeps_the_last_10000():
    lru = station_reports.MessageIdLRU(size=3)
    for i in range(5):
        lru.add(("PC_A", f"m{i}"))
    assert len(lru) == 3 and ("PC_A", "m0") not in lru and ("PC_A", "m4") in lru
    assert station_reports.DEDUP_SIZE == 10_000


def test_a_deploy_of_another_pc_is_rejected(h):
    _online(h)
    h.add_pc("PC_B")
    _row(h, "cfg-b", pc_id="PC_B", status="received", confirmable=True)
    ack = relay.deploy_result("pc-a", _result("cfg-b", "applied", "m-x"))
    assert _valid_ack(ack) == {"status": "rejected", "message_id": "m-x", "reason": "config_id belongs to another PC"}
    assert _get(h, PcDeployment, "cfg-b").status == "received"
    assert _get(h, PC, "PC_B").last_applied is None


def test_an_unknown_id_updates_only_running_config_id(h):
    _online(h)
    ack = relay.deploy_result("pc-a", _result("client-3f2b8c1e-0000-4000-8000-000000000001", "applied"))
    assert ack["status"] == "ok"
    assert relay.health_buffer.peek("PC_A")["running_config_id"] == "client-3f2b8c1e-0000-4000-8000-000000000001"
    assert len(relay.pending_deploy_events) == 0  # Client-minted ids never get a row
    assert _get(h, PC, "PC_A").last_applied is None
    db = h.Session()
    assert db.query(PcDeployment).count() == 0
    db.close()
    # A backend-looking id is kept in case its row is still being committed.
    relay.deploy_result("pc-a", _result("0f0e8d9c-0000-4000-8000-000000000009", "accepted"))
    assert len(relay.pending_deploy_events) == 1
    _row(h, "0f0e8d9c-0000-4000-8000-000000000009", status="received", confirmable=True)
    relay.retry_pending_deploy_events()
    assert _get(h, PcDeployment, "0f0e8d9c-0000-4000-8000-000000000009").status == "accepted"


def test_a_database_error_keeps_the_result(h):
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=True)
    h.db_fails = True
    assert relay.deploy_result("pc-a", _result("cfg-1", "applied"))["status"] == "ok"
    assert len(relay.pending_deploy_events) == 1
    h.db_fails = False
    relay.retry_pending_deploy_events()
    assert _get(h, PcDeployment, "cfg-1").status == "applied"
    assert _get(h, PC, "PC_A").last_applied is not None


def _credential():
    return "rtsp://" + "fixture-user" + ":" + "fixture-pw" + "@192.0.2.7/stream"


@pytest.mark.parametrize(
    "build,reason",
    [
        (lambda: "not an object", "report is not an object"),
        (lambda: {"config_id": "cfg-1", "status": "applied", "ts": time.time()}, "invalid report"),
        (lambda: _result("cfg-1", "deployed"), "invalid report"),
        (lambda: _result("cfg-1", "applied", detail="failed at " + _credential()), "invalid report"),
        (lambda: _result("cfg-1", "applied", extra_field={"url": _credential()}), "invalid report"),
        (lambda: _result("cfg-1", "applied", config={"screens": []}), "invalid report"),
        (lambda: _result("cfg-1", "applied", auth_token="x"), "invalid report"),
        (lambda: _result("c" * 65, "applied"), "invalid report"),
        (lambda: _result("cfg-1", "applied", ts=time.time() * 1000), "invalid report"),
        (lambda: _result("cfg-1", "applied", detail="x" * 501), "invalid report"),
        (lambda: _result("cfg-1", "applied", padding="x" * (64 * 1024)), "report too large"),
    ],
    ids=["not-dict", "no-message-id", "bad-status", "userinfo-detail", "userinfo-extra", "config-key",
         "token-key", "long-id", "ms-ts", "long-detail", "too-large"],
)
def test_invalid_deploy_results_are_rejected(h, caplog, build, reason):
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=True)
    with caplog.at_level(logging.DEBUG):
        ack = relay.deploy_result("pc-a", build())
    assert _valid_ack(ack)["status"] == "rejected" and ack["reason"] == reason
    assert _get(h, PcDeployment, "cfg-1").status == "received"
    assert "fixture-pw" not in caplog.text and "xxxxxxxxxx" not in caplog.text


@pytest.mark.parametrize("count,ok", [(50, True), (51, False)])
def test_station_status_caps_failed_ids(h, count, ok):
    _online(h)
    wall = {"streams_failed": [f"src-{i}" for i in range(count)], "streams_failed_count": 80}
    ack = relay.station_status("pc-a", _status(wall=wall))
    assert (ack["status"] == "ok") is ok
    if ok:
        row = relay.health_buffer.peek("PC_A")
        assert len(row["streams_failed"]) == 50 and row["streams_failed_count"] == 80


def test_station_status_is_buffered_with_relay_flags(h):
    outdated = make_pc_token("PC_A", h.secret, exp_delta=4000 * 3600)
    h.add_pc("PC_A")
    _online(h, token=outdated)  # differs from the stored token: token_outdated
    assert relay.session_data["pc-a"]["status_seen"] is False
    relay.health_buffer.clear()  # the register row was flushed: the status must carry the flags itself
    ack = relay.station_status("pc-a", _status("st-1", health="degraded", reasons=["no_video"],
                                               core={"state": "RUNNING", "pid": 4242, "uptime_sec": 60},
                                               telemetry={"mode": "stdout_only", "tap_state": "ok"},
                                               wall={"running_config_id": "cfg-9", "screens": 2,
                                                     "streams_total": 8, "streams_playing": 6}))
    assert _valid_ack(ack) == {"status": "ok", "message_id": "st-1"}
    row = relay.health_buffer.peek("PC_A")
    assert (row["health"], row["reasons"], row["core_state"], row["core_pid"], row["running_config_id"],
            row["streams_playing"], row["telemetry_mode"], row["last_message_id"]) == (
        "degraded", ["no_video"], "RUNNING", 4242, "cfg-9", 6, "stdout_only", "st-1")
    assert row["relay_flags"] == {"token_outdated": True}
    assert relay.session_data["pc-a"]["status_seen"] is True
    # Flags the relay changes later (D30 clears token_outdated) follow at the next status.
    relay.session_data["pc-a"]["flags"] = {}
    relay.station_status("pc-a", _status())
    assert relay.health_buffer.peek("PC_A")["relay_flags"] == {}
    assert relay.station_status("pc-a", _status(health="bogus"))["status"] == "rejected"


def test_report_db_work_runs_off_the_hub(h, monkeypatch):
    """NEW-44: every relay database call goes through _run_blocking
    (eventlet.tpool in production): register, reports, flush, sweeps."""
    calls = []
    real = relay._run_blocking
    monkeypatch.setattr(relay, "_run_blocking", lambda fn, *a: calls.append(fn.__name__) or real(fn, *a))
    _online(h)
    assert calls == ["_authorize_pc"]
    _row(h, "cfg-1", status="received", confirmable=True)
    relay.deploy_result("pc-a", _result("cfg-1", "applied"))
    relay.flush_health_once()
    relay.sweep_deploys_once()
    relay.connected_pcs["PC_A"] = "pc-a"
    import itertools

    sleeps = itertools.count()

    class Stop(Exception):
        pass

    def one_tick(seconds):
        if next(sleeps):
            raise Stop

    monkeypatch.setattr(relay.sio, "sleep", one_tick)
    with pytest.raises(Stop):
        relay._last_seen_sweep(30)
    assert calls == ["_authorize_pc", "_apply_and_commit", "write", "_apply_and_commit", "presence_sweep_once"]


def test_a_reporting_station_is_confirmable(h):
    """A row the relay could not mark confirmable (it learned the capability
    later) still becomes confirmable, and applied stamps last_applied."""
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=False)
    relay.deploy_result("pc-a", _result("cfg-1", "applied"))
    row = _get(h, PcDeployment, "cfg-1")
    assert (row.status, row.confirmable) == ("applied", True)
    assert _get(h, PC, "PC_A").last_applied == row.applied_at


# --------------------------------------------------------------------------- #
# Unknown events
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("event", ["deploy_result_v2", "get_station_config_v9", "station_reports", "nonexistent"])
def test_unknown_event_regression(h, event):
    """python-socketio sends an ACK only when a handler ran; for an event the
    relay does not serve, the caller's ``call`` times out (D3/D9: a timeout
    is transient, never an answer)."""
    _online(h)
    assert relay.sio._trigger_event(event, "/", "pc-a", {"message_id": "m"}) is relay.sio.not_handled


def test_the_relay_serves_exactly_these_events():
    assert set(relay.sio.handlers["/"]) == {"connect", "disconnect", "register", "message", "get_clients",
                                             "deploy_config", "deploy_result", "station_status"}
    assert "*" not in relay.sio.handlers["/"]


# --------------------------------------------------------------------------- #
# C23 allow-list, E20 versions
# --------------------------------------------------------------------------- #


def test_relay_allow_list(h):
    _online(h)
    url = "http://192.0.2.9:8080/core/logs?key=abc"
    payload = _status(
        "st-allow",
        core={"state": "RUNNING", "pid": 1, "build_class": "reports_v1", "exe_sha256": "a" * 64, "restarts_24h": 2,
              "last_exit": "0x544C0002", "uptime_sec": 5, "extra_core": "dropped"},
        telemetry={"mode": "listener", "tap_state": "lossy",
                   "channels": {"heartbeat": {"target": "listener", "connected": True, "last_message_age_sec": 3,
                                              "secret_extra": "dropped"}}},
        wall={"running_config_id": "cfg-1", "deploying": False, "screens": 1, "streams_total": 4,
              "streams_playing": 4, "streams_connecting": 0, "streams_failed": [], "streams_failed_count": 0,
              "wall_extra": "dropped"},
        flags={"protocol_hash_mismatch": True},
        readiness=[{"code": "AUTOLOGON_OFF", "detail": f"see {url}"}],
        config_status={"core_config_mode": "managed", "mode_set_by": "fleet", "core_revision": "rev-1",
                       "core_version": "1.2.0", "core_caps": ["report_fanout"], "migration_status": "done",
                       "drift_key_names": ["heartbeat_address"], "legacy_available": True,
                       "last_config_error": f"rejected by {url}", "data_dir_acl_warning": False,
                       "fleet_errors_count": 1, "values": {"heartbeat_address": "dropped"}},
        top_level_extra={"anything": "dropped"},
        screen_layout_note="dropped",
    )
    assert relay.station_status("pc-a", payload)["status"] == "ok"
    row = relay.health_buffer.peek("PC_A")
    assert set(row) <= station_reports.HEALTH_WRITABLE
    text = json.dumps(row)
    assert "dropped" not in text and url not in text and "key=abc" not in text
    assert row["last_config_error"] == "rejected by <url>" and row["readiness"] == [
        {"code": "AUTOLOGON_OFF", "detail": "see <url>"}]
    assert row["telemetry_channels"] == {"heartbeat": {"target": "listener", "connected": True,
                                                       "last_message_age_sec": 3}}
    assert (row["core_config_mode"], row["config_core_version"], row["core_caps"], row["drift_key_names"],
            row["core_build_class"], row["core_exe_sha256"], row["core_last_exit"]) == (
        "managed", "1.2.0", ["report_fanout"], ["heartbeat_address"], "reports_v1", "a" * 64, "0x544C0002")
    # Every column the relay can write exists on pc_health, and nothing else.
    assert station_reports.HEALTH_WRITABLE == {c.name for c in PcHealth.__table__.columns} - {
        "pc_id", "created_at", "updated_at"}
    with pytest.raises(ValueError):
        relay.health_buffer.put("PC_A", {"rtsp_url": "x"})


def test_relay_register_versions(h, caplog):
    h.add_pc("PC_A")
    token = make_pc_token("PC_A", h.secret)
    good = {"client_version": "1.4.0", "client_build": "1.4.0+0000000", "core_version": "1.1.0+d53952a",
            "core_installed_version": "1.1.0+d53952a", "core_running_version": "1.1.0+d53952a",
            "core_version_source": "stdout", "core_exe_sha256": "b" * 64, "core_exe_size": 2867200,
            "core_build_class": "reports_v1", "delivery_policy": "live_with_fallback", "telemetry_mode": "stdout_only",
            "running_config_id": "cfg-0"}
    h.connect("pc-a")
    relay.register("pc-a", {"pc_id": "PC_EVIL", "auth_token": token, "capabilities": CAPS, **good})
    assert relay._registered_pc("pc-a") == "PC_A"  # pc_id from the token/session, never the payload
    assert relay.health_buffer.peek("PC_EVIL") is None
    row = relay.health_buffer.peek("PC_A")
    for field, value in good.items():
        assert row[station_reports.REGISTER_FIELDS[field][0]] == value, field
    assert row["capabilities"] == CAPS and row["registered_at"] and row["relay_flags"] == {}

    bad = {"client_version": "1.4.0 beta", "client_build": "x" * 65, "core_version": "1.1;rm",
           "core_exe_size": True, "core_build_class": "fancy", "delivery_policy": "both",
           "core_exe_sha256": "B" * 64, "running_config_id": "has space", "telemetry_mode": "a" * 33}
    h.connect("pc-a2")
    with caplog.at_level(logging.WARNING):
        relay.register("pc-a2", {"pc_id": "PC_A", "auth_token": token, "capabilities": ["c"] * 17, **bad})
    assert relay._registered_pc("pc-a2") == "PC_A"  # still registers
    row = relay.health_buffer.peek("PC_A")
    for field in bad:
        assert row[station_reports.REGISTER_FIELDS[field][0]] is None, field
    assert row["capabilities"] == [] and relay.session_data["pc-a2"]["capabilities"] == []
    assert "ignored invalid field(s)" in caplog.text and "capabilities" in caplog.text
    for value in ("1.4.0 beta", "1.1;rm", "fancy"):
        assert value not in caplog.text  # names only, never values
    # A capability of 33 characters is refused too.
    assert station_reports.parse_capabilities({"capabilities": ["x" * 33]}) is None
    assert station_reports.parse_capabilities({"capabilities": ["x" * 32]}) == ["x" * 32]


def test_a_1_1_x_register_clears_newer_versions(h):
    token = _online(h, client_version="1.4.0")
    assert relay.health_buffer.peek("PC_A")["client_version"] == "1.4.0"
    h.connect("pc-old")
    relay.register("pc-old", {"pc_id": "PC_A", "auth_token": token})  # Client 1.1.x
    row = relay.health_buffer.peek("PC_A")
    assert row["client_version"] is None and row["capabilities"] == []
    assert row["relay_flags"] == {"duplicate_registration": True}


# --------------------------------------------------------------------------- #
# NEW-52: presence leaves updated_at alone
# --------------------------------------------------------------------------- #


def _set_updated_at(h, pc_id, value):
    db = h.Session()
    try:
        db.execute(PC.__table__.update().where(PC.__table__.c.id == pc_id).values(updated_at=value))
        db.commit()
    finally:
        db.close()


def _updated_at(h, pc_id):
    row = _get(h, PC, pc_id)
    value = row.updated_at
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def test_presence_sweep_keeps_updated_at(h):
    token = h.add_pc("PC_A")
    _set_updated_at(h, "PC_A", OLD)
    h.connect("pc-a")
    h.register("pc-a", "PC_A", token)
    row = _get(h, PC, "PC_A")
    assert row.last_connected and row.last_seen
    assert _updated_at(h, "PC_A") == OLD  # the register stamp is presence, not an edit

    _set_updated_at(h, "PC_A", OLD)
    db = h.Session()
    db.execute(PC.__table__.update().values(last_seen=1))
    db.commit()
    db.close()
    _set_updated_at(h, "PC_A", OLD)
    relay.presence_sweep_once(["PC_A"])
    assert _get(h, PC, "PC_A").last_seen > 1
    assert _updated_at(h, "PC_A") == OLD

    # Sanity: an ORM edit of pcs still bumps updated_at, so the checks above can fail.
    db = h.Session()
    pc = db.get(PC, "PC_A")
    pc.name = "renamed"
    db.commit()
    db.close()
    assert _updated_at(h, "PC_A") != OLD


def test_status_stamps_keep_updated_at(h):
    """last_applied (a 1.1.x receipt, an applied report) is a status stamp of
    the PC, not an edit: updated_at, shown with updated_by as provenance,
    stays."""
    _online(h)
    _row(h, "cfg-111", status="delivered", confirmable=False)
    _row(h, "cfg-140", status="received", confirmable=True)
    _set_updated_at(h, "PC_A", OLD)
    relay._record_receipt("cfg-111", "PC_A", False, 1_700_000_000, 1_700_000_001)
    assert _get(h, PC, "PC_A").last_applied == 1_700_000_001
    assert _updated_at(h, "PC_A") == OLD
    relay.deploy_result("pc-a", _result("cfg-140", "applied"))
    assert _get(h, PC, "PC_A").last_applied > 1_700_000_001
    assert _updated_at(h, "PC_A") == OLD


def test_the_presence_loop_uses_the_sweep(h, monkeypatch):
    calls = []

    class Stop(Exception):
        pass

    def fake_sleep(seconds):
        if len(calls) >= 2:
            raise Stop
        calls.append(("sleep", seconds))

    monkeypatch.setattr(relay.sio, "sleep", fake_sleep)
    monkeypatch.setattr(relay, "presence_sweep_once", lambda ids: calls.append(("sweep", tuple(ids))))
    relay.connected_pcs["PC_A"] = "pc-a"
    with pytest.raises(Stop):
        relay._last_seen_sweep(30)
    assert calls == [("sleep", 30), ("sweep", ("PC_A",))]


# --------------------------------------------------------------------------- #
# The health buffer and its flush
# --------------------------------------------------------------------------- #


def _health(h, pc_id):
    return _get(h, PcHealth, pc_id)


def test_many_reports_make_one_health_batch(h, monkeypatch):
    _online(h)
    _online(h, "PC_B", "pc-b")
    for i in range(20):
        relay.station_status("pc-a", _status(health="ok" if i % 2 else "degraded", wall={"streams_playing": i}))
    relay.station_status("pc-b", _status(health="stopped"))
    statements = []
    real = station_reports.upsert_health
    monkeypatch.setattr(station_reports, "upsert_health", lambda db, batch: statements.append(dict(batch))
                        or real(db, batch))
    assert relay.flush_health_once() == 2
    assert len(statements) == 1 and relay.relay_counters["health_batches"] == 1
    assert (_health(h, "PC_A").health, _health(h, "PC_A").streams_playing) == ("ok", 19)  # the latest wins
    assert _health(h, "PC_B").health == "stopped"
    assert _health(h, "PC_A").registered_at and _health(h, "PC_A").capabilities == CAPS
    assert relay.flush_health_once() == 0  # nothing new: no batch
    assert relay.relay_counters["health_batches"] == 1


def test_flush_merges_and_keeps_register_versions(h):
    _online(h, client_version="1.4.0", core_running_version="1.1.0+d53952a")
    relay.flush_health_once()
    relay.station_status("pc-a", _status(health="ok"))  # carries no versions
    relay.flush_health_once()
    row = _health(h, "PC_A")
    assert (row.health, row.client_version, row.core_running_version) == ("ok", "1.4.0", "1.1.0+d53952a")
    relay.station_status("pc-a", _status(health="degraded", client_version="1.4.1"))
    relay.flush_health_once()
    row = _health(h, "PC_A")
    assert (row.health, row.client_version) == ("degraded", "1.4.1")


def test_flush_drops_rows_of_deleted_pcs(h):
    _online(h)
    _online(h, "PC_B", "pc-b")
    db = h.Session()
    db.query(PC).filter(PC.id == "PC_B").delete()
    db.commit()
    db.close()
    relay.station_status("pc-b", _status())
    assert relay.flush_health_once() == 1
    assert _health(h, "PC_A") is not None and _health(h, "PC_B") is None


def test_a_failed_flush_is_retried_and_newer_values_win(h):
    _online(h)
    relay.station_status("pc-a", _status(health="degraded"))
    h.db_fails = True
    assert relay.flush_health_once() == 0
    assert len(relay.health_buffer) == 1
    relay.station_status("pc-a", _status(health="ok"))
    h.db_fails = False
    assert relay.flush_health_once() == 1
    assert _health(h, "PC_A").health == "ok"


def test_background_loops_use_their_intervals(h, monkeypatch):
    assert relay.HEALTH_FLUSH_SECONDS == 15 and relay.DEPLOY_SWEEP_SECONDS == 30
    assert relay.DEPLOY_EVENT_RETRY_SECONDS == 5

    class Stop(Exception):
        pass

    for loop, interval, target in ((relay._health_flush_loop, 15, "flush_health_once"),
                                   (relay._deploy_sweep_loop, 30, "sweep_deploys_once")):
        calls = []

        def fake_sleep(seconds, calls=calls):
            if len(calls) >= 4:
                raise Stop
            calls.append(("sleep", seconds))

        def failing_work(calls=calls):
            calls.append("work")
            raise RuntimeError("database unavailable (simulated)")  # the loop must survive it

        monkeypatch.setattr(relay.sio, "sleep", fake_sleep)
        monkeypatch.setattr(relay, target, failing_work)
        with pytest.raises(Stop):
            loop(interval)
        assert calls == [("sleep", interval), "work", ("sleep", interval), "work"], loop.__name__


def test_rejection_logs_name_fields_not_values(h, caplog):
    _online(h)
    secret_key = "k3y-" + "q" * 12
    with caplog.at_level(logging.WARNING):
        ack = relay.station_status("pc-a", _status(flags={secret_key + " not a token": True}, health="nope"))
    assert ack["status"] == "rejected"
    assert secret_key not in caplog.text and "nope" not in caplog.text
    assert "health" in caplog.text and "<key>" in caplog.text
