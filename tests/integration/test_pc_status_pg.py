"""
Status and history API and retention on Postgres (plan 06 D16; OD-22).

A scratch database built by alembic (template0, so migration 015's tables and
indexes are the real ones), the real pcs router through TestClient, and the
relay's presence endpoint faked.

* test_status_and_history_on_postgres: rows inserted in one transaction share
  ``created_at`` (now() is the transaction time): /pcs/status still returns
  each PC once, the tie broken by the greater id; superseded rows lose to
  older live ones; timestamps come back with a UTC offset (timestamptz);
  health and its staleness; history order and pagination; the deployment
  route is not shadowed by /{pc_id}/... routes; one SELECT for the whole list.
* test_retention_is_exact_on_postgres: 50 per PC and nothing older than 90
  days, exactly (including a same-timestamp tie at rank 50/51 and a row one
  second either side of the cutoff), through the Celery task's code path.

    python -m pytest tests/integration/test_pc_status_pg.py -o addopts='' -p no:cacheprovider
"""

import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.models.screen_layout import ScreenLayout
from app.models.team import Team
from app.services import pc_status
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def scratch_db():
    name = f"d16_status_{os.getpid()}_{secrets.token_hex(3)}"
    admin = _admin_connection()
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}" OWNER truelive TEMPLATE template0 ENCODING \'UTF8\' '
                    "LC_COLLATE 'C' LC_CTYPE 'C'")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    try:
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(BACKEND_ROOT),
                                env=_base_env(DATABASE_URL=url), capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def pg(scratch_db, monkeypatch):
    from app.api import deps
    from app.api.v1 import pcs as pcs_api
    from app.database import get_db

    engine = create_engine(scratch_db, pool_size=5)
    with engine.begin() as conn:
        for table in ("audit_logs", "pc_token_renewals", "pc_health", "pc_deployments", "pcs", "screen_layouts",
                      "teams"):
            conn.exec_driver_sql(f"DELETE FROM {table}")
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        db.add(Team(id="TEAM_d16", name="D16"))
        db.add(ScreenLayout(id="LAYOUT_d16", name="D16 wall", team_id="TEAM_d16"))
        for pc_id in ("PC_A", "PC_B", "PC_C"):
            db.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id="TEAM_d16",
                      auth_token="fixture-token-" + secrets.token_hex(8), last_seen=int(time.time()) - 3))
        db.commit()

    state = {"ws": {"connected_pcs": [], "total_connected": 0}}

    async def fake_ws():
        return state["ws"]

    monkeypatch.setattr(pcs_api, "get_websocket_connected_pcs", fake_ws)
    app = FastAPI()
    app.include_router(pcs_api.router, prefix="/pcs")

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(role="user", is_active=True,
                                                                              username="viewer")
    yield SimpleNamespace(client=TestClient(app), Session=Session, engine=engine, state=state)
    engine.dispose()


def _deploy(config_id, pc_id, status="received", **fields):
    return PcDeployment(id=config_id, pc_id=pc_id, layout_id="LAYOUT_d16", trigger="pc_deploy", status=status,
                        **fields)


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_status_and_history_on_postgres(pg):
    now = int(time.time())
    with pg.Session() as db:  # one transaction: every created_at is the same now()
        db.add_all([
            _deploy("a-1", "PC_A", "offline"), _deploy("a-9", "PC_A", "received", received_at=now),
            _deploy("a-5", "PC_A", "relay_failed"),
            _deploy("b-live", "PC_B", "applied", confirmable=True, applied_at=now),
        ])
        db.commit()
    with pg.Session() as db:  # later transaction: newer, but superseded
        db.add(_deploy("b-newer-superseded", "PC_B", "superseded", superseded_at=now))
        db.add(PcHealth(pc_id="PC_A", health="ok", received_at=now - 2, client_version="1.4.0",
                        core_build_class="reports_v1", core_running_version="1.1.0+d53952a",
                        relay_flags={"token_expiring": True}))
        db.add(PcHealth(pc_id="PC_B", health="degraded", received_at=now - 600))
        db.commit()
        created = {r.id: r.created_at for r in db.query(PcDeployment)}
    assert created["a-1"] == created["a-9"] == created["a-5"]  # a real same-instant tie
    pg.state["ws"] = {"connected_pcs": [{"pc_id": "PC_A", "connected_at": "2026-11-17T01:02:03.000001+00:00"}],
                      "total_connected": 1}

    selects = []

    @event.listens_for(pg.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    body = pg.client.get("/pcs/status").json()
    event.remove(pg.engine, "before_cursor_execute", _count)
    assert len(selects) == 1
    assert [p["pc_id"] for p in body["pcs"]] == ["PC_A", "PC_B", "PC_C"]
    a, b, c = body["pcs"]
    assert a["last_deploy"]["config_id"] == "a-9"  # tie: the greatest id
    assert b["last_deploy"]["config_id"] == "b-live"  # superseded loses to an older live row
    assert c["last_deploy"] is None and c["health"] is None
    assert a["health"]["health_stale"] is False and b["health"]["health_stale"] is True
    assert (a["health"]["client_version"], a["health"]["core_build_class"], a["health"]["core_running_version"]) == (
        "1.4.0", "reports_v1", "1.1.0+d53952a")
    assert a["health"]["relay_flags"] == {"token_expiring": True}
    created_a = _parse(a["last_deploy"]["created_at"])
    assert created_a.utcoffset() == timedelta(0) and created_a == created["a-9"]
    assert _parse(a["last_seen"]).utcoffset() == timedelta(0)
    assert a["websocket_connected_at"] == "2026-11-17T01:02:03.000001+00:00"
    assert pg.client.get("/pcs/PC_A/status").json()["last_deploy"]["config_id"] == "a-9"

    # History: newest first, ties by id desc, paginated; the deployment route.
    page = pg.client.get("/pcs/PC_A/deployments", params={"limit": 2}).json()
    assert page["total"] == 3 and [d["config_id"] for d in page["items"]] == ["a-9", "a-5"]
    assert [d["config_id"] for d in pg.client.get("/pcs/PC_A/deployments",
                                                  params={"limit": 2, "offset": 2}).json()["items"]] == ["a-1"]
    assert pg.client.get("/pcs/deployments/b-live").json()["status"] == "applied"
    with pg.Session() as db:
        db.add(_deploy("health", "PC_C", "pending"))
        db.commit()
    assert pg.client.get("/pcs/deployments/health").json()["pc_id"] == "PC_C"  # not /{pc_id}/health
    detail = pg.client.get("/pcs/PC_A/health").json()
    assert detail["pc_id"] == "PC_A" and detail["health"] == "ok"
    assert pg.client.get("/pcs/PC_C/health").json() is None


def test_retention_is_exact_on_postgres(pg, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    day = timedelta(days=1)
    rows = []
    for i in range(120):  # PC_A: the newest 50 of 120 stay
        rows.append(("a-%03d" % i, "PC_A", now - 30 * day + i * timedelta(minutes=5)))
    for i in range(10):  # PC_B: 4 older than 90 days
        rows.append(("b-%02d" % i, "PC_B", now - (200 - i) * day if i < 4 else now - i * day))
    rows.append(("c-inside", "PC_C", now - 90 * day + timedelta(seconds=1)))
    rows.append(("c-outside", "PC_C", now - 90 * day - timedelta(seconds=1)))
    with pg.Session() as db:
        for config_id, pc_id, created in rows:
            row = _deploy(config_id, pc_id)
            row.created_at = created
            db.add(row)
        db.commit()
    # A tie at ranks 50/51 for PC_A: a-069 (51st newest) gets a-070's (50th newest) timestamp; the
    # greater id stays.
    with pg.Session() as db:
        tie = db.get(PcDeployment, "a-070").created_at
        db.query(PcDeployment).filter(PcDeployment.id == "a-069").update({"created_at": tie})
        db.commit()
    expected_a = {"a-%03d" % i for i in range(70, 120)}
    with pg.Session() as db:  # the same rank computed by Postgres independently
        ranked = db.execute(text(
            "SELECT id FROM (SELECT id, row_number() OVER (PARTITION BY pc_id ORDER BY created_at DESC, id DESC) rn "
            "FROM pc_deployments WHERE pc_id = 'PC_A') t WHERE rn <= 50")).scalars().all()
    assert set(ranked) == expected_a

    from app.tasks import pc_maintenance

    monkeypatch.setattr("app.database.SessionLocal", pg.Session)
    monkeypatch.setattr(pc_status, "datetime", _FrozenDatetime(now))
    result = pc_maintenance.prune_deploy_history.run()
    assert result == {"deleted": 70 + 4 + 1, "by_age": 4 + 1, "by_count": 70}
    with pg.Session() as db:
        left = {pc: {r for (r,) in db.query(PcDeployment.id).filter(PcDeployment.pc_id == pc)}
                for pc in ("PC_A", "PC_B", "PC_C")}
    assert left["PC_A"] == expected_a
    assert left["PC_B"] == {"b-%02d" % i for i in range(4, 10)}
    assert left["PC_C"] == {"c-inside"}
    assert pc_maintenance.prune_deploy_history.run() == {"deleted": 0, "by_age": 0, "by_count": 0}


class _FrozenDatetime:
    """Stands in for ``datetime`` in pc_status so "now" is fixed."""

    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now if tz is None else self._now.astimezone(tz)

    def __getattr__(self, name):
        return getattr(datetime, name)
