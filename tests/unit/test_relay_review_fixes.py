"""
Relay regressions found by the M2 review (server track), handlers called
directly (tests/relay_testkit.py: emits recorded, background tasks inline,
get_db on SQLite).

* Register races (D15, NEW-44): register checks the database off the hub, so
  the session can close, or a newer registration of the same PC can finish,
  while the check runs. Neither may leave a ghost in connected_pcs or demote
  the live session.
* A registration the database could not check (D12, OD-23): under
  RELAY_REQUIRE_CURRENT_TOKEN=true it fails closed and completes by itself
  once the database answers; under the default it stays fail-open
  (db_unverified), gets no fleet config, and is checked again (flag cleared,
  or the session unregistered when the PC was deleted).
* Station free text (D1, D15): NUL and other control characters are removed
  before storage, and a URL with credentials in an error message is replaced
  by <url> instead of the whole report being rejected.
* The database refusing data (D15): one PC's health row or one deploy event
  no longer blocks every other one; a deploy_result the database refuses is
  answered "rejected", not "ok".
* The relay accepts production-sized polling POSTs (D14).

    python -m pytest tests/unit/test_relay_review_fixes.py -o addopts='' -p no:cacheprovider
"""

import logging
import time

import pytest
from sqlalchemy.exc import OperationalError

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.schemas import station_protocol as sp
from app.services import core_station_config, deploy_service, station_reports
from tests.relay_testkit import RelayHarness, import_relay, make_pc_token

relay = import_relay()

CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
_mids = iter(range(10 ** 6))


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "q" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", False)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)
    relay.enable_feature(sp.FEATURE_STATION_STATUS)
    relay.enable_feature(sp.FEATURE_STATION_CONFIG)
    relay.enable_feature(sp.FEATURE_PC_TOKEN)
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()
    relay.pending_deploy_events.clear()
    relay.health_buffer.clear()
    relay.report_ids.clear()


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


def _connected_pcs_endpoint():
    """What the relay's /api/connected-pcs (read by /pcs/status) answers."""
    import json

    captured = {}
    body = relay.app.get_connected_pcs({"REQUEST_METHOD": "GET"},
                                       lambda status, headers: captured.setdefault("status", status))
    assert captured["status"].startswith("200")
    return json.loads(b"".join(body))


def _slow_check(monkeypatch, during):
    """Make register's database check run ``during()`` while it "waits" (the
    hub serves other events during the real tpool call)."""
    real = relay._run_blocking
    state = {"armed": True}

    def run_blocking(fn, *args):
        if fn is relay._authorize_pc and state["armed"]:
            state["armed"] = False
            during()
        return real(fn, *args)

    monkeypatch.setattr(relay, "_run_blocking", run_blocking)


# --------------------------------------------------------------------------- #
# Register races (M2 review: the check runs off the hub)
# --------------------------------------------------------------------------- #


def test_a_session_that_closes_during_its_register_check_is_not_registered(h, monkeypatch, caplog):
    token = h.add_pc("PC_G")
    h.connect("pc-g")
    _slow_check(monkeypatch, lambda: relay.disconnect("pc-g"))
    with caplog.at_level(logging.INFO):
        h.register("pc-g", "PC_G", token)
    assert relay.connected_pcs == {}
    assert "pc-g" not in relay.session_data
    assert _connected_pcs_endpoint() == {"connected_pcs": [], "total_connected": 0}
    assert h.received("pc-g", "registered") == []
    assert relay.health_buffer.peek("PC_G") is None
    assert relay.relay_counters["stale_registrations"] == 1
    assert "closed while its registration was checked" in caplog.text
    # The presence sweep has nothing to stamp for it.
    assert list(relay.connected_pcs) == []


def test_a_dying_duplicate_does_not_demote_the_live_session(h, monkeypatch):
    token = _online(h, "PC_L", "pc-live")
    h.connect("pc-dying")
    _slow_check(monkeypatch, lambda: relay.disconnect("pc-dying"))
    h.register("pc-dying", "PC_L", token)
    assert relay.connected_pcs == {"PC_L": "pc-live"}
    assert relay._registered_pc("pc-live") == "PC_L"
    assert relay.session_data["pc-live"]["flags"] == {}
    # The live session still gets deploys and its reports are accepted.
    h.connect_sender()
    answer = relay.deploy_config("sender-1", {"config_id": "cfg-1", "targetId": "PC_L",
                                              "content": {"config_id": "cfg-1", "screens": []}})
    assert answer["status"] == "delivered"
    ack = relay.station_status("pc-live", {"message_id": _mid(), "ts": time.time(), "health": "ok"})
    assert ack["status"] == "ok"


def test_an_older_register_that_finishes_last_does_not_take_over(h, monkeypatch, caplog):
    """Session A starts its check first; session B registers the same PC and
    finishes while A's check still runs. A's later result must not replace B."""
    token = h.add_pc("PC_A")
    h.connect("pc-a-old")
    h.connect("pc-a-new")
    _slow_check(monkeypatch, lambda: h.register("pc-a-new", "PC_A", token, capabilities=CAPS))
    with caplog.at_level(logging.WARNING):
        h.register("pc-a-old", "PC_A", token, capabilities=CAPS)
    assert relay.connected_pcs == {"PC_A": "pc-a-new"}
    assert relay._registered_pc("pc-a-new") == "PC_A" and relay._registered_pc("pc-a-old") is None
    assert relay.session_data["pc-a-new"]["flags"] == {}  # nobody took over from it
    assert h.received("pc-a-old", "registered") == []
    assert relay.relay_counters["stale_registrations"] == 1
    assert "finished first" in caplog.text
    # A register that starts later than the current one still takes over (NEW-43).
    h.register("pc-a-old", "PC_A", token, capabilities=CAPS)
    assert relay.connected_pcs == {"PC_A": "pc-a-old"}
    assert relay.session_data["pc-a-old"]["flags"] == {relay.FLAG_DUPLICATE: True}


def test_register_order_numbers_increase(h):
    token = h.add_pc("PC_A")
    h.connect("s1")
    h.connect("s2")
    h.register("s1", "PC_A", token)
    h.register("s2", "PC_A", token)
    assert relay.session_data["s2"]["register_seq"] > relay.session_data["s1"]["register_seq"]


# --------------------------------------------------------------------------- #
# Database errors at register (D12, OD-23)
# --------------------------------------------------------------------------- #


def test_require_current_fails_closed_on_a_database_error_and_completes_later(h, monkeypatch, caplog):
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.db_fails = True
    with caplog.at_level(logging.INFO):
        h.register("pc-a", "PC_A", token, capabilities=CAPS)
    assert relay._registered_pc("pc-a") is None and relay.connected_pcs == {}
    assert h.errors("pc-a") == [relay.MSG_DB_UNAVAILABLE]
    assert relay.relay_counters["deferred_registrations"] == 1
    # Deploys and reports find no registered session.
    h.connect_sender()
    assert relay.deploy_config("sender-1", {"config_id": "c1", "targetId": "PC_A", "content": {}})["status"] == \
        "offline"
    # Still down: the retry is quiet (no second tray message) and stays pending.
    assert relay.reverify_sessions_once() == 0
    assert h.errors("pc-a") == [relay.MSG_DB_UNAVAILABLE]
    assert relay.session_data["pc-a"]["register_retry"] is not None
    # The database answers: the registration completes by itself.
    h.db_fails = False
    assert relay.reverify_sessions_once() == 1
    assert relay._registered_pc("pc-a") == "PC_A"
    assert len(h.received("pc-a", "registered")) == 1
    assert relay.session_data["pc-a"]["flags"] == {}
    assert relay.session_data["pc-a"].get("register_retry") is None
    assert relay.reverify_sessions_once() == 0  # nothing left to do
    assert token not in caplog.text


def test_require_current_never_registers_a_replaced_token_during_a_database_error(h, monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    stored = h.add_pc("PC_A")
    replaced = make_pc_token("PC_A", h.secret, exp_delta=4000 * 3600)
    assert replaced != stored
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", replaced)
    assert relay._registered_pc("pc-a") is None
    h.db_fails = False
    relay.reverify_sessions_once()
    assert relay._registered_pc("pc-a") is None
    assert h.errors("pc-a") == [relay.MSG_DB_UNAVAILABLE, relay.MSG_TOKEN_REPLACED]
    assert relay.session_data["pc-a"].get("register_retry") is None
    relay.reverify_sessions_once()
    assert h.errors("pc-a") == [relay.MSG_DB_UNAVAILABLE, relay.MSG_TOKEN_REPLACED]  # not retried again


def test_a_deferred_registration_yields_to_one_that_succeeded_meanwhile(h, monkeypatch):
    """The station reconnected while its first session (refused for the
    database) lingered: the retry of the old session must not take over."""
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    token = h.add_pc("PC_A")
    h.connect("pc-a-old")
    h.db_fails = True
    h.register("pc-a-old", "PC_A", token)
    h.db_fails = False
    h.connect("pc-a-new")
    h.register("pc-a-new", "PC_A", token)
    assert relay.connected_pcs == {"PC_A": "pc-a-new"}
    relay.reverify_sessions_once()
    assert relay.connected_pcs == {"PC_A": "pc-a-new"}
    assert relay.session_data["pc-a-new"]["flags"] == {}
    assert relay.session_data["pc-a-old"].get("register_retry") is None
    assert h.received("pc-a-old", "registered") == []


def test_a_new_register_replaces_a_pending_retry(h, monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", token)
    h.db_fails = False
    relay.register("pc-a", {"pc_id": "PC_A"})  # a broken register: refused at once
    assert relay.session_data["pc-a"].get("register_retry") is None
    assert relay.reverify_sessions_once() == 0
    assert relay._registered_pc("pc-a") is None


def test_unverified_session_gets_no_station_config_until_checked(h, monkeypatch):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", token, capabilities=CAPS)
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_DB_UNVERIFIED: True}

    served = core_station_config.StationConfig(values={"core.heartbeat_interval_sec": 0}, errors=(),
                                               revision="r" * 64)
    monkeypatch.setattr(relay.station_config_cache, "get", lambda loader: served)
    refused = relay.get_station_config("pc-a", {})
    assert (refused["status"], refused["reason"], refused["values"]) == ("rejected", "not verified", {})
    h.db_fails = False
    assert relay.reverify_sessions_once() == 1
    assert relay.session_data["pc-a"]["flags"] == {}
    assert relay.get_station_config("pc-a", {})["status"] == "ok"


def test_unverified_session_is_checked_again_and_its_flag_cleared(h, caplog):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", token, capabilities=CAPS)
    relay.health_buffer.clear()
    # Still down: nothing changes, and the pass stops at the first error.
    calls = []
    real = relay._authorize_pc
    relay._authorize_pc = lambda *a: calls.append(a[0]) or real(*a)
    try:
        h.add_pc("PC_B")
        h.connect("pc-b")
        h.register("pc-b", "PC_B", make_pc_token("PC_B", h.secret))
        calls.clear()
        assert relay.reverify_sessions_once() == 0
        assert len(calls) == 1
    finally:
        relay._authorize_pc = real
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_DB_UNVERIFIED: True}
    h.db_fails = False
    with caplog.at_level(logging.INFO):
        assert relay.reverify_sessions_once() == 2
    for sid, pc_id in (("pc-a", "PC_A"), ("pc-b", "PC_B")):
        assert relay._registered_pc(sid) == pc_id
        assert relay.session_data[sid]["flags"] == {}
        assert relay.session_data[sid]["reverify"] is None
        # pc_health.relay_flags follows at the next flush.
        assert relay.health_buffer.peek(pc_id)["relay_flags"] == {}
    assert _get(h, PC, "PC_A").last_seen is not None  # the check stamped presence
    assert "checked against the database after registering without a check" in caplog.text
    assert token not in caplog.text
    assert relay.reverify_sessions_once() == 0


def test_reverify_flags_an_outdated_token(h):
    h.add_pc("PC_A")
    other = make_pc_token("PC_A", h.secret, exp_delta=4000 * 3600)
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", other)
    h.db_fails = False
    relay.reverify_sessions_once()
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_TOKEN_OUTDATED: True}
    assert relay._registered_pc("pc-a") == "PC_A"  # OD-23 (a): still registered


def test_reverify_unregisters_a_deleted_pc(h):
    """A deleted PC's token registered fail-open during the outage: once the
    database answers, the session is no longer that PC (D12: deleted PCs
    cannot produce accepted reports)."""
    token = make_pc_token("PC_GONE", h.secret)
    h.connect("pc-gone")
    h.db_fails = True
    h.register("pc-gone", "PC_GONE", token)
    assert relay._registered_pc("pc-gone") == "PC_GONE"
    h.db_fails = False
    assert relay.reverify_sessions_once() == 1
    assert relay._registered_pc("pc-gone") is None and relay.connected_pcs == {}
    assert h.errors("pc-gone") == [relay.MSG_UNKNOWN_PC]
    ack = relay.station_status("pc-gone", {"message_id": _mid(), "ts": time.time(), "health": "ok"})
    assert ack == {"status": "rejected", "reason": "not registered"}


def test_reverify_skips_a_session_replaced_meanwhile(h):
    token = h.add_pc("PC_A")
    h.connect("pc-a")
    h.db_fails = True
    h.register("pc-a", "PC_A", token)
    h.db_fails = False
    h.connect("pc-a2")
    h.register("pc-a2", "PC_A", token)  # takes over, checked
    assert relay.reverify_sessions_once() == 0
    assert relay.connected_pcs == {"PC_A": "pc-a2"}
    assert relay.session_data["pc-a"]["reverify"] is None


def test_the_start_line_names_the_mode():
    import inspect

    source = inspect.getsource(relay.run_server)
    assert "Relay registration: db_error=" in source and "_reverify_loop" in source


# --------------------------------------------------------------------------- #
# Station free text (D1, D15)
# --------------------------------------------------------------------------- #


def _credential():
    return "rtsp://" + "fixture-user" + ":" + "fixture-pw" + "@192.0.2.7/stream"


def test_nul_and_control_characters_in_station_text_are_removed(h):
    _online(h)
    status = {"message_id": _mid(), "ts": time.time(), "health": "degraded",
              "config_status": {"last_config_error": "parse error at byte \x00\x07 near \x1b[31m"},
              "readiness": [{"code": "AUTOLOGON_OFF", "detail": "off\x00\tline2\nline3"}]}
    assert relay.station_status("pc-a", status)["status"] == "ok"
    row = relay.health_buffer.peek("PC_A")
    assert row["last_config_error"] == "parse error at byte  near [31m"
    assert row["readiness"] == [{"code": "AUTOLOGON_OFF", "detail": "off\tline2\nline3"}]

    _row(h, "cfg-1", status="applied", confirmable=True)
    ack = relay.deploy_result("pc-a", {"message_id": _mid(), "config_id": "cfg-1", "status": "failed",
                                       "detail": "Core said \x00", "ts": time.time()})
    assert ack["status"] == "ok"
    row = _get(h, PcDeployment, "cfg-1")
    assert (row.status, row.status_detail) == ("failed", "Core said ")


def test_credentials_in_free_text_are_replaced_not_rejected(h, caplog):
    """D15: "userinfo stripped". A Core error that quotes a camera URL with
    its password keeps the report (a real failure shows Failed, not
    Unconfirmed after 300 s) and never stores or logs the credential."""
    _online(h)
    _row(h, "cfg-1", status="accepted", confirmable=True)
    with caplog.at_level(logging.DEBUG):
        ack = relay.deploy_result("pc-a", {"message_id": _mid(), "config_id": "cfg-1", "status": "failed",
                                           "detail": "open failed: " + _credential() + " (401)", "ts": time.time()})
        status = relay.station_status("pc-a", {
            "message_id": _mid(), "ts": time.time(), "health": "unhealthy",
            "config_status": {"last_config_error": "bad source " + _credential()},
            "readiness": [{"code": "CAMERA_AUTH", "detail": _credential()}]})
    assert ack["status"] == "ok" and status["status"] == "ok"
    row = _get(h, PcDeployment, "cfg-1")
    assert (row.status, row.status_detail) == ("failed", "open failed: <url> (401)")
    health = relay.health_buffer.peek("PC_A")
    assert health["last_config_error"] == "bad source <url>"
    assert health["readiness"] == [{"code": "CAMERA_AUTH", "detail": "<url>"}]
    assert "fixture-pw" not in caplog.text and "fixture-pw" not in repr(health)
    # A credential outside the free-text fields still rejects the report (D1).
    rejected = relay.station_status("pc-a", {"message_id": _mid(), "ts": time.time(), "health": "ok",
                                             "extra": {"url": _credential()}})
    assert rejected["status"] == "rejected"


def test_scrub_report_text_only_touches_the_free_text_fields():
    data = {"message_id": "m", "config_id": "c", "status": "failed", "ts": 1.0, "mode": "live",
            "detail": "x " + _credential(), "other": "y " + _credential()}
    out = station_reports.scrub_report_text("DeployResultV1", data)
    assert out["detail"] == "x <url>" and out["other"] == data["other"]
    assert data["detail"].endswith("stream")  # the input is not changed
    assert station_reports.scrub_report_text("StationStatusV1", {"readiness": "not a list"}) == {
        "readiness": "not a list"}
    assert station_reports.scrub_report_text("StationStatusV1", {"config_status": None}) == {"config_status": None}
    assert station_reports.scrub_report_text("DeployResultV1", {"detail": 5}) == {"detail": 5}
    assert station_reports.clean_text("a\x00b\x7fc\ud800d") == "abc\ufffdd"
    assert station_reports.clean_value({"k\x00": ["v\x01", ("t\x02",), 3]}) == {"k": ["v", ("t",), 3]}


# --------------------------------------------------------------------------- #
# The database refusing data (D15)
# --------------------------------------------------------------------------- #


def test_one_refused_health_row_does_not_keep_the_others_out(h, monkeypatch, caplog):
    for pc_id in ("PC_A", "PC_B", "PC_BAD"):
        h.add_pc(pc_id)
        relay.health_buffer.put(pc_id, {"health": "ok", "received_at": 1})
    real = station_reports.upsert_health

    def upsert(db, batch):
        if "PC_BAD" in batch:
            raise ValueError("A string literal cannot contain NUL (0x00) characters.")
        return real(db, batch)

    monkeypatch.setattr(station_reports, "upsert_health", upsert)
    with caplog.at_level(logging.ERROR):
        assert relay.flush_health_once() == 2
    assert len(relay.health_buffer) == 0  # the refused row is dropped, not retried for ever
    db = h.Session()
    try:
        from app.models.pc_tracking import PcHealth

        assert sorted(r.pc_id for r in db.query(PcHealth).all()) == ["PC_A", "PC_B"]
    finally:
        db.close()
    assert "refused the row(s) of PC(s) PC_BAD (ValueError); dropped" in caplog.text
    assert relay.relay_counters["health_rows_refused"] == 1


def test_an_unavailable_database_keeps_the_whole_health_batch(h, monkeypatch):
    for pc_id in ("PC_A", "PC_B"):
        h.add_pc(pc_id)
        relay.health_buffer.put(pc_id, {"health": "ok"})

    def upsert(db, batch):
        raise OperationalError("INSERT", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(station_reports, "upsert_health", upsert)
    assert relay.flush_health_once() == 0
    assert len(relay.health_buffer) == 2
    monkeypatch.undo()


def test_health_rows_with_nul_reach_the_database_clean(h):
    """End to end on SQLite: HealthBuffer.put cleans every string."""
    _online(h)
    relay.station_status("pc-a", {"message_id": _mid(), "ts": time.time(), "health": "ok",
                                  "config_status": {"last_config_error": "e\x00"}})
    relay.flush_health_once()
    from app.models.pc_tracking import PcHealth

    row = _get(h, PcHealth, "PC_A")
    assert row.last_config_error == "e"
    # The buffer cleans whatever is put into it, whichever path filled it.
    relay.health_buffer.put("PC_A", {"last_config_error": "a\x00b", "reasons": ["r\x00"],
                                     "relay_flags": {"k\x00": True}})
    assert relay.health_buffer.peek("PC_A") == {"last_config_error": "ab", "reasons": ["r"],
                                                "relay_flags": {"k": True}}


def test_a_deploy_result_the_database_refuses_is_rejected(h, monkeypatch):
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=True)

    def refuse(apply):
        raise ValueError("A string literal cannot contain NUL (0x00) characters.")

    monkeypatch.setattr(relay, "_apply_and_commit", refuse)
    ack = relay.deploy_result("pc-a", {"message_id": "m-x", "config_id": "cfg-1", "status": "applied",
                                       "ts": time.time()})
    assert ack == {"status": "rejected", "message_id": "m-x", "reason": "could not be stored"}
    assert len(relay.pending_deploy_events) == 0
    # The same message may be sent again (it was not recorded as seen).
    monkeypatch.undo()
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    assert relay.deploy_result("pc-a", {"message_id": "m-x", "config_id": "cfg-1", "status": "applied",
                                        "ts": time.time()})["status"] == "ok"


def test_a_deploy_result_hit_by_a_database_outage_is_kept(h, monkeypatch):
    _online(h)
    _row(h, "cfg-1", status="received", confirmable=True)

    def down(apply):
        raise OperationalError("UPDATE", {}, Exception("could not connect to server"))

    monkeypatch.setattr(relay, "_apply_and_commit", down)
    ack = relay.deploy_result("pc-a", {"message_id": _mid(), "config_id": "cfg-1", "status": "applied",
                                       "ts": time.time()})
    assert ack["status"] == "ok" and len(relay.pending_deploy_events) == 1


# --------------------------------------------------------------------------- #
# Production-sized deploys (D14)
# --------------------------------------------------------------------------- #


def test_the_relay_accepts_production_sized_polling_posts():
    assert relay.sio.eio.max_http_buffer_size == deploy_service.RELAY_MAX_HTTP_BUFFER_BYTES == 4_000_000
    assert relay._make_server(False, "threading").eio.max_http_buffer_size == 4_000_000
    assert deploy_service.SENDER_ROUND_BYTES < 1_000_000


def test_config_ids_with_a_trailing_newline_are_refused(h):
    _online(h)
    h.connect_sender()
    answer = relay.deploy_config("sender-1", {"config_id": "cfg-1\n", "targetId": "PC_A", "content": {}})
    assert (answer["status"], answer.get("reason")) == ("error", "invalid config_id")
    assert "message_id" not in relay._ack("ok", "m-1\n")
