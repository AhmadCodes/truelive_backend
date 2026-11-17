"""
Catch-up re-push (plan 06 D17; OD-20, NEW-21). Flag off in M2.

Card tests: "decision table", plus the relay hooks and the worker side; the
real relay, a real Celery worker and a broker outage are in
tests/integration/test_catchup_pg.py ("reconnect catch-up", "broker down").

The relay handlers run in-process (tests/relay_testkit.py, SQLite with pcs,
pc_deployments and pc_health); the Redis key and the Celery queue are
replaced by recorders here.

    python -m pytest tests/unit/test_catchup.py -o addopts='' -p no:cacheprovider
"""

import socket
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.schemas import station_protocol as sp
from app.services import catchup, deploy_tracking
from tests.deploy_testkit import AuditRecorder, FakeSender, create_deploy_tables, patch_generator, seed_fleet, \
    sqlite_engine, synthetic_layout
from tests.fake_redis import FakeRedis
from tests.relay_testkit import RelayHarness, import_relay

relay = import_relay()

CONFIRMABLE_CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]
LAYOUT = "LAYOUT_cu"


# --------------------------------------------------------------------------- #
# Decision table
# --------------------------------------------------------------------------- #


def _row(status, **fields):
    values = dict(id="cfg-1", status=status, layout_id=LAYOUT, config_hash="h" * 64, received_at=None,
                  accepted_at=None, applied_at=None, failed_at=None)
    values.update(fields)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("status,push", [
    ("offline", True), ("relay_failed", True), ("unconfirmed", True),
    ("pending", False), ("delivered", False), ("received", False), ("accepted", False), ("applied", False),
    ("failed", False), ("superseded", False),
])
def test_decision_table_by_state(status, push):
    decision = catchup.decide(_row(status))
    assert decision.push is push, decision.reason
    if push:
        assert (decision.config_id, decision.layout_id, decision.config_hash) == ("cfg-1", LAYOUT, "h" * 64)


@pytest.mark.parametrize("evidence", ["received_at", "accepted_at", "applied_at", "failed_at"])
@pytest.mark.parametrize("status", ["offline", "relay_failed", "unconfirmed"])
def test_decision_table_any_receipt_means_no_push(status, evidence):
    """'unconfirmed with no receipt' (a late ACK stamps received_at, D14)."""
    assert catchup.decide(_row(status, **{evidence: 1_800_000_000})).push is False


def test_decision_table_other_rules():
    assert catchup.decide(None).reason == "no deploy"
    assert catchup.decide(_row("offline", layout_id=None)).push is False  # the layout was deleted
    assert catchup.decide(_row("offline"), running_config_id="cfg-1").push is False  # the wall runs it
    assert catchup.decide(_row("offline"), running_config_id="other").push is True
    assert catchup.decide(_row("offline"), running_config_id="").push is True  # a legacy layout on the wall


# --------------------------------------------------------------------------- #
# Relay hooks
# --------------------------------------------------------------------------- #


class Recorder:
    """Stands in for the Redis key and the Celery queue."""

    def __init__(self, allow=True, fail=None, delay=0.0):
        self.allow, self.fail, self.delay = allow, fail, delay
        self.acquired, self.queued = [], []

    def acquire(self, pc_id):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        self.acquired.append(pc_id)
        return self.allow

    def enqueue(self, pc_id, config_id, config_hash):
        self.queued.append((pc_id, config_id, config_hash))
        return "task-" + uuid.uuid4().hex[:8]


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "x" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", True)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)
    relay.enable_feature(sp.FEATURE_STATION_STATUS)
    rec = Recorder()
    monkeypatch.setattr(relay, "_catchup_limiter", rec)
    monkeypatch.setattr(catchup, "enqueue", rec.enqueue)
    harness.rec = rec
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()


def _pc(h, pc_id="PC_A", layout=LAYOUT, latest="offline", **row):
    token = h.add_pc(pc_id, screen_layout_id=layout)
    if latest:
        db = h.Session()
        try:
            db.add(PcDeployment(id=row.pop("id", f"{pc_id}-cfg"), pc_id=pc_id, layout_id=LAYOUT, trigger="pc_deploy",
                                status=latest, config_hash="c" * 64, **row))
            db.commit()
        finally:
            db.close()
    return token


def _register(h, sid, pc_id, token, caps=None):
    h.connect(sid)
    extra = {"capabilities": caps} if caps is not None else {}
    h.register(sid, pc_id, token, **extra)
    assert relay._registered_pc(sid) == pc_id, h.errors(sid)


def _status(running="__absent__", message_id=None):
    data = {"message_id": message_id or uuid.uuid4().hex, "ts": time.time(), "health": "ok"}
    if running != "__absent__":
        data["wall"] = {"running_config_id": running}
    return data


def test_flag_off_does_nothing_at_all(h, monkeypatch):
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", False)
    token_old = _pc(h, "PC_OLD")
    token_new = _pc(h, "PC_NEW")
    _register(h, "pc-old", "PC_OLD", token_old)
    _register(h, "pc-new", "PC_NEW", token_new, CONFIRMABLE_CAPS)
    assert relay.station_status("pc-new", _status(""))["status"] == "ok"
    assert len(relay._catchup_queue) == 0 and relay.process_catchup_once() == 0
    assert h.rec.acquired == [] and h.rec.queued == []
    assert relay.schedule_catchup("pc-old", "PC_OLD", catchup.AT_REGISTER) is False


def test_legacy_client_is_checked_at_register_after_registered(h):
    token = _pc(h, "PC_OLD", latest="offline")
    _register(h, "pc-old", "PC_OLD", token)
    assert h.received("pc-old", "registered")  # registered went out; the work is deferred
    assert list(relay._catchup_queue) == ["PC_OLD"] and h.rec.acquired == []
    assert relay.process_catchup_once() == 1
    assert h.rec.acquired == ["PC_OLD"] and h.rec.queued == [("PC_OLD", "PC_OLD-cfg", "c" * 64)]
    assert relay.relay_counters["catchup_queued"] == 1
    # Once per session: a status from it (it cannot send one, but even so) adds nothing.
    relay.process_catchup_once()
    assert len(h.rec.queued) == 1


def test_confirmable_client_is_checked_on_its_first_status_with_running_config_id(h):
    token = _pc(h, "PC_NEW", latest="relay_failed")
    _register(h, "pc-new", "PC_NEW", token, CONFIRMABLE_CAPS)
    assert len(relay._catchup_queue) == 0  # not at register
    assert relay.station_status("pc-new", _status())["status"] == "ok"  # no wall block
    assert relay.station_status("pc-new", _status(running=None))["status"] == "ok"  # wall without the id
    assert len(relay._catchup_queue) == 0
    assert relay.station_status("pc-new", _status(running="something-else"))["status"] == "ok"
    assert relay._catchup_queue["PC_NEW"][1:] == ("something-else", catchup.AT_STATUS)
    relay.process_catchup_once()
    assert h.rec.queued == [("PC_NEW", "PC_NEW-cfg", "c" * 64)]
    assert relay.station_status("pc-new", _status(running="something-else"))["status"] == "ok"
    assert len(relay._catchup_queue) == 0  # the first one only


def test_the_wall_running_the_latest_deploy_is_not_re_pushed(h):
    token = _pc(h, "PC_NEW", latest="unconfirmed", id="cfg-running")
    _register(h, "pc-new", "PC_NEW", token, CONFIRMABLE_CAPS)
    relay.station_status("pc-new", _status(running="cfg-running"))
    relay.process_catchup_once()
    assert h.rec.acquired == [] and h.rec.queued == []
    assert relay.relay_counters["catchup_not_needed"] == 1


@pytest.mark.parametrize("latest,layout,push", [
    ("offline", LAYOUT, True), ("applied", LAYOUT, False), (None, LAYOUT, False),
    ("offline", None, False),  # the PC's layout assignment changed since
])
def test_relay_decision_uses_the_latest_row_and_the_assignment(h, latest, layout, push):
    token = _pc(h, "PC_OLD", layout=layout, latest=latest)
    _register(h, "pc-old", "PC_OLD", token)
    relay.process_catchup_once()
    assert bool(h.rec.queued) is push


def test_rate_limited_pc_is_not_re_pushed(h):
    h.rec.allow = False
    token = _pc(h, "PC_OLD")
    _register(h, "pc-old", "PC_OLD", token)
    relay.process_catchup_once()
    assert h.rec.acquired == ["PC_OLD"] and h.rec.queued == []


def test_failure_pauses_catch_up_then_it_resumes(h, caplog):
    h.rec.fail = ConnectionError("redis down (simulated)")
    tokens = {pc: _pc(h, pc) for pc in ("PC_1", "PC_2", "PC_3")}
    for i, (pc, token) in enumerate(tokens.items()):
        _register(h, f"s-{i}", pc, token)
    assert relay.process_catchup_once() == 3
    assert relay.relay_counters["catchup_errors"] == 1 and relay.relay_counters["catchup_paused"] == 2
    assert relay.catchup_breaker.is_open() and "catch-up paused for 30s" in caplog.text
    h.rec.fail = None
    relay.catchup_breaker.reset()
    relay.schedule_catchup("s-1", "PC_2", catchup.AT_REGISTER)
    relay.process_catchup_once()
    assert h.rec.queued == [("PC_2", "PC_2-cfg", "c" * 64)]


def test_a_replaced_session_is_dropped(h):
    token = _pc(h, "PC_OLD")
    _register(h, "pc-old", "PC_OLD", token)
    relay.disconnect("pc-old")
    relay.process_catchup_once()
    assert h.rec.acquired == []


def test_registered_never_waits_for_redis_or_the_broker(h):
    """A hung Redis/broker is only met in the catch-up loop; register returns at once."""
    h.rec.delay = 3.0
    token = _pc(h, "PC_OLD")
    started = time.monotonic()
    _register(h, "pc-old", "PC_OLD", token)
    assert time.monotonic() - started < 0.5
    assert h.received("pc-old", "registered")


def test_queue_is_bounded_and_deduplicated(h, monkeypatch):
    monkeypatch.setattr(relay, "CATCHUP_QUEUE_MAX", 3)
    for i in range(5):
        relay.schedule_catchup(f"s-{i}", f"PC_{i}", catchup.AT_REGISTER)
    relay.schedule_catchup("s-4b", "PC_4", catchup.AT_REGISTER)  # same PC: one entry, the newest session
    assert list(relay._catchup_queue) == ["PC_2", "PC_3", "PC_4"]
    assert relay._catchup_queue["PC_4"][0] == "s-4b"


# --------------------------------------------------------------------------- #
# The Redis key and the queue call
# --------------------------------------------------------------------------- #


def test_rate_limiter_uses_set_nx_ex_600_on_redis():
    server = FakeRedis()
    try:
        limiter = catchup.RateLimiter(server.url)
        assert limiter.acquire("PC_A") is True
        assert limiter.acquire("PC_A") is False
        assert limiter.acquire("PC_B") is True
        assert 599 <= server.ttl("truelive:catchup:PC_A") <= 600
        assert [c for c in server.commands if c[0] == "SET"] == [("SET", "truelive:catchup:PC_A")] * 2 + [
            ("SET", "truelive:catchup:PC_B")]
    finally:
        server.stop()


def _dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_rate_limiter_and_queue_fail_fast_when_down(monkeypatch):
    limiter = catchup.RateLimiter(f"redis://127.0.0.1:{_dead_port()}/0")
    started = time.monotonic()
    with pytest.raises(Exception):
        limiter.acquire("PC_A")
    assert time.monotonic() - started < 3.0
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app.conf, "broker_url", f"redis://127.0.0.1:{_dead_port()}/0")
    started = time.monotonic()
    with pytest.raises(Exception):
        catchup.enqueue("PC_A", "cfg-1", "h" * 64)
    assert time.monotonic() - started < 6.0, time.monotonic() - started  # was 64 s before ignore_result


def test_fail_fast_options_only_for_a_redis_broker():
    assert catchup._transport_options("redis://:pw@redis:6379/0")["socket_connect_timeout"] == 1.0
    assert catchup._transport_options("rediss://redis:6380/0")["max_retries"] == 0
    assert catchup._transport_options("sqla+postgresql://u@127.0.0.1:1/x") == {}
    assert catchup._transport_options(None) == {}


def test_enqueue_sends_the_task_without_retry_and_with_an_expiry(monkeypatch):
    from app.tasks.celery_app import celery_app

    calls = []

    def fake_send_task(name, args=None, **kwargs):
        calls.append((name, args, kwargs))
        return SimpleNamespace(id="t-1")

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    monkeypatch.setattr(celery_app.conf, "broker_url", "memory://")
    assert catchup.enqueue("PC_A", "cfg-1", "h" * 64) == "t-1"
    name, args, kwargs = calls[0]
    assert name == catchup.TASK_NAME == "app.tasks.pc_maintenance.catch_up_deploy"
    assert args == ["PC_A", "cfg-1", "h" * 64]
    assert kwargs["retry"] is False and kwargs["expires"] == 120 and kwargs["connection"] is not None
    assert kwargs["ignore_result"] is True  # never waits for the result backend
    assert catchup.TASK_NAME in celery_app.tasks


# --------------------------------------------------------------------------- #
# The worker side
# --------------------------------------------------------------------------- #


@pytest.fixture
def worker_db(monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = sqlite_engine(str(tmp_path / "cu.db"))
    create_deploy_tables(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        seed_fleet(db, ["PC_A", "PC_B"])
    audits = AuditRecorder(monkeypatch)
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", True)
    monkeypatch.setattr(relay.settings, "DEPLOY_INJECT_CONFIG_ID", True)
    return SimpleNamespace(Session=Session, audits=audits)


def _missed(Session, pc_id):
    """Deploy ``pc_id`` while it is offline: the latest row ends offline."""
    from app.services import deploy_service as ds

    with Session() as db:
        summary = ds.deploy_config_to_pcs([db.get(PC, pc_id)], db, trigger=ds.TRIGGER_PC_DEPLOY,
                                          sender=FakeSender({pc_id: "offline"}))
    result = summary["results"][0]
    assert result["delivery"] == "offline"
    # SQLite keeps created_at to the second: age every earlier row so that
    # "latest" never depends on a same-second tie (Postgres has microseconds).
    with Session() as db:
        db.query(PcDeployment).filter(PcDeployment.id != result["config_id"]).update(
            {"created_at": func.datetime(PcDeployment.created_at, "-60 seconds")}, synchronize_session=False)
        db.commit()
    return result["config_id"]


def test_worker_re_pushes_the_missed_deploy_with_a_new_config_id(worker_db, monkeypatch):
    patch_generator(monkeypatch)
    missed = _missed(worker_db.Session, "PC_A")
    with worker_db.Session() as db:
        hash_ = db.get(PcDeployment, missed).config_hash
    sender = FakeSender({"PC_A": "delivered"})
    with worker_db.Session() as db:
        result = catchup.run_catch_up(db, "PC_A", missed, hash_, sender=sender)
    assert result["status"] == "deployed" and result["replaces"] == missed and result["config_id"] != missed
    with worker_db.Session() as db:  # the missed row is the older one (see _missed)
        db.query(PcDeployment).filter(PcDeployment.id == missed).update(
            {"created_at": func.datetime(PcDeployment.created_at, "-60 seconds")}, synchronize_session=False)
        db.commit()
    request = sender.requests[0]
    assert request["targetId"] == "PC_A" and request["content"]["config_id"] == result["config_id"]
    with worker_db.Session() as db:
        new = db.get(PcDeployment, result["config_id"])
        assert (new.trigger, new.status, new.config_hash) == ("catch_up", "delivered", hash_)
        assert (new.created_by_type, new.created_by_label) == ("system", "system")
        assert db.get(PcDeployment, missed).status == "offline"  # history kept
    assert [e["changes"]["trigger"] for e in worker_db.audits.entries] == ["pc_deploy", "catch_up"]
    # The next check sees the new row: nothing more to push (no loop).
    with worker_db.Session() as db:
        assert catchup.evaluate_pc(db, "PC_A").push is False
        assert catchup.run_catch_up(db, "PC_A", missed, hash_, sender=sender)["status"] == "skipped"
    assert len(sender.calls) == 1


def test_worker_refuses_a_changed_layout_and_a_newer_deploy(worker_db, monkeypatch):
    layouts = patch_generator(monkeypatch)
    missed = _missed(worker_db.Session, "PC_A")
    with worker_db.Session() as db:
        hash_ = db.get(PcDeployment, missed).config_hash
    layouts["PC_A"] = synthetic_layout("PC_A", marker="edited after the deploy")
    sender = FakeSender()
    with worker_db.Session() as db:
        result = catchup.run_catch_up(db, "PC_A", missed, hash_, sender=sender)
        assert result["status"] == "skipped" and "changed" in result["message"]
        assert db.query(PcDeployment).count() == 1 and sender.calls == []
    # A newer deploy (by an operator) replaced the missed one: the queued task is stale.
    layouts.pop("PC_A")
    newer = _missed(worker_db.Session, "PC_A")
    with worker_db.Session() as db:
        result = catchup.run_catch_up(db, "PC_A", missed, hash_, sender=sender)
    assert result == {"status": "skipped", "reason": "a newer deploy replaced the missed one"}
    assert newer != missed and sender.calls == []


def test_catch_up_task_respects_the_flag_in_the_worker(worker_db, monkeypatch):
    from app.tasks import pc_maintenance

    monkeypatch.setattr("app.database.SessionLocal", worker_db.Session)
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", False)
    assert pc_maintenance.catch_up_deploy.run("PC_A", "cfg-x", None) == {"status": "disabled"}
    monkeypatch.setattr(relay.settings, "DEPLOY_CATCHUP_ENABLED", True)
    assert pc_maintenance.catch_up_deploy.run("PC_A", "cfg-x", None)["status"] == "skipped"  # no such deploy
    assert pc_maintenance.catch_up_deploy.ignore_result is True


def test_settings_default_is_off():
    from app.core.config import Settings

    assert Settings.model_fields["DEPLOY_CATCHUP_ENABLED"].default is False
