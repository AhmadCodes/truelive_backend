"""
Stream providers through the real processes (LUM-SRV): scratch Postgres built
by alembic, the real eventlet relay, a real Celery worker, a python-socketio
station that registers like Client 1.2.0, a fake Lumana on 127.0.0.1 and
FakeRedis. The layout comes from the real config_loader and config_generator.

* a deploy sends the station minted addresses (cell and LocationUris), never
  the reference; the station's ACK reaches Redis through the relay;
* the worker's tick renews before the expiry and the relay re-delivers the
  same layout (same config_id, same address path, a new token) without a
  pc_deployments row or an audit entry;
* a reconnect whose acknowledgement is missing gets one delivery;
* a failure report makes the worker reopen the closed session, once.

Prerequisites (failing, never skipping, without them): the local Postgres
(TRUELIVE_TEST_PG_ADMIN_URL) and an interpreter that can run the eventlet
relay (TRUELIVE_RELAY_PYTHON, default this one; Python 3.11).

    python -m pytest tests/integration/test_stream_providers_pg.py -o addopts='' -p no:cacheprovider
"""

import json
import os
import secrets
import subprocess
import sys
import time

import pytest
import redis as redis_py
from celery import Celery
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.camera import Camera
from app.models.device import Device
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.models.screen import Screen
from app.models.screen_layout import ScreenLayout
from app.models.screen_mapping import ScreenMapping
from app.models.site import Site
from app.models.site_camera_layout import SiteCamerasLayout, SiteCamerasLayoutConfig
from app.models.stream_provider import StreamProvider
from app.models.system_setting import SystemSetting
from app.models.team import Team
from app.models.view import View
from app.schemas import station_protocol as sp
from app.services import deploy_service as ds
from app.services.stream_providers import engine as sp_engine
from app.services.system_settings_service import system_settings_service
from app.utils.url_processor import make_provider_ref
from tests.fake_lumana import FakeLumana
from tests.fake_redis import FakeRedis
from tests.integration.test_catchup_pg import Worker, _mint, _wait
from tests.integration.test_env_wiring import BACKEND_ROOT, PG_ADMIN_URL, _admin_connection, _base_env
from tests.integration.test_relay_reports_pg import Pc, Relay

pytestmark = [pytest.mark.integration, pytest.mark.websocket]

RTSP_URL = "rtsp://" + "fixture-user:" + "fixture-" + "pass" + "@192.0.2.20:554/Streaming/channels/101"
LIMIT_S = 30


@pytest.fixture(scope="module")
def scratch_db():
    name = f"lum_srv_{os.getpid()}_{secrets.token_hex(3)}"
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


def _seed(Session, token, lumana, key):
    """A 1x2 wall: a Lumana camera and an RTSP camera of one site."""
    with Session() as db:
        db.add_all([Team(id="TEAM_sp", name="SP"), Site(id="SITE_sp", name="SP site")])
        db.flush()
        db.add_all([
            ScreenLayout(id="LAYOUT_sp", name="SP wall", team_id="TEAM_sp"),
            Device(id="DEV_sp", name="SP device", site_id="SITE_sp", nvr_username="fixture", nvr_password="fixture"),
        ])
        db.flush()
        provider = StreamProvider(name="Lumana main", type="lumana", settings={"base_url": lumana.url},
                                  secrets=system_settings_service._encrypt_value(json.dumps({"api_key": key})))
        db.add(provider)
        db.add(PC(id="PC_sp", name="SP wall PC", role="controller", team_id="TEAM_sp", screen_layout_id="LAYOUT_sp",
                  auth_token=token, hls_capable=True))
        db.add(Screen(id="SCREEN_sp", screen_layout_id="LAYOUT_sp", name="Monitor 1", rows=1, columns=2,
                      switching_interval=10))
        db.flush()
        db.add(View(id="VIEW_sp", screen_id="SCREEN_sp", name="View 1", layout_rows=1, layout_columns=2,
                    view_number=1))
        reference = make_provider_ref(provider.id, "cam1")
        db.add_all([Camera(id="CAM_lumana", device_id="DEV_sp", name="Lumana cam", rtsp_url=reference),
                    Camera(id="CAM_rtsp", device_id="DEV_sp", name="RTSP cam", rtsp_url=RTSP_URL)])
        db.add(SiteCamerasLayoutConfig(site_id="SITE_sp", site_name="SP site", n_rows=1, n_cols=2))
        db.flush()
        for col, cam_id in ((1, "CAM_lumana"), (2, "CAM_rtsp")):
            db.add(ScreenMapping(screen_id="SCREEN_sp", view_id="VIEW_sp", slot_row=1, slot_col=col,
                                 device_id="DEV_sp", camera_id=cam_id))
            db.add(SiteCamerasLayout(site_id="SITE_sp", site_name="SP site", slot_row=1, slot_col=col,
                                     camera_id=cam_id))
        # Renew 6 s after a mint (tokens of the fake live 120 s); one automatic delivery per second.
        for name, value in (("renew_fraction", "0.05"), ("pc_refresh_min_interval_sec", "1")):
            db.add(SystemSetting(key=f"stream_providers.{name}", value=value, category="stream_providers",
                                 description="fixture", is_encrypted=False, data_type="string"))
        db.commit()
        return reference


def _cells(content):
    return {source["camera_id"]: source for source in sp_engine._sources(content) if source.get("camera_id")}


def test_deploy_renewal_reconnect_and_failure_report(scratch_db, monkeypatch):
    db_engine = create_engine(scratch_db)
    Session = sessionmaker(bind=db_engine, autoflush=False)
    jwt_secret, sender_secret = secrets.token_hex(24), secrets.token_urlsafe(32)
    fernet_key = Fernet.generate_key().decode()
    key = "fixture-key-" + secrets.token_hex(8)
    token = _mint("PC_sp", jwt_secret)
    lumana = FakeLumana({key}, {"cam1"}, lifetime=120)
    redis_server = FakeRedis()
    client = redis_py.Redis.from_url(redis_server.url)
    broker = "sqla+" + scratch_db
    shared = dict(REDIS_URL=redis_server.url, CELERY_BROKER_URL=broker, CELERY_RESULT_BACKEND="db+" + scratch_db,
                  SETTINGS_ENCRYPTION_KEY=fernet_key, RELAY_CATCHUP_LOOP_SECONDS="0.2",
                  STREAM_PROVIDERS_ENABLED="true")
    # This process plays the API: the same Fernet key and Redis as the relay and the worker.
    monkeypatch.setattr(system_settings_service, "fernet", Fernet(fernet_key.encode()))
    monkeypatch.setattr(system_settings_service, "key_status", "ok")
    monkeypatch.setattr(sp_engine, "_redis", client)
    reference = _seed(Session, token, lumana, key)
    relay = worker = station = None
    logs = []
    tasks = Celery("lum-srv-test", broker=broker)
    try:
        relay = Relay(scratch_db, jwt_secret, sender_secret, **shared)
        worker_env = _base_env(DATABASE_URL=scratch_db, JWT_SECRET=jwt_secret, RELAY_SENDER_SECRET=sender_secret,
                               WEBSOCKET_HOST="127.0.0.1", WEBSOCKET_PORT=str(relay.port), **shared)
        worker_env.pop("TRUELIVE_RELAY_ASYNC_MODE", None)
        worker = Worker(worker_env)
        monkeypatch.setattr(settings, "WEBSOCKET_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "WEBSOCKET_PORT", relay.port)
        monkeypatch.setattr(settings, "RELAY_SENDER_SECRET", sender_secret)
        monkeypatch.setattr(settings, "DEPLOY_INJECT_CONFIG_ID", True)

        # A station that registers like Client 1.2.0: pc_id and token only.
        station = Pc(relay.url, "PC_sp", token)

        # 1. Deploy: the real generator, the real sender, the real relay.
        with Session() as db:
            summary = ds.deploy_config_to_pcs(db.query(PC).all(), db, trigger=ds.TRIGGER_PC_DEPLOY)
        result = summary["results"][0]
        assert result["delivery"] == "delivered" and result["provider_streams"] == {"total": 1, "unresolved": []}
        config_id = result["config_id"]
        _wait(lambda: station.configs, LIMIT_S, "the station never got the deploy")
        first = station.configs[0]["content"]
        cells = _cells(first)
        minted = cells["CAM_lumana"]["url"]
        assert minted.startswith(lumana.url + "/v2/file/cam1/") and "?token=" in minted
        assert cells["CAM_rtsp"]["url"] == RTSP_URL  # nothing to encode in this password: unchanged
        for cell in cells.values():
            assert [u["url"] for u in cell["LocationUris"]] == [minted, RTSP_URL]
        assert "provider://" not in json.dumps(first) and first["config_id"] == config_id
        # The station's ACK reaches Redis through the relay (and the deploy row is received as always).
        _wait(lambda: client.get("truelive:sp:ack:PC_sp") == config_id.encode(), LIMIT_S, "no ACK key")
        with Session() as db:
            _wait(lambda: db.get(PcDeployment, config_id, populate_existing=True).status == "received", LIMIT_S,
                  "the deploy row never became received")
            assert db.get(PcDeployment, config_id).config_hash  # of the unresolved layout
        assert lumana.count("start") == 1

        # 2. Renewal by the worker's tick, delivered by the relay; not a deploy.
        time.sleep(7)  # past renew_at (6 s after the mint)
        tasks.send_task("app.tasks.stream_provider_tasks.tick")
        _wait(lambda: len(station.configs) >= 2, LIMIT_S, "the renewal was never delivered")
        renewed = station.configs[1]["content"]
        new_url = _cells(renewed)["CAM_lumana"]["url"]
        assert renewed["config_id"] == config_id  # the same config_id
        assert new_url.split("?")[0] == minted.split("?")[0] and new_url != minted  # only the token changed
        assert _cells(renewed)["CAM_rtsp"]["url"] == RTSP_URL
        assert [u["url"] for u in _cells(renewed)["CAM_rtsp"]["LocationUris"]] == [new_url, RTSP_URL]
        starts = [entry for entry in lumana.log if entry[0] == "start"]
        assert len(starts) == 2 and starts[1][2] and lumana.open_sessions() == 1  # the session was reused
        record = sp_engine._load("pc:PC_sp")
        _wait(lambda: client.get("truelive:sp:ack:PC_sp") == record["sent_id"].encode(), LIMIT_S, "no refresh ACK")
        with db_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM pc_deployments")).scalar() == 1
            audits = conn.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'config.deployed'")).scalar()
            assert audits == 1
        # Acknowledged and unchanged: another tick sends nothing.
        tasks.send_task("app.tasks.stream_provider_tasks.tick")
        time.sleep(3)
        assert len(station.configs) == 2

        # 3. Reconnect with the acknowledgement lost: the relay queues the check, the worker delivers once.
        station.close()
        client.delete("truelive:sp:ack:PC_sp")
        station = Pc(relay.url, "PC_sp", token)
        _wait(lambda: len(station.configs) >= 1, LIMIT_S, "nothing was delivered after the reconnect")
        assert station.configs[0]["content"]["config_id"] == config_id
        time.sleep(2)
        assert len(station.configs) == 1

        # 4. A failure report from a station that reports (a later Client): the closed session is reopened.
        station.close()
        station = Pc(relay.url, "PC_sp", token, capabilities=[sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS],
                     versions={"client_version": "1.4.0"})
        lumana.close_session()
        before = lumana.count("start")
        source_id = _cells(renewed)["CAM_lumana"]["id"]
        for n in range(3):  # a failing cell is reported again and again
            ack = station.call("station_status", {"message_id": f"st-{n}", "ts": time.time(), "health": "degraded",
                                                  "wall": {"running_config_id": config_id, "streams_total": 2,
                                                           "streams_playing": 1, "streams_failed": [source_id]}})
            assert ack["status"] == "ok"
            time.sleep(0.5)
        _wait(lambda: lumana.open_sessions() == 1, LIMIT_S, "the closed session was never reopened")
        time.sleep(2)
        assert lumana.count("start") == before + 1  # once: the relay's 60 s limit and the kept address
        assert client.exists("truelive:sp:rl:fail:PC_sp")
        assert sp_engine.peek(reference)

        logs = [relay.proc.text, worker.proc.text]
    finally:
        if station is not None:
            station.close()
        if worker is not None:
            logs.append(worker.proc.text)
            worker.stop()
        if relay is not None:
            logs.append(relay.proc.text)
            relay.stop()
        lumana.stop()
        client.close()
        redis_server.stop()
        db_engine.dispose()

    # Nothing secret in the relay's or the worker's log.
    text_log = "\n".join(logs)
    assert "Stream addresses" in text_log and "stream_provider_tasks.tick" in text_log
    for secret in [key, fernet_key, token, jwt_secret, sender_secret, "?token=", "/v2/file/"] + list(lumana.tokens):
        assert secret not in text_log
