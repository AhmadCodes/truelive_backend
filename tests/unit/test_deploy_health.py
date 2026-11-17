"""
The API stays responsive during a deploy (plan 06 D14):
test_health_responsive_during_deploy[pcs|screen_layouts].

The real application (app.main.app) is served by uvicorn on 127.0.0.1 with
an ephemeral port (lifespan off: no startup DB work). The relay is
tests/fake_relay.py, which answers each ``deploy_config`` only after
``ack_delay_s``. While a deploy to 3 PCs waits on it (one layout deploy, or
three concurrent PC deploys), ``GET /health`` must answer within 200 ms.
Both deploy routes block on the relay call, so they must be plain ``def``
(threadpool); an ``async def`` would hold the event loop for the whole call.

Only teams, screen_layouts, pcs and pc_deployments exist (file SQLite);
auth and the audit writer are replaced, /health's DB probe answers True.

    python -m pytest tests/unit/test_deploy_health.py -o addopts='' -p no:cacheprovider
"""

import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from sqlalchemy.orm import sessionmaker

import app.main as main
from app.api import deps
from app.core.config import settings
from app.database import get_db
from app.models.pc_tracking import PcDeployment
from tests.deploy_testkit import (
    LAYOUT_ID,
    AuditRecorder,
    create_deploy_tables,
    patch_generator,
    seed_fleet,
    sqlite_engine,
)
from tests.fake_relay import FakeRelay

ACK_DELAY_S = 1.5
HEALTH_BUDGET_S = 0.2
PCS = ("PC_A", "PC_B", "PC_C")


@pytest.fixture
def served(tmp_path, monkeypatch):
    relay = FakeRelay(ack_delay_s=ACK_DELAY_S).start()
    monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
    monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", "fixture-sender-" + uuid.uuid4().hex)
    monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)
    monkeypatch.setattr(main, "check_db_health", lambda: True)

    engine = sqlite_engine(str(tmp_path / "health.sqlite"))
    create_deploy_tables(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as seed:
        seed_fleet(seed, PCS)
    patch_generator(monkeypatch)
    AuditRecorder(monkeypatch)

    admin = SimpleNamespace(role="admin", username="operator", user_id=uuid.uuid4())

    def _db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[deps.get_admin_user] = lambda: admin
    main.app.dependency_overrides[deps.get_current_user] = lambda: admin

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=0, lifespan="off",
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, name="api-under-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{port}", relay=relay, Session=Session)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        relay.stop()
        for dep in (get_db, deps.get_admin_user, deps.get_current_user):
            main.app.dependency_overrides.pop(dep, None)
        engine.dispose()
    assert not thread.is_alive()


def _post(base, path, json=None, out=None):
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as client:
        response = client.post(path, json=json)
    if out is not None:
        out.append(response)
    return response


@pytest.mark.parametrize("route", ["pcs", "screen_layouts"])
def test_health_responsive_during_deploy(served, route):
    responses = []
    if route == "pcs":
        workers = [threading.Thread(target=_post, args=(served.base, f"/api/v1/pcs/{pc}/deploy", None, responses))
                   for pc in PCS]
    else:
        workers = [threading.Thread(target=_post, args=(served.base, f"/api/v1/screen-layouts/{LAYOUT_ID}/deploy",
                                                        {}, responses))]
    started = time.monotonic()
    for worker in workers:
        worker.start()
    assert served.relay.first_request.wait(10), "the deploy never reached the relay"

    timings = []
    with httpx.Client(base_url=served.base, trust_env=False, timeout=5) as client:
        for _ in range(5):
            t0 = time.monotonic()
            health = client.get("/health")
            timings.append(time.monotonic() - t0)
            assert health.status_code == 200 and health.json()["status"] == "healthy"
            time.sleep(0.05)
    in_flight = any(worker.is_alive() for worker in workers)
    for worker in workers:
        worker.join(timeout=30)
    elapsed = time.monotonic() - started

    assert in_flight, "the deploy finished before /health was measured; the test proves nothing"
    assert max(timings) < HEALTH_BUDGET_S, timings
    assert elapsed >= ACK_DELAY_S  # the deploy really waited on the relay
    assert len(served.relay.requests) == 3
    assert {r["targetId"] for r in served.relay.requests} == set(PCS)
    assert all(r["content"]["config_id"] == r["config_id"] for r in served.relay.requests)
    assert served.relay.connects and all(a == {"role": "sender", "secret": settings.RELAY_SENDER_SECRET}
                                         for a in served.relay.connects)

    assert [r.status_code for r in responses] == [200] * len(workers)
    results = [r for resp in responses for r in (resp.json().get("results") or [resp.json()])]
    assert sorted(r["pc_id"] for r in results) == sorted(PCS)
    assert {r["delivery"] for r in results} == {"delivered"}
    with served.Session() as db:
        assert sorted(row.status for row in db.query(PcDeployment).all()) == ["delivered"] * 3
