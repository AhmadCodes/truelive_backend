"""
Deploy path (plan 06 D14; NEW-45, NEW-46, NEW-54): "deploy path".

deploy_service.deploy_config_to_pcs on SQLite (only teams, screen_layouts,
pcs and pc_deployments exist), with the relay replaced by FakeSender and the
audit writer recorded (audit_logs uses PG UUIDs; the real audit rows are
checked on Postgres in tests/integration/test_deploy_path_pg.py):

* every PC gets a unique uuid4 config_id at the payload root, and its row is
  committed (visible to another connection) before the send;
* delivered / offline / relay error / no answer / relay unreachable map to
  their rows and responses; an offline PC is failed and not stamped;
* a relay that runs ahead (received or accepted first) is never moved back;
  a 1.1.x receipt stamps last_applied, the API never does;
* a newer deploy supersedes in-flight rows, a row superseded before its send
  is not sent;
* DEPLOY_INJECT_CONFIG_ID=false sends the layout unchanged and nothing is
  confirmable;
* the routes are plain ``def`` and their responses carry neither the layout
  (no ``config`` key) nor any ``rtsp://`` URL; one audit entry per PC;
* socketio_sender: the secret goes in the CONNECT auth payload only, every
  call is in flight at once, and a silent relay costs one timeout per batch.

    python -m pytest tests/unit/test_deploy_path.py -o addopts='' -p no:cacheprovider
"""

import inspect
import json
import secrets
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.v1 import pcs as pcs_api
from app.api.v1 import screen_layouts as layouts_api
from app.core.config import settings
from app.database import get_db
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.services import deploy_service as ds
from app.services import deploy_tracking as dt
from tests.deploy_testkit import (
    LAYOUT_ID,
    LAYOUT_NAME,
    AuditRecorder,
    FakeSender,
    camera_url,
    create_deploy_tables,
    find_key,
    patch_generator,
    seed_fleet,
    sqlite_engine,
)

ACTOR = ("user", "7b3f0a52-0000-4000-8000-0000000000d1", "operator")
RESULT_KEYS = {"pc_id", "status", "delivery", "config_id", "confirmable", "message", "screens"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A file SQLite DB (separate connections), three PCs on one layout."""
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    engine = sqlite_engine(str(tmp_path / "d14.sqlite"))
    create_deploy_tables(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    seed = Session()
    seed_fleet(seed, ["PC_A", "PC_B", "PC_C"])
    seed.close()
    patch_generator(monkeypatch)
    audit = AuditRecorder(monkeypatch)
    db = Session()
    yield SimpleNamespace(engine=engine, Session=Session, db=db, audit=audit)
    db.close()
    engine.dispose()


def _pcs(env, *ids):
    rows = env.db.query(PC).filter(PC.id.in_(ids)).all()
    by_id = {pc.id: pc for pc in rows}
    return [by_id[i] for i in ids]


def _rows(env):
    with env.Session() as s:
        return {r.id: r for r in s.query(PcDeployment).all()}


def _last_applied(env, pc_id):
    with env.Session() as s:
        return s.get(PC, pc_id).last_applied


def _deploy(env, *ids, sender=None, trigger=ds.TRIGGER_LAYOUT_DEPLOY):
    return ds.deploy_config_to_pcs(_pcs(env, *ids), env.db, trigger=trigger, actor=ACTOR,
                                   sender=sender or FakeSender())


# --------------------------------------------------------------------------- #
# config_id, commit before send
# --------------------------------------------------------------------------- #


def test_unique_root_config_ids_each_committed_before_its_send(env):
    seen_at_send = {}

    def on_send(requests):
        # Another connection must already see every row, pending.
        with env.Session() as other:
            for request in requests:
                row = other.get(PcDeployment, request["config_id"])
                seen_at_send[request["config_id"]] = (row.status, row.pc_id) if row else None

    sender = FakeSender(on_send=on_send)
    summary = _deploy(env, "PC_A", "PC_B", "PC_C", sender=sender)

    requests = sender.requests
    assert [r["targetId"] for r in requests] == ["PC_A", "PC_B", "PC_C"]
    ids = [r["config_id"] for r in requests]
    assert len(set(ids)) == 3
    for request in requests:
        assert uuid.UUID(request["config_id"]).version == 4
        assert request["content"]["config_id"] == request["config_id"]  # at the payload root
        assert seen_at_send[request["config_id"]] == ("pending", request["targetId"])
    rows = _rows(env)
    assert set(rows) == set(ids)
    for request in requests:
        row = rows[request["config_id"]]
        assert row.pc_id == request["targetId"]
        assert (row.trigger, row.layout_id, row.layout_name, row.screens) == ("layout_deploy", LAYOUT_ID,
                                                                            LAYOUT_NAME, 2)
        assert row.config_hash == dt.config_hash(request["content"])
        assert (row.created_by_type, row.created_by_id, row.created_by_label) == ACTOR
    assert [r["config_id"] for r in summary["results"]] == ids


def test_delivered_rows_and_truthful_response(env):
    summary = _deploy(env, "PC_A")
    assert (summary["total"], summary["deployed"], summary["failed"], summary["skipped"]) == (1, 1, 0, 0)
    result = summary["results"][0]
    assert set(result) == RESULT_KEYS
    assert (result["status"], result["delivery"], result["confirmable"], result["screens"]) == (
        "deployed", "delivered", False, 2)
    assert "cannot confirm" in result["message"]
    row = _rows(env)[result["config_id"]]
    assert row.status == "delivered" and row.delivered_at and row.confirmable is False
    assert _last_applied(env, "PC_A") is None  # stamped only on receipt (1.1.x) or applied (D15)


def test_confirmable_answer_is_recorded(env):
    summary = _deploy(env, "PC_A", sender=FakeSender({"PC_A": "delivered_confirmable"}))
    result = summary["results"][0]
    assert result["confirmable"] is True and "waiting for the station" in result["message"]
    assert _rows(env)[result["config_id"]].confirmable is True


def test_offline_pc_is_failed_and_not_stamped(env):
    summary = _deploy(env, "PC_A", "PC_B", sender=FakeSender({"PC_B": "offline"}))
    a, b = summary["results"]
    assert (a["status"], b["status"], b["delivery"]) == ("deployed", "failed", "offline")
    assert (summary["deployed"], summary["failed"]) == (1, 1)
    row = _rows(env)[b["config_id"]]
    assert (row.status, row.status_detail, row.delivered_at) == ("offline", "PC not connected to the relay", None)
    assert _last_applied(env, "PC_B") is None


@pytest.mark.parametrize("case", ["unreachable", "timeout", "error", "garbage"])
def test_relay_failures_are_relay_failed(env, case):
    if case == "unreachable":
        sender = FakeSender(unreachable=True)
    elif case == "garbage":
        def sender(requests):
            return {r["config_id"]: {"status": "deployed!"} for r in requests}
    else:
        sender = FakeSender({"PC_A": case})
    summary = _deploy(env, "PC_A", sender=sender)
    result = summary["results"][0]
    assert (result["status"], result["delivery"], result["confirmable"]) == ("failed", "error", False)
    row = _rows(env)[result["config_id"]]
    assert row.status == "relay_failed"
    expected = {"unreachable": "relay unreachable", "timeout": "relay did not answer",
                "garbage": "relay did not answer", "error": "relay error: invalid content"}[case]
    assert row.status_detail == expected
    assert _last_applied(env, "PC_A") is None


def test_an_answer_for_another_config_id_is_not_trusted(env):
    def sender(requests):
        return {r["config_id"]: {"status": "delivered", "config_id": "someone-else"} for r in requests}

    result = _deploy(env, "PC_A", sender=sender)["results"][0]
    assert result["status"] == "failed" and _rows(env)[result["config_id"]].status == "relay_failed"


# --------------------------------------------------------------------------- #
# Races with the relay
# --------------------------------------------------------------------------- #


def test_relay_that_runs_ahead_ends_in_accepted(env):
    def relay_first(requests):
        with env.Session() as relay_db:
            for request in requests:
                dt.transition(relay_db, request["config_id"], dt.ACCEPTED, pc_id=request["targetId"])
            relay_db.commit()

    result = _deploy(env, "PC_A", sender=FakeSender(on_send=relay_first))["results"][0]
    row = _rows(env)[result["config_id"]]
    assert row.status == "accepted" and row.accepted_at and row.delivered_at  # delivered kept as evidence
    assert result["status"] == "deployed"


def test_a_1_1_x_receipt_before_the_api_answer_stays_received_and_is_stamped(env):
    def receipt_first(requests):
        with env.Session() as relay_db:
            for request in requests:
                dt.record_receipt(relay_db, request["config_id"], request["targetId"], confirmable=False)
            relay_db.commit()

    result = _deploy(env, "PC_A", sender=FakeSender(on_send=receipt_first))["results"][0]
    row = _rows(env)[result["config_id"]]
    assert row.status == "received" and row.received_at and row.delivered_at
    assert _last_applied(env, "PC_A") == row.received_at


def test_a_receipt_after_relay_failed_still_counts(env):
    result = _deploy(env, "PC_A", sender=FakeSender({"PC_A": "timeout"}))["results"][0]
    with env.Session() as relay_db:
        assert dt.record_receipt(relay_db, result["config_id"], "PC_A", confirmable=False).changed
        relay_db.commit()
    assert _rows(env)[result["config_id"]].status == "received"
    assert _last_applied(env, "PC_A") is not None


# --------------------------------------------------------------------------- #
# Supersede
# --------------------------------------------------------------------------- #


def test_a_newer_deploy_supersedes_in_flight_rows(env):
    first = _deploy(env, "PC_A", "PC_B", "PC_C", sender=FakeSender({"PC_C": "delivered_confirmable"}))
    first_ids = {r["pc_id"]: r["config_id"] for r in first["results"]}
    with env.Session() as relay_db:  # PC_B is a 1.1.x station that ACKed
        dt.record_receipt(relay_db, first_ids["PC_B"], "PC_B", confirmable=False)
        relay_db.commit()
    second = _deploy(env, "PC_A", "PC_B", "PC_C")
    rows = _rows(env)
    assert rows[first_ids["PC_A"]].status == "superseded"  # delivered, in flight
    assert rows[first_ids["PC_B"]].status == "received"  # 1.1.x end state is history
    assert rows[first_ids["PC_C"]].status == "superseded"
    assert rows[first_ids["PC_A"]].status_detail == f"superseded by {second['results'][0]['config_id']}"
    for result in second["results"]:
        assert rows[result["config_id"]].status == "delivered"


def test_a_row_superseded_before_its_send_is_not_sent(env, monkeypatch):
    real_statuses = dt.statuses

    def concurrent_deploy_then_read(db, ids):
        # Another admin's deploy to PC_B commits between our commit and our send.
        newer = "newer-" + secrets.token_hex(4)
        with env.Session() as other:
            other.add(PcDeployment(id=newer, pc_id="PC_B", trigger="pc_deploy", status="pending"))
            other.flush()
            assert dt.supersede_older(other, "PC_B", newer) == 1
            other.commit()
        return real_statuses(db, ids)

    monkeypatch.setattr(dt, "statuses", concurrent_deploy_then_read)
    sender = FakeSender()
    summary = _deploy(env, "PC_A", "PC_B", sender=sender)
    assert [r["targetId"] for r in sender.requests] == ["PC_A"]
    b = summary["results"][1]
    assert (b["status"], b["delivery"], b["message"]) == ("skipped", None, ds.MSG_SUPERSEDED)
    assert _rows(env)[b["config_id"]].status == "superseded"


# --------------------------------------------------------------------------- #
# Kill switch, skipped and failed generation, duplicates
# --------------------------------------------------------------------------- #


def test_inject_kill_switch_sends_the_layout_unchanged(env, monkeypatch):
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", False)
    sender = FakeSender({"PC_A": "delivered_confirmable"})
    result = _deploy(env, "PC_A", sender=sender)["results"][0]
    content = sender.requests[0]["content"]
    assert "config_id" not in content
    assert result["config_id"] and result["confirmable"] is False  # the station cannot correlate it
    row = _rows(env)[result["config_id"]]
    assert row.status == "delivered" and row.confirmable is False
    assert env.audit.entries[0]["changes"]["config_id_injected"] is False


def test_no_layout_and_generation_failure_get_no_row(env, monkeypatch):
    patch_generator(monkeypatch, fail_for={"PC_B"})
    pc_c = _pcs(env, "PC_C")[0]
    pc_c.screen_layout_id = None
    env.db.commit()
    sender = FakeSender()
    summary = _deploy(env, "PC_A", "PC_B", "PC_C", sender=sender)
    a, b, c = summary["results"]
    assert (a["status"], b["status"], c["status"]) == ("deployed", "failed", "skipped")
    assert b["message"] == ds.MSG_GENERATION_FAILED and c["message"] == ds.MSG_NO_LAYOUT
    assert b["config_id"] is None and c["config_id"] is None and b["delivery"] is None
    assert set(_rows(env)) == {a["config_id"]}
    assert [r["targetId"] for r in sender.requests] == ["PC_A"]
    assert (summary["total"], summary["deployed"], summary["failed"], summary["skipped"]) == (3, 1, 1, 1)


def test_nothing_to_send_never_opens_the_relay(env, monkeypatch):
    patch_generator(monkeypatch, fail_for={"PC_A"})
    sender = FakeSender()
    summary = _deploy(env, "PC_A", sender=sender)
    assert sender.calls == [] and summary["failed"] == 1


def test_duplicate_targets_are_deployed_once(env):
    pcs = _pcs(env, "PC_A") * 3
    sender = FakeSender()
    summary = ds.deploy_config_to_pcs(pcs, env.db, trigger="pc_deploy", actor=ACTOR, sender=sender)
    assert summary["total"] == 1 and len(sender.requests) == 1 and len(_rows(env)) == 1


# --------------------------------------------------------------------------- #
# Audit (NEW-54)
# --------------------------------------------------------------------------- #


def test_one_config_deployed_audit_entry_per_pc(env, monkeypatch):
    patch_generator(monkeypatch, fail_for={"PC_C"})
    summary = _deploy(env, "PC_A", "PC_B", "PC_C", sender=FakeSender({"PC_B": "offline"}))
    entries = env.audit.entries
    assert [e["resource_id"] for e in entries] == ["PC_A", "PC_B", "PC_C"]
    for entry, result in zip(entries, summary["results"]):
        assert entry["action"] == "config.deployed" and entry["resource_type"] == "pc"
        assert entry["actor"] == ACTOR
        assert entry["changes"]["config_id"] == result["config_id"]
        assert entry["changes"]["trigger"] == "layout_deploy"
        text = json.dumps(entry["changes"])
        assert "rtsp://" not in text and "source_groups" not in text
    assert [e["changes"]["outcome"] for e in entries] == ["pending", "pending", "generation_failed"]


def test_the_audit_commits_with_the_rows_before_the_send(env, monkeypatch):
    counts_at_send = []
    sender = FakeSender(on_send=lambda requests: counts_at_send.append(len(env.audit.entries)))
    _deploy(env, "PC_A", "PC_B", sender=sender)
    assert counts_at_send == [2]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def test_deploy_routes_are_plain_def():
    """Both handlers block on the relay call, so they must run in the
    threadpool (an ``async def`` would stall the event loop)."""
    assert not inspect.iscoroutinefunction(pcs_api.deploy_config)
    assert not inspect.iscoroutinefunction(layouts_api.deploy_screen_layout)


@pytest.fixture
def api(env, monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(ds, "socketio_sender", sender)
    app = FastAPI()
    app.include_router(pcs_api.router, prefix="/api/v1/pcs")
    app.include_router(layouts_api.router, prefix="/api/v1/screen-layouts")
    admin = SimpleNamespace(role="admin", username="operator", user_id=uuid.UUID(ACTOR[1]))

    def _db():
        session = env.Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_admin_user] = lambda: admin
    app.dependency_overrides[deps.get_current_user] = lambda: admin
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, sender=sender)


def _assert_no_layout_in(response):
    text = response.text
    assert "rtsp://" not in text and camera_url() not in text and "source_groups" not in text
    assert not find_key(response.json(), "config")


def test_pc_deploy_route_response(api, env):
    response = api.client.post("/api/v1/pcs/PC_A/deploy")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == RESULT_KEYS
    assert (body["pc_id"], body["status"], body["delivery"]) == ("PC_A", "deployed", "delivered")
    _assert_no_layout_in(response)
    assert _rows(env)[body["config_id"]].trigger == "pc_deploy"
    # The layout did go to the relay, with its URL and the root config_id.
    content = api.sender.requests[0]["content"]
    assert content["config_id"] == body["config_id"]
    assert content["screens"][0]["source_groups"][0][0]["url"] == camera_url()


def test_pc_deploy_route_offline_is_502_and_recorded(api, env):
    api.sender.answers["PC_A"] = "offline"
    response = api.client.post("/api/v1/pcs/PC_A/deploy")
    assert response.status_code == 502
    body = response.json()
    # A string detail (what older portals show) plus the per-PC result (D18).
    assert set(body) == {"detail", "result"}
    assert isinstance(body["detail"], str) and "not connected" in body["detail"]
    assert set(body["result"]) == RESULT_KEYS
    assert (body["result"]["pc_id"], body["result"]["status"], body["result"]["delivery"]) == (
        "PC_A", "failed", "offline")
    assert body["result"]["message"] == body["detail"]
    assert body["result"]["confirmable"] is False
    _assert_no_layout_in(response)
    rows = _rows(env)
    assert [r.status for r in rows.values()] == ["offline"]
    assert body["result"]["config_id"] in rows


@pytest.mark.parametrize("answer", ["timeout", "error", "unreachable"])
def test_pc_deploy_route_relay_failure_is_502_with_result(api, env, answer):
    if answer == "unreachable":
        api.sender.unreachable = True
    else:
        api.sender.answers["PC_A"] = answer
    response = api.client.post("/api/v1/pcs/PC_A/deploy")
    assert response.status_code == 502
    body = response.json()
    assert set(body) == {"detail", "result"} and isinstance(body["detail"], str)
    assert set(body["result"]) == RESULT_KEYS
    assert (body["result"]["status"], body["result"]["delivery"]) == ("failed", "error")
    assert _rows(env)[body["result"]["config_id"]].status == "relay_failed"
    _assert_no_layout_in(response)


def test_layout_deploy_route_response(api, env):
    api.sender.answers["PC_B"] = "offline"
    response = api.client.post(f"/api/v1/screen-layouts/{LAYOUT_ID}/deploy", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"total", "deployed", "skipped", "failed", "results", "screen_layout_id", "message"}
    assert (body["total"], body["deployed"], body["failed"]) == (3, 2, 1)
    for result in body["results"]:
        assert set(result) == RESULT_KEYS
    assert {r["pc_id"]: r["status"] for r in body["results"]} == {"PC_A": "deployed", "PC_B": "failed",
                                                                  "PC_C": "deployed"}
    _assert_no_layout_in(response)
    assert {r.trigger for r in _rows(env).values()} == {"layout_deploy"}
    # subset deploy
    response = api.client.post(f"/api/v1/screen-layouts/{LAYOUT_ID}/deploy", json={"pc_ids": ["PC_C"]})
    assert [r["pc_id"] for r in response.json()["results"]] == ["PC_C"]
    _assert_no_layout_in(response)


# --------------------------------------------------------------------------- #
# socketio_sender
# --------------------------------------------------------------------------- #


class _FakeClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.connects = []
        self.emits = []
        self.disconnected = False
        self.answer = True
        _FakeClient.instances.append(self)

    def connect(self, url, **kwargs):
        self.connects.append((url, kwargs))

    def emit(self, event, data, callback=None):
        self.emits.append((event, data, time.monotonic()))
        if self.answer and callback is not None:
            callback({"status": "delivered", "config_id": data["config_id"], "confirmable": False})

    def disconnect(self):
        self.disconnected = True


def test_api_sender_connects_with_the_secret(monkeypatch):
    """socketio_sender passes the sender auth on connect (and only there:
    never in the URL). Moved here from pcs._deploy_config_to_pcs (D12)."""
    import socketio

    secret = "fixture-sender-" + secrets.token_hex(16)
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", secret)
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "WEBSOCKET_PORT", 9)
    _FakeClient.instances = []
    monkeypatch.setattr(socketio, "Client", _FakeClient)
    answers = ds.socketio_sender([{"config_id": "c1", "targetId": "PC_A", "content": {}}])
    client = _FakeClient.instances[0]
    assert answers == {"c1": {"status": "delivered", "config_id": "c1", "confirmable": False}}
    url, kwargs = client.connects[0]
    assert url == "http://127.0.0.1:9"
    assert kwargs["auth"] == {"role": "sender", "secret": secret}
    assert secret not in url
    assert client.kwargs.get("reconnection") is False
    assert [e[0] for e in client.emits] == ["deploy_config"] and client.disconnected


def test_compose_relay_host(monkeypatch):
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "0.0.0.0")
    monkeypatch.setattr(settings, "WEBSOCKET_PORT", 8080)
    assert ds.relay_url() == "http://websocket:8080"


def test_a_silent_relay_costs_one_timeout_per_batch(monkeypatch):
    import socketio

    _FakeClient.instances = []
    monkeypatch.setattr(socketio, "Client", _FakeClient)
    original_init = _FakeClient.__init__

    def silent_init(self, *a, **k):
        original_init(self, *a, **k)
        self.answer = False

    monkeypatch.setattr(_FakeClient, "__init__", silent_init)
    requests = [{"config_id": f"c{i}", "targetId": f"PC_{i}", "content": {}} for i in range(5)]
    started = time.monotonic()
    answers = ds.socketio_sender(requests, url="http://127.0.0.1:9", timeout=0.4)
    elapsed = time.monotonic() - started
    assert answers == {f"c{i}": None for i in range(5)}
    assert 0.35 <= elapsed < 1.2, elapsed  # not 5 x 0.4 s
    emits = _FakeClient.instances[0].emits
    assert len(emits) == 5 and emits[-1][2] - emits[0][2] < 0.2  # all in flight before any wait


def test_sender_keeps_each_polling_post_under_the_engineio_packet_limit(monkeypatch):
    """python-engineio refuses a polling POST with more than 16 packets; the
    sender polls. 40 deploys through a real engine.io server all get their
    answer (before the window: every one timed out, relay_failed)."""
    from tests.fake_relay import FakeRelay

    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", "")
    relay = FakeRelay().start()
    try:
        requests = [{"config_id": f"c{i:02d}", "targetId": f"PC_{i:02d}", "content": {"n": i}} for i in range(40)]
        started = time.monotonic()
        answers = ds.socketio_sender(requests, url=relay.url, timeout=3.0)
        elapsed = time.monotonic() - started
    finally:
        relay.stop()
    assert {k: (v or {}).get("status") for k, v in answers.items()} == {f"c{i:02d}": "delivered" for i in range(40)}
    assert [r["config_id"] for r in relay.requests] == [f"c{i:02d}" for i in range(40)]
    assert elapsed < 3.0, elapsed


def test_a_silent_relay_still_costs_one_timeout_with_many_requests(monkeypatch):
    import socketio

    _FakeClient.instances = []
    monkeypatch.setattr(socketio, "Client", _FakeClient)
    original_init = _FakeClient.__init__

    def silent_init(self, *a, **k):
        original_init(self, *a, **k)
        self.answer = False

    monkeypatch.setattr(_FakeClient, "__init__", silent_init)
    requests = [{"config_id": f"c{i}", "targetId": f"PC_{i}", "content": {}} for i in range(30)]
    started = time.monotonic()
    answers = ds.socketio_sender(requests, url="http://127.0.0.1:9", timeout=0.4)
    elapsed = time.monotonic() - started
    assert answers == {f"c{i}": None for i in range(30)}
    assert 0.35 <= elapsed < 1.2, elapsed  # one round's timeout, not 4
    assert len(_FakeClient.instances[0].emits) == ds.SENDER_WINDOW  # the rest were never sent


def test_unreachable_relay_raises(monkeypatch):
    import socketio

    class Refusing(_FakeClient):
        def connect(self, url, **kwargs):
            raise socketio.exceptions.ConnectionError("refused")

    monkeypatch.setattr(socketio, "Client", Refusing)
    with pytest.raises(ds.RelayUnreachable):
        ds.socketio_sender([{"config_id": "c1", "targetId": "PC_A", "content": {}}], url="http://127.0.0.1:9")
