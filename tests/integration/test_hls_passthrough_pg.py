"""
HLS camera URL from the API to the layout Core receives (HLS-SRV, 07_work/hls_card.md).

One scratch Postgres built by alembic. Cameras are created and updated through
the real camera routes, the wall is deployed with the real config_loader,
config_generator and deploy_service (only the relay sender is a fake), and the
payload handed to the relay is inspected:

* the HLS URL is in the layout byte for byte (source "url" and LocationUris),
  query string included, for a token with "@", ":" and percent escapes;
* the RTSP camera beside it is encoded exactly as before;
* the API returns the URL as stored; the audit log and the process log hold no token.

Prerequisite (failing, never skipping, without it): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL, default postgresql://truelive@127.0.0.1:5433/postgres).

    python -m pytest tests/integration/test_hls_passthrough_pg.py -o addopts='' -p no:cacheprovider
"""

import json
import logging
import os
import secrets
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.v1 import cameras as cameras_api
from app.database import get_db
from app.models.camera import Camera
from app.models.device import Device
from app.models.pc import PC
from app.models.screen import Screen
from app.models.screen_layout import ScreenLayout
from app.models.screen_mapping import ScreenMapping
from app.models.site import Site
from app.models.site_camera_layout import SiteCamerasLayout, SiteCamerasLayoutConfig
from app.models.team import Team
from app.models.view import View
from app.services import deploy_service as ds
from app.services.stream_config_generator import create_camera_object
from tests.deploy_testkit import FakeSender
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env

pytestmark = [pytest.mark.integration]

ACTOR = ("service_account", "sa_hls_fixture", "hls fixture")
TOKEN_1 = "eyJhbGciOiJIUzI1NiJ9." + secrets.token_urlsafe(40) + ".sig_-1"
TOKEN_2 = "eyJhbGciOiJIUzI1NiJ9." + secrets.token_urlsafe(40) + ".sig_-2"
# "@", ":" (what the RTSP password encoder keys on), escapes and parameter order.
HLS_FIRST = f"https://hls.example.com:8443/v2/live/cam-a/manifest.m3u8?z=1&token={TOKEN_1}&u=op@example.com:x&p=a%2Fb+c"
HLS_URL = f"https://hls.example.com:8443/v2/live/cam-a/manifest.m3u8?z=1&token={TOKEN_2}&u=op@example.com:x&p=a%2Fb+c"
RTSP_PASSWORD = "fix@ture:" + secrets.token_hex(4)
RTSP_URL = "rtsp://" + "fixture-user:" + RTSP_PASSWORD + "@192.0.2.20:554/Streaming/channels/101"
RTSP_ENCODED = ("rtsp://" + "fixture-user:" + RTSP_PASSWORD.replace("@", "%40").replace(":", "%3A")
                + "@192.0.2.20:554/Streaming/channels/101")


@pytest.fixture(scope="module")
def scratch_db():
    name = f"hls_srv_{os.getpid()}_{secrets.token_hex(3)}"
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


@pytest.fixture(scope="module")
def wall(scratch_db):
    """A 1x2 wall whose two cells will hold the HLS and the RTSP camera."""
    engine = create_engine(scratch_db)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        db.add_all([Team(id="TEAM_hls", name="HLS"), Site(id="SITE_hls", name="HLS site")])
        db.flush()
        db.add_all([
            ScreenLayout(id="LAYOUT_hls", name="HLS wall", team_id="TEAM_hls"),
            Device(id="DEV_hls", name="HLS device", site_id="SITE_hls", nvr_username="fixture", nvr_password="fixture"),
        ])
        db.flush()
        db.add(PC(id="PC_hls", name="HLS wall PC", role="controller", team_id="TEAM_hls",
                  screen_layout_id="LAYOUT_hls"))
        db.add(Screen(id="SCREEN_hls", screen_layout_id="LAYOUT_hls", name="Monitor 1", rows=1, columns=2,
                      switching_interval=10))
        db.flush()
        db.add(View(id="VIEW_hls", screen_id="SCREEN_hls", name="View 1", layout_rows=1, layout_columns=2,
                    view_number=1))
        db.commit()
    yield SimpleNamespace(engine=engine, Session=Session)
    engine.dispose()


@pytest.fixture(scope="module")
def client(wall):
    app = FastAPI()
    app.include_router(cameras_api.router, prefix="/api/v1/cameras")
    principal = SimpleNamespace(id=uuid.uuid4(), name="hls fixture")   # duck-typed service account

    def _db():
        session = wall.Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    # The routes' auth deps are closures made by admin_or_scope()/user_or_scope().
    for route in app.routes:
        for dep in getattr(getattr(route, "dependant", None), "dependencies", []):
            if getattr(dep.call, "__qualname__", "").split(".")[0] in ("admin_or_scope", "user_or_scope"):
                app.dependency_overrides[dep.call] = lambda: principal
    with TestClient(app) as test_client:
        yield test_client


def _strings(obj, key):
    """Every value stored under ``key`` at any depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _strings(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _strings(item, key)


def test_hls_url_reaches_the_deployed_layout_unchanged(wall, client, caplog):
    caplog.set_level(logging.DEBUG)

    # Entered through the API: create both cameras, then replace the HLS token.
    for cam_id, name, url in (("CAM_hls", "HLS cam", HLS_FIRST), ("CAM_rtsp", "RTSP cam", RTSP_URL)):
        response = client.post("/api/v1/cameras", json={"id": cam_id, "device_id": "DEV_hls", "name": name,
                                                        "rtsp_url": url})
        assert response.status_code == 201, response.text
        assert response.json()["rtsp_url"] == url
    response = client.put("/api/v1/cameras/CAM_hls", json={"rtsp_url": HLS_URL})
    assert response.status_code == 200, response.text
    assert response.json()["rtsp_url"] == HLS_URL
    assert client.get("/api/v1/cameras/CAM_hls").json()["rtsp_url"] == HLS_URL

    # Stored as typed.
    with wall.Session() as db:
        assert db.query(Camera).filter(Camera.id == "CAM_hls").one().rtsp_url == HLS_URL
        db.add(SiteCamerasLayoutConfig(site_id="SITE_hls", site_name="HLS site", n_rows=1, n_cols=2))
        db.flush()
        for col, cam_id in ((1, "CAM_hls"), (2, "CAM_rtsp")):
            db.add(ScreenMapping(screen_id="SCREEN_hls", view_id="VIEW_hls", slot_row=1, slot_col=col,
                                 device_id="DEV_hls", camera_id=cam_id))
            db.add(SiteCamerasLayout(site_id="SITE_hls", site_name="HLS site", slot_row=1, slot_col=col,
                                     camera_id=cam_id))
        db.commit()

    # Deployed with the real loader and generator; only the relay is a fake.
    sender = FakeSender()
    with wall.Session() as db:
        pcs = db.query(PC).filter(PC.id == "PC_hls").all()
        results = ds.deploy_config_to_pcs(pcs, db, trigger=ds.TRIGGER_PC_DEPLOY, actor=ACTOR, sender=sender)
        standalone = create_camera_object(db.query(Camera).filter(Camera.id == "CAM_hls").one(), db)
    assert len(sender.requests) == 1, results
    layout = sender.requests[0]["content"]
    if isinstance(layout, str):
        layout = json.loads(layout)

    sources = [s for screen in layout["screens"] for group in screen["source_groups"]
               for s in _strings(group, "url") if s]
    by_camera = {}
    for screen in layout["screens"]:
        for group in screen["source_groups"]:
            for entry in _entries(group):
                if entry.get("camera_id"):
                    by_camera[entry["camera_id"]] = entry
    assert set(by_camera) == {"CAM_hls", "CAM_rtsp"}, layout
    assert by_camera["CAM_hls"]["url"] == HLS_URL
    assert by_camera["CAM_rtsp"]["url"] == RTSP_ENCODED
    for entry in by_camera.values():
        assert [u["url"] for u in entry["LocationUris"]] == [HLS_URL, RTSP_ENCODED]
    # No other spelling of the HLS URL anywhere in the layout, and the old token is gone.
    assert {s for s in sources if s.lower().startswith("http")} == {HLS_URL}
    wire = json.dumps(layout)
    assert TOKEN_1 not in wire and wire.count(json.dumps(HLS_URL)) >= 3

    # The stream-config generator (configs API) passes it through as well.
    assert standalone["url"] == HLS_URL
    assert {u["url"] for u in standalone["LocationUris"]} == {HLS_URL, RTSP_ENCODED}

    # Secrets: nothing in the audit log, the deploy rows or the process log.
    with wall.engine.connect() as conn:
        stored = " ".join(str(row) for table in ("audit_logs", "pc_deployments")
                          for row in conn.execute(text(f"SELECT * FROM {table}")))
    assert conn is not None and stored
    for secret in (TOKEN_1, TOKEN_2, RTSP_PASSWORD, "op@example.com"):
        assert secret not in stored
        assert secret not in caplog.text


def _entries(obj):
    """Every dict at any depth (source entries live inside the groups)."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _entries(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _entries(item)
