"""
PC status and deploy-history API, retention (plan 06 D16; OD-22, NEW-51, E20).

Card tests: "status and history API", "retention", test_pcs_status_versions.
The routes run through TestClient on SQLite holding only teams, screen_layouts,
pcs, pc_deployments and pc_health (plan 6.1); the relay's presence endpoint
is faked. The Postgres version (timestamptz, same-transaction ties, exact
pruning) is tests/integration/test_pc_status_pg.py.

    python -m pytest tests/unit/test_pc_status_api.py -o addopts='' -p no:cacheprovider
"""

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.services import pc_status
from tests.deploy_testkit import camera_url, sqlite_engine

NOW = int(time.time())  # refreshed by the api fixture: staleness is relative to the test's own clock
TEAM = "TEAM_d16"
LAYOUT = "LAYOUT_d16"


def _naive(ts):
    """SQLite keeps naive UTC datetimes."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


@pytest.fixture
def api(monkeypatch):
    global NOW
    NOW = int(time.time())
    from app.api import deps
    from app.api.v1 import pcs as pcs_api
    from app.database import get_db

    engine = sqlite_engine()
    meta = MetaData()
    for model in (Team, ScreenLayout, PC, PcDeployment, PcHealth):
        model.__table__.to_metadata(meta)
    meta.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        db.add(Team(id=TEAM, name="D16"))
        db.add(ScreenLayout(id=LAYOUT, name="D16 wall", team_id=TEAM))
        db.commit()

    state = {"ws": {"connected_pcs": [], "total_connected": 0}}

    async def fake_ws():
        return state["ws"]

    monkeypatch.setattr(pcs_api, "get_websocket_connected_pcs", fake_ws)
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

    def add_pc(pc_id, **fields):
        with Session() as db:
            db.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id=TEAM,
                      auth_token=fields.pop("auth_token", "fixture-token-" + secrets.token_hex(8)), **fields))
            db.commit()

    def add_deploy(config_id, pc_id, status="received", created=NOW - 60, **fields):
        with Session() as db:
            row = PcDeployment(id=config_id, pc_id=pc_id, layout_id=LAYOUT, layout_name="D16 wall", trigger="pc_deploy",
                               status=status, confirmable=fields.pop("confirmable", False), **fields)
            row.created_at = _naive(created)
            row.created_by_type, row.created_by_id, row.created_by_label = "user", "u-1", "alice"
            db.add(row)
            db.commit()

    def add_health(pc_id, **fields):
        with Session() as db:
            db.add(PcHealth(pc_id=pc_id, **fields))
            db.commit()

    return SimpleNamespace(client=TestClient(app), Session=Session, state=state, engine=engine, user=user,
                           add_pc=add_pc, add_deploy=add_deploy, add_health=add_health, app=app)


def _entry(body, pc_id):
    found = [p for p in body["pcs"] if p["pc_id"] == pc_id]
    assert len(found) == 1, (pc_id, [p["pc_id"] for p in body["pcs"]])
    return found[0]


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# /pcs/status and /pcs/{id}/status
# --------------------------------------------------------------------------- #


def test_status_joins_health_and_the_latest_deploy(api):
    api.add_pc("PC_A", last_seen=NOW - 5, last_connected=NOW - 50, last_applied=NOW - 90)
    api.add_pc("PC_B")
    api.add_pc("PC_C")
    api.add_health("PC_A", health="degraded", reasons=["no_video"], received_at=NOW - 10, reported_at=NOW - 11,
                   streams_total=8, streams_playing=6, streams_failed_count=2, running_config_id="d-applied",
                   telemetry_mode="stdout_only", relay_flags={"token_outdated": True})
    api.add_deploy("d-applied", "PC_A", "applied", created=NOW - 300, confirmable=True, applied_at=NOW - 290)
    api.add_deploy("d-superseded", "PC_A", "superseded", created=NOW - 100, superseded_at=NOW - 50)
    api.add_deploy("d-received", "PC_A", "received", created=NOW - 200, received_at=NOW - 199)
    api.add_deploy("b-old", "PC_B", "superseded", created=NOW - 500)
    api.add_deploy("b-new", "PC_B", "superseded", created=NOW - 400)
    api.state["ws"] = {"connected_pcs": [{"pc_id": "PC_A", "connected_at": "2026-11-17T01:02:03.456789"}],
                       "total_connected": 1}

    response = api.client.get("/pcs/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["total_pcs"], body["connected_count"], body["disconnected_count"]) == (3, 1, 2)
    a = _entry(body, "PC_A")
    assert a["is_connected"] is True
    # The latest non-superseded row wins over a newer superseded one.
    assert a["last_deploy"]["config_id"] == "d-received" and a["last_deploy"]["status"] == "received"
    assert a["last_deploy"]["deployed_by"] == {"type": "user", "id": "u-1", "label": "alice"}
    health = a["health"]
    assert (health["health"], health["reasons"], health["streams_playing"], health["streams_total"],
            health["streams_failed_count"], health["telemetry_mode"], health["health_stale"]) == (
        "degraded", ["no_video"], 6, 8, 2, "stdout_only", False)
    assert health["relay_flags"] == {"token_outdated": True}
    # Every PC's latest is all-superseded: the newest superseded one.
    assert _entry(body, "PC_B")["last_deploy"]["config_id"] == "b-new"
    # No health, no deploy: null, not missing.
    c = _entry(body, "PC_C")
    assert c["health"] is None and c["last_deploy"] is None and c["is_connected"] is False

    one = api.client.get("/pcs/PC_A/status").json()
    assert one["last_deploy"] == a["last_deploy"] and one["health"] == a["health"]
    assert api.client.get("/pcs/PC_C/status").json()["health"] is None
    assert api.client.get("/pcs/PC_NONE/status").status_code == 404


def test_status_is_one_query_for_any_number_of_pcs(api):
    for i in range(12):
        api.add_pc(f"PC_{i:02d}")
        api.add_health(f"PC_{i:02d}", health="ok", received_at=NOW)
        api.add_deploy(f"d-{i:02d}-1", f"PC_{i:02d}", created=NOW - 100)
        api.add_deploy(f"d-{i:02d}-2", f"PC_{i:02d}", created=NOW - 50)
    selects = []

    @event.listens_for(api.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    body = api.client.get("/pcs/status").json()
    event.remove(api.engine, "before_cursor_execute", _count)
    assert len(body["pcs"]) == 12
    assert all(p["last_deploy"]["config_id"].endswith("-2") for p in body["pcs"])
    assert len(selects) == 1, selects
    assert "row_number() OVER" in selects[0].replace("ROW_NUMBER", "row_number")


def test_same_second_tie_returns_the_pc_once(api):
    api.add_pc("PC_A")
    api.add_deploy("aaa", "PC_A", "offline", created=NOW - 30)
    api.add_deploy("bbb", "PC_A", "received", created=NOW - 30)
    body = api.client.get("/pcs/status").json()
    assert [p["pc_id"] for p in body["pcs"]] == ["PC_A"]
    assert body["pcs"][0]["last_deploy"]["config_id"] == "bbb"  # ties: the greater id
    assert api.client.get("/pcs/PC_A/status").json()["last_deploy"]["config_id"] == "bbb"


@pytest.mark.parametrize("age,stale", [(0, False), (170, False), (190, True), (3600, True)])
def test_health_staleness_flips_after_180_s(api, age, stale):
    api.add_pc("PC_A")
    api.add_health("PC_A", health="ok", received_at=int(time.time()) - age)
    assert api.client.get("/pcs/PC_A/status").json()["health"]["health_stale"] is stale
    assert api.client.get("/pcs/PC_A/health").json()["health_stale"] is stale


@pytest.mark.parametrize("elapsed,stale", [(0, False), (179.9, False), (180, False), (180.01, True), (181, True)])
def test_staleness_boundary(elapsed, stale):
    row = PcHealth(pc_id="PC_A", received_at=1_800_000_000)
    assert pc_status.is_stale(row, now=1_800_000_000 + elapsed) is stale
    assert pc_status.is_stale(PcHealth(pc_id="PC_A"), now=0) is True and pc_status.is_stale(None) is True


def test_register_only_health_row_is_stale_with_no_state(api):
    """A 1.1.x Client: register versions only, no station_status ever."""
    api.add_pc("PC_A")
    api.add_health("PC_A", registered_at=NOW - 5, relay_flags={})
    health = api.client.get("/pcs/PC_A/status").json()["health"]
    assert health["health"] is None and health["health_stale"] is True and health["received_at"] is None
    assert _parse(health["registered_at"]).timestamp() == NOW - 5


def test_pcs_status_versions(api):
    """E20: versions and build class are exposed in /pcs/status."""
    api.add_pc("PC_A")
    api.add_pc("PC_OLD")
    api.add_health("PC_A", health="ok", received_at=NOW, client_version="1.4.0", client_build="1.4.0+abc1234",
                   core_version="1.1.0+d53952a", core_installed_version="1.2.0", core_running_version="1.1.0+d53952a",
                   core_version_source="hello", core_build_class="legacy", delivery_policy="live_with_fallback",
                   core_state="RUNNING", core_last_exit="0x544C0002")
    entry = _entry(api.client.get("/pcs/status").json(), "PC_A")["health"]
    assert {k: entry[k] for k in ("client_version", "client_build", "core_version", "core_installed_version",
                                  "core_running_version", "core_version_source", "core_build_class",
                                  "delivery_policy", "core_state", "core_last_exit")} == {
        "client_version": "1.4.0", "client_build": "1.4.0+abc1234", "core_version": "1.1.0+d53952a",
        "core_installed_version": "1.2.0", "core_running_version": "1.1.0+d53952a", "core_version_source": "hello",
        "core_build_class": "legacy", "delivery_policy": "live_with_fallback", "core_state": "RUNNING",
        "core_last_exit": "0x544C0002"}
    assert _entry(api.client.get("/pcs/status").json(), "PC_OLD")["health"] is None  # "-" for old Clients
    assert api.client.get("/pcs/PC_A/status").json()["health"]["core_build_class"] == "legacy"


def test_timestamps_are_utc_aware(api):
    """NEW-51: every timestamp carries its UTC offset, whatever the server's zone."""
    api.add_pc("PC_A", last_seen=NOW - 5, last_connected=NOW - 50, last_applied=NOW - 90)
    api.add_health("PC_A", health="ok", received_at=NOW - 7, reported_at=NOW - 8)
    api.add_deploy("d-1", "PC_A", "applied", created=NOW - 100, delivered_at=NOW - 99, applied_at=NOW - 98)
    api.state["ws"] = {"connected_pcs": [{"pc_id": "PC_A", "connected_at": "2026-11-17T01:02:03.456789"}],
                       "total_connected": 1}
    for path in ("/pcs/status", "/pcs/PC_A/status"):
        body = api.client.get(path).json()
        entry = body["pcs"][0] if "pcs" in body else body
        for name, epoch in (("last_seen", NOW - 5), ("last_connected", NOW - 50), ("last_applied", NOW - 90)):
            parsed = _parse(entry[name])
            assert parsed.tzinfo is not None and parsed.timestamp() == epoch, (path, name)
        assert entry["websocket_connected_at"] == "2026-11-17T01:02:03.456789+00:00"
        assert _parse(entry["health"]["received_at"]).timestamp() == NOW - 7
        deploy = entry["last_deploy"]
        assert _parse(deploy["created_at"]).timestamp() == NOW - 100
        assert _parse(deploy["applied_at"]).timestamp() == NOW - 98
        for value in (deploy["created_at"], deploy["delivered_at"], entry["last_seen"]):
            assert value.endswith("Z") or value.endswith("+00:00"), value


@pytest.mark.parametrize("raw,expected", [
    ("2026-11-17T01:02:03", "2026-11-17T01:02:03+00:00"),
    ("2026-11-17T01:02:03Z", "2026-11-17T01:02:03+00:00"),
    ("2026-11-17T06:02:03+05:00", "2026-11-17T01:02:03+00:00"),
    ("garbage", None), (None, None), (12, None),
])
def test_normalize_iso(raw, expected):
    assert pc_status.normalize_iso(raw) == expected


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_history_is_ordered_and_paginated(api):
    api.add_pc("PC_A")
    api.add_pc("PC_B")
    for i in range(25):
        api.add_deploy(f"a-{i:02d}", "PC_A", "applied" if i % 3 else "superseded", created=NOW - 1000 + i * 10)
    api.add_deploy("a-tie-x", "PC_A", created=NOW - 1000)  # same second as a-00: id desc
    api.add_deploy("b-1", "PC_B", created=NOW)
    page = api.client.get("/pcs/PC_A/deployments", params={"limit": 10}).json()
    assert (page["pc_id"], page["total"], page["limit"], page["offset"]) == ("PC_A", 26, 10, 0)
    assert [d["config_id"] for d in page["items"]] == [f"a-{i:02d}" for i in range(24, 14, -1)]
    last = api.client.get("/pcs/PC_A/deployments", params={"limit": 10, "offset": 20}).json()
    assert [d["config_id"] for d in last["items"]] == ["a-04", "a-03", "a-02", "a-01", "a-tie-x", "a-00"]
    everything = []
    for offset in range(0, 26, 7):
        everything += api.client.get("/pcs/PC_A/deployments",
                                     params={"limit": 7, "offset": offset}).json()["items"]
    assert len(everything) == 26 and len({d["config_id"] for d in everything}) == 26
    assert "b-1" not in {d["config_id"] for d in everything}
    assert api.client.get("/pcs/PC_A/deployments").json()["limit"] == 20  # default
    for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}):
        assert api.client.get("/pcs/PC_A/deployments", params=bad).status_code == 422, bad
    assert api.client.get("/pcs/PC_NONE/deployments").status_code == 404
    empty = api.client.get("/pcs/PC_B/deployments", params={"offset": 5}).json()
    assert empty["total"] == 1 and empty["items"] == []


@pytest.mark.parametrize("config_id", ["9b2c2d0e-0000-4000-8000-000000000001", "status", "health", "deployments",
                                       "screens", "controlled", "with-manager", "count"])
def test_deployment_by_config_id_is_not_shadowed(api, config_id):
    api.add_pc("PC_A")
    api.add_deploy(config_id, "PC_A", "applied", created=NOW - 10)
    response = api.client.get(f"/pcs/deployments/{config_id}")
    assert response.status_code == 200, response.text
    assert response.json()["config_id"] == config_id and response.json()["pc_id"] == "PC_A"
    assert api.client.get("/pcs/deployments/no-such-deploy").status_code == 404


def test_pc_routes_still_resolve(api):
    api.add_pc("PC_A")
    assert api.client.get("/pcs/PC_A").status_code == 200
    assert api.client.get("/pcs/PC_A/status").status_code == 200
    assert api.client.get("/pcs/status").status_code == 200


# --------------------------------------------------------------------------- #
# /pcs/{id}/health
# --------------------------------------------------------------------------- #


def test_health_route(api):
    api.add_pc("PC_A")
    api.add_pc("PC_B")
    url = camera_url()  # credentialed, built at run time
    api.add_health("PC_A", health="unhealthy", received_at=NOW - 3, last_message_id="m-9",
                   capabilities=["deploy_result.v1", "station_status.v1"], streams_failed=["src-1", "src-2"],
                   telemetry_channels={"heartbeat": {"target": "listener", "connected": True,
                                                     "last_message_age_sec": 2}},
                   flags={"lossy": True}, readiness=[{"code": "AUTOLOGON_OFF", "detail": f"see {url}"}],
                   core_caps=["ack_reason"], drift_key_names=["host_server_port"], migration_status="done",
                   last_config_error=f"could not open {url}", core_exe_sha256="a" * 64, core_exe_size=1234,
                   core_build_class="reports_v1")
    body = api.client.get("/pcs/PC_A/health").json()
    assert body["pc_id"] == "PC_A" and body["health"] == "unhealthy" and body["health_stale"] is False
    assert body["capabilities"] == ["deploy_result.v1", "station_status.v1"]
    assert body["streams_failed"] == ["src-1", "src-2"] and body["flags"] == {"lossy": True}
    assert body["telemetry_channels"]["heartbeat"]["target"] == "listener"
    assert body["readiness"] == [{"code": "AUTOLOGON_OFF", "detail": "see <url>"}]
    assert body["last_config_error"] == "could not open <url>"
    assert (body["core_caps"], body["drift_key_names"], body["migration_status"]) == (
        ["ack_reason"], ["host_server_port"], "done")
    assert "rtsp://" not in json.dumps(body) and "fixture-pass" not in json.dumps(body)
    assert api.client.get("/pcs/PC_B/health").json() is None  # a PC that never reported
    assert api.client.get("/pcs/PC_NONE/health").status_code == 404


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #


def test_no_tokens_bodies_or_urls_in_any_response(api):
    token = "eyJ" + secrets.token_urlsafe(40)
    api.add_pc("PC_A", auth_token=token)
    url = camera_url()
    api.add_health("PC_A", health="ok", received_at=NOW, last_config_error=f"bad {url}")
    api.add_deploy("d-1", "PC_A", "failed", created=NOW - 5, status_detail=f"Core said: cannot open {url} (x)")
    api.state["ws"] = {"connected_pcs": [{"pc_id": "PC_A", "connected_at": "2026-11-17T01:02:03"}],
                       "total_connected": 1}
    for role in ("user", "admin", "super_admin"):
        api.user.role = role
        for path in ("/pcs/status", "/pcs/PC_A/status", "/pcs/PC_A/deployments", "/pcs/deployments/d-1",
                     "/pcs/PC_A/health"):
            response = api.client.get(path)
            assert response.status_code == 200, (role, path, response.text)
            text = response.text
            assert token not in text and "auth_token" not in text, (role, path)
            assert "rtsp://" not in text and "fixture-pass" not in text and "fixture-user" not in text, (role, path)
    detail = api.client.get("/pcs/deployments/d-1").json()["status_detail"]
    assert detail == "Core said: cannot open <url> (x)"


def test_routes_need_a_logged_in_user(api):
    """OD-22 (a): every logged-in user, like /pcs; nobody without a login."""
    from app.api import deps

    api.add_pc("PC_A")
    api.add_deploy("d-1", "PC_A")
    del api.app.dependency_overrides[deps.get_current_user]
    for path in ("/pcs/status", "/pcs/PC_A/status", "/pcs/PC_A/deployments", "/pcs/deployments/d-1",
                 "/pcs/PC_A/health"):
        assert api.client.get(path).status_code in (401, 403), path


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def _ids(Session, pc_id=None):
    with Session() as db:
        query = db.query(PcDeployment.id)
        if pc_id:
            query = query.filter(PcDeployment.pc_id == pc_id)
        return {r for (r,) in query}


def test_retention_keeps_50_per_pc_and_nothing_older_than_90_days(api):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    day = 86400
    base = int(now.timestamp())
    for pc_id in ("PC_A", "PC_B", "PC_C", "PC_D"):
        api.add_pc(pc_id)
    # PC_A: 60 recent rows -> the newest 50 stay.
    for i in range(60):
        api.add_deploy(f"a-{i:02d}", "PC_A", created=base - 10 * day + i * 60)
    # PC_B: 10 rows, 3 older than 90 days.
    for i in range(10):
        age = (100 + i) * day if i < 3 else (i + 1) * day
        api.add_deploy(f"b-{i:02d}", "PC_B", created=base - age)
    # PC_C: the 90-day boundary, one second either side.
    api.add_deploy("c-inside", "PC_C", created=base - 90 * day + 1)
    api.add_deploy("c-outside", "PC_C", created=base - 90 * day - 1)
    # PC_D: a tie at ranks 50/51 (same second): the greater id stays.
    for i in range(49):
        api.add_deploy(f"d-{i:02d}", "PC_D", created=base - day + i * 10)
    api.add_deploy("d-tie-a", "PC_D", created=base - 2 * day)
    api.add_deploy("d-tie-b", "PC_D", created=base - 2 * day)

    with api.Session() as db:
        result = pc_status.prune_deploy_history(db, now=now)
        db.commit()
    assert result == {"deleted": 10 + 3 + 1 + 1, "by_age": 3 + 1, "by_count": 10 + 1}
    assert _ids(api.Session, "PC_A") == {f"a-{i:02d}" for i in range(10, 60)}
    assert _ids(api.Session, "PC_B") == {f"b-{i:02d}" for i in range(3, 10)}
    assert _ids(api.Session, "PC_C") == {"c-inside"}
    assert _ids(api.Session, "PC_D") == {f"d-{i:02d}" for i in range(49)} | {"d-tie-b"}
    with api.Session() as db:  # idempotent
        assert pc_status.prune_deploy_history(db, now=now) == {"deleted": 0, "by_age": 0, "by_count": 0}


def test_prune_task_and_its_daily_schedule(api, monkeypatch):
    from app.tasks import celery_app as celery_module
    from app.tasks import pc_maintenance

    api.add_pc("PC_A")
    base = int(time.time())
    for i in range(53):
        api.add_deploy(f"t-{i:02d}", "PC_A", created=base - 3600 + i)
    monkeypatch.setattr("app.database.SessionLocal", api.Session)
    assert pc_maintenance.prune_deploy_history.run() == {"deleted": 3, "by_age": 0, "by_count": 3}
    assert _ids(api.Session, "PC_A") == {f"t-{i:02d}" for i in range(3, 53)}
    entry = celery_module.celery_app.conf.beat_schedule["pc-deploy-history-prune"]
    assert entry["task"] == "app.tasks.pc_maintenance.prune_deploy_history"
    schedule = entry["schedule"]
    assert schedule.hour == {3} and schedule.minute == {30} and len(schedule.day_of_week) == 7
    assert "app.tasks.pc_maintenance.prune_deploy_history" in celery_module.celery_app.tasks
