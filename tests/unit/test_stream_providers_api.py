"""
Stream providers through the API (LUM-SRV): provider CRUD and its test
action, admin or stream_providers:manage access, the key never returned, logged or audited, delete
refused while cameras use the provider, provider cameras in the camera API,
the test-stream endpoint, snapshots, ``hls_capable`` and the PC health block.
Routes run through TestClient on SQLite with the engine's rig
(tests/stream_provider_testkit.py: fake Lumana on 127.0.0.1, FakeRedis).

    python -m pytest tests/unit/test_stream_providers_api.py -o addopts='' -p no:cacheprovider
"""

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.camera import Camera
from app.models.pc_tracking import PcHealth
from app.models.snapshot import Snapshot
from app.models.stream_provider import StreamProvider
from app.services import snapshot_service
from app.services.stream_providers import engine, registry
from app.services.system_settings_service import system_settings_service
from app.utils.url_processor import make_provider_ref
from tests.deploy_testkit import camera_url
from tests.stream_provider_testkit import TEAM, Rig, new_key, ref, register_fixed

BASE = "/stream-providers"


@pytest.fixture
def api(monkeypatch):
    from app.api import deps
    from app.api.v1 import cameras as cameras_api
    from app.api.v1 import pcs as pcs_api
    from app.api.v1 import stream as stream_api
    from app.api.v1 import stream_providers as providers_api
    from app.database import get_db

    rig = Rig(monkeypatch)
    Snapshot.__table__.create(rig.db_engine)
    rig.add_camera("CAM_RTSP", camera_url(), "RTSP camera")  # creates the site and the device
    user = SimpleNamespace(role="admin", is_active=True, username="fixture-admin", user_id="u-1")
    audits = []

    def record(db, **fields):
        audits.append(fields)

    async def current_user(*_args, **_kwargs):
        return user

    def _db():
        session = rig.Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(providers_api.router, prefix=BASE)
    app.include_router(cameras_api.router, prefix="/cameras")
    app.include_router(pcs_api.router, prefix="/pcs")
    app.include_router(stream_api.router, prefix="/stream")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    monkeypatch.setattr(deps, "get_current_user", current_user)  # the *_or_scope closures call it directly
    monkeypatch.setattr("app.services.audit_service.create_audit_log", record)
    monkeypatch.setattr("app.services.audit_service.record_event", record)
    monkeypatch.setattr("app.api.v1.alert_addresses._provision_address", lambda db, camera_id: None)
    monkeypatch.setattr(system_settings_service, "key_status", "ok")  # as with SETTINGS_ENCRYPTION_KEY set
    client = TestClient(app, headers={"Authorization": "Bearer fixture"})
    yield SimpleNamespace(client=client, rig=rig, user=user, audits=audits, monkeypatch=monkeypatch,
                          stream=stream_api)
    client.close()
    rig.close()
    registry.PROVIDER_TYPES.pop("fixed", None)


def _create(api, name="Lumana main", key=None, **settings):
    body = {"name": name, "type": "lumana",
            "settings": {"api_key": key or api.rig.key, "base_url": api.rig.lumana.url, **settings}}
    response = api.client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _camera(api, camera_id, url, expected=201):
    response = api.client.post("/cameras", json={"id": camera_id, "device_id": "DEV_sp", "name": camera_id,
                                                 "rtsp_url": url})
    assert response.status_code == expected, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# CRUD, test, delete
# --------------------------------------------------------------------------- #


def test_types_describe_the_form(api):
    types = api.client.get(f"{BASE}/types").json()
    assert [t["type"] for t in types] == ["lumana"]
    assert [(f["name"], f["kind"], f["required"]) for f in types[0]["fields"]] == [
        ("api_key", "secret", True), ("base_url", "url", False)]
    register_fixed()
    assert {t["type"] for t in api.client.get(f"{BASE}/types").json()} == {"lumana", "fixed"}


def test_create_list_update(api):
    created = _create(api)
    assert created["type"] == "lumana" and created["type_label"] == "Lumana"
    assert created["settings"] == {"base_url": api.rig.lumana.url}
    assert created["secrets_set"] == {"api_key": True} and created["secrets_changed_at"]
    assert created["cameras_count"] == 0 and created["status"]["state"] == "unused"
    [listed] = api.client.get(BASE).json()
    assert listed["id"] == created["id"] and listed["name"] == "Lumana main"
    assert api.client.get(f"{BASE}/options").json() == [{"id": created["id"], "name": "Lumana main",
                                                         "type": "lumana"}]
    # An update without the key keeps the stored one.
    response = api.client.put(f"{BASE}/{created['id']}", json={"name": "Lumana renamed", "settings": {}})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["name"] == "Lumana renamed" and updated["settings"] == {"base_url": api.rig.lumana.url}
    assert updated["secrets_changed_at"] == created["secrets_changed_at"] and updated["secrets_set"]["api_key"]
    assert api.client.post(f"{BASE}/{created['id']}/test").json() == {"ok": True,
                                                                      "message": "Lumana accepted the API key"}
    # An empty key keeps it too; a new key replaces it.
    api.client.put(f"{BASE}/{created['id']}", json={"name": "Lumana renamed", "settings": {"api_key": "  "}})
    assert api.client.post(f"{BASE}/{created['id']}/test").json()["ok"] is True
    time.sleep(0.01)
    replaced = api.client.put(f"{BASE}/{created['id']}",
                              json={"name": "Lumana renamed", "settings": {"api_key": "wrong-" + new_key()}}).json()
    assert replaced["secrets_changed_at"] != created["secrets_changed_at"]
    assert api.client.post(f"{BASE}/{created['id']}/test").json() == {
        "ok": False, "code": "auth", "message": "Lumana refused the API key"}
    assert api.rig.lumana.open_sessions() == 0  # the test action opens no session


def test_default_base_url_and_validation(api):
    created = api.client.post(BASE, json={"name": "Defaults", "type": "lumana",
                                          "settings": {"api_key": new_key()}}).json()
    assert created["settings"] == {"base_url": "https://access.lumana.ai"}
    for body, text in [
        ({"name": "x", "type": "nope", "settings": {}}, "Unknown provider type"),
        ({"name": "x", "type": "lumana", "settings": {}}, "API key is required"),
        ({"name": "x", "type": "lumana", "settings": {"api_key": "k", "base_url": "ftp://h"}},
         "API address must start with http:// or https://"),
        ({"name": "x", "type": "lumana", "settings": {"api_key": "k", "extra": 1}}, "Unknown field(s) for Lumana"),
        ({"name": "   ", "type": "lumana", "settings": {"api_key": "k"}}, "Name is required"),
    ]:
        response = api.client.post(BASE, json=body)
        assert response.status_code == 422 and text in response.text, response.text
    assert api.client.post(BASE, json={"name": "Defaults", "type": "lumana",
                                       "settings": {"api_key": "k"}}).status_code == 409  # the name is taken
    assert api.client.put(f"{BASE}/{created['id']}", json={"name": "Defaults", "type": "fixed",
                                                           "settings": {}}).status_code == 422
    assert api.client.put(f"{BASE}/missing", json={"name": "n", "settings": {}}).status_code == 404
    assert api.client.post(f"{BASE}/missing/test").status_code == 404


def test_saving_a_key_needs_the_shared_encryption_key(api):
    api.monkeypatch.setattr(system_settings_service, "key_status", "no_key")
    response = api.client.post(BASE, json={"name": "No key", "type": "lumana", "settings": {"api_key": new_key()}})
    assert response.status_code == 409
    assert response.json()["detail"] == "Set SETTINGS_ENCRYPTION_KEY before saving a provider key"
    assert api.client.get(BASE).json() == []
    api.monkeypatch.setattr(system_settings_service, "key_status", "ok")
    created = _create(api)
    api.monkeypatch.setattr(system_settings_service, "key_status", "no_key")
    # Without a new key the provider can still be renamed.
    assert api.client.put(f"{BASE}/{created['id']}", json={"name": "Renamed", "settings": {}}).status_code == 200
    assert api.client.put(f"{BASE}/{created['id']}",
                          json={"name": "Renamed", "settings": {"api_key": new_key()}}).status_code == 409


def test_two_providers_of_the_same_type(api):
    other = new_key()
    api.rig.lumana.keys.add(other)
    first, second = _create(api, "Account one"), _create(api, "Account two", key=other)
    assert first["id"] != second["id"]
    with api.rig.db() as db:
        resolved = engine.resolve_refs(db, [ref(first["id"], 1), ref(second["id"], 2)], budget_sec=5)
    assert all(r.url for r in resolved.values())
    assert {e[1]: e[3] for e in api.rig.lumana.log if e[0] == "start"} == {"cam1": api.rig.key, "cam2": other}


def test_non_admin_is_refused_everywhere_but_options(api):
    created = _create(api)
    api.user.role = "user"
    body = {"name": "n", "type": "lumana", "settings": {"api_key": "k"}}
    calls = [("get", f"{BASE}/types", None), ("get", BASE, None), ("post", BASE, body),
             ("put", f"{BASE}/{created['id']}", body), ("post", f"{BASE}/{created['id']}/test", None),
             ("delete", f"{BASE}/{created['id']}", None)]
    for method, url, payload in calls:
        response = api.client.request(method, url, json=payload)
        assert response.status_code == 403, (method, url, response.status_code)
    options = api.client.get(f"{BASE}/options")
    assert options.status_code == 200 and set(options.json()[0]) == {"id", "name", "type"}
    # hls_capable is an admin's to set, like every PC field.
    assert api.client.post("/pcs", json={"id": "PC_X", "name": "x", "team_id": TEAM,
                                         "hls_capable": True}).status_code == 403
    api.user.role = "admin"
    assert api.client.get(BASE).status_code == 200


def test_key_is_never_returned_logged_or_audited(api, caplog):
    caplog.set_level(logging.DEBUG)
    key = new_key()
    api.rig.lumana.keys.add(key)
    created = _create(api, key=key)
    provider = created["id"]
    _camera(api, "CAM_P1", make_provider_ref(provider, "cam1"))
    replies = [
        created,
        api.client.get(BASE).json(),
        api.client.get(f"{BASE}/options").json(),
        api.client.get(f"{BASE}/types").json(),
        api.client.put(f"{BASE}/{provider}", json={"name": "Renamed", "settings": {"api_key": key}}).json(),
        api.client.post(f"{BASE}/{provider}/test").json(),
        api.client.get("/cameras").json(),
        api.client.get("/cameras/CAM_P1").json(),
        api.client.delete(f"{BASE}/{provider}").json(),  # refused: a camera uses it
    ]
    text = json.dumps(replies)
    assert key not in text and "api_key" in text  # only the field's name and that it is set
    assert key not in caplog.text
    assert key not in json.dumps(api.audits, default=str)
    provider_audits = [a for a in api.audits if a["resource_type"] == "stream_provider"]
    assert [(a["action"], a["changes"]["fields"]) for a in provider_audits] == [
        ("stream_provider.created", ["api_key", "base_url"]), ("stream_provider.updated", ["api_key", "name"])]
    with api.rig.db() as db:
        row = db.get(StreamProvider, provider)
        assert key not in (row.secrets or "") and key not in json.dumps(row.settings)  # encrypted at rest
        assert engine.decrypt_secrets(row) == {"api_key": key}


def test_delete_is_refused_while_cameras_use_the_provider(api):
    provider = _create(api)["id"]
    other = _create(api, "Other")["id"]
    for n in range(12):
        _camera(api, f"CAM_P{n:02d}", make_provider_ref(provider, f"cam{n + 1}"))
    assert api.client.get(BASE).json()[0]["cameras_count"] == 12
    response = api.client.delete(f"{BASE}/{provider}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith("12 camera(s) still use this provider: CAM_P00, CAM_P01") and detail.endswith("...")
    assert "CAM_P10" not in detail  # the first ten names
    # A provider nobody uses goes, and gives its sessions back first.
    with api.rig.db() as db:
        engine.resolve_refs(db, [ref(other, 1)], budget_sec=5)
    assert api.rig.lumana.open_sessions() == 1
    assert api.client.delete(f"{BASE}/{other}").status_code == 204
    assert api.rig.lumana.open_sessions() == 0 and api.rig.redis.keys(engine.PREFIX + f"addr:{other}:*") == []
    assert [p["id"] for p in api.client.get(BASE).json()] == [provider]
    with api.rig.db() as db:  # the cameras go (directly: the camera API's cascades need more tables)
        db.query(Camera).filter(Camera.id.like("CAM_P%")).delete(synchronize_session=False)
        db.commit()
    assert api.client.delete(f"{BASE}/{provider}").status_code == 204
    assert api.client.delete(f"{BASE}/{provider}").status_code == 404


def test_update_of_the_settings_drops_the_cached_addresses(api):
    provider = _create(api)["id"]
    with api.rig.db() as db:
        engine.resolve_refs(db, [ref(provider, 1)], budget_sec=5)
    assert api.rig.addr(ref(provider, 1))
    api.client.put(f"{BASE}/{provider}", json={"name": "Renamed only", "settings": {}})
    assert api.rig.addr(ref(provider, 1))  # a rename keeps them
    api.client.put(f"{BASE}/{provider}", json={"name": "Renamed only", "settings": {"api_key": api.rig.key}})
    assert api.rig.addr(ref(provider, 1)) is None and api.rig.lumana.open_sessions() == 0


def test_second_type_needs_no_new_api_code(api):
    register_fixed()
    response = api.client.post(BASE, json={"name": "Fixed one", "type": "fixed",
                                           "settings": {"host": "https://fixed.example/", "channel": "7"}})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["settings"] == {"host": "https://fixed.example", "channel": 7}
    assert created["secrets_set"] == {"pin": False} and created["secrets_changed_at"] is None
    assert api.client.post(f"{BASE}/{created['id']}/test").json() == {"ok": True, "message": "Fixed is fine"}
    assert api.client.post(BASE, json={"name": "Fixed two", "type": "fixed",
                                       "settings": {"host": "https://h", "channel": "x"}}).status_code == 422


# --------------------------------------------------------------------------- #
# Cameras
# --------------------------------------------------------------------------- #


def test_camera_create_and_update_with_a_reference(api):
    provider = _create(api)["id"]
    reference = make_provider_ref(provider, "cam1")
    created = _camera(api, "CAM_P1", reference)
    assert created["rtsp_url"] == reference
    assert created["stream_source"] == {
        "kind": "provider", "provider_id": provider, "provider_name": "Lumana main", "provider_type": "lumana",
        "provider_camera_id": "cam1",
        "status": {"state": "unused", "message": None, "valid_until": None, "renewed_at": None,
                   "last_error_at": None, "code": None}}
    # A provider that does not exist, or a malformed reference, is refused.
    for bad in ("provider://missing-provider/cam1", f"provider://{provider}", "provider://"):
        assert _camera(api, "CAM_BAD", bad, expected=422)["detail"] == "Stream provider not found"
    assert api.client.put("/cameras/CAM_P1", json={"rtsp_url": "provider://missing/cam9"}).status_code == 422
    # An RTSP camera becomes a provider camera and back.
    assert api.client.get("/cameras/CAM_RTSP").json()["stream_source"] == {"kind": "url"}
    moved = api.client.put("/cameras/CAM_RTSP", json={"rtsp_url": make_provider_ref(provider, "cam2")}).json()
    assert moved["stream_source"]["provider_camera_id"] == "cam2"
    back = api.client.put("/cameras/CAM_RTSP", json={"rtsp_url": camera_url()}).json()
    assert back["rtsp_url"] == camera_url() and back["stream_source"] == {"kind": "url"}
    # Renaming a provider camera does not re-check its (unchanged) reference.
    assert api.client.put("/cameras/CAM_P1", json={"name": "Gate"}).json()["name"] == "Gate"
    # The status follows the address cache.
    with api.rig.db() as db:
        engine.resolve_refs(db, [reference], budget_sec=5)
    listed = {c["id"]: c for c in api.client.get("/cameras").json()}
    status = listed["CAM_P1"]["stream_source"]["status"]
    assert status["state"] == "ok" and status["valid_until"] == api.rig.clock.t + 18000
    assert listed["CAM_RTSP"]["stream_source"] == {"kind": "url"}
    assert "token" not in json.dumps(listed) and "/v2/file/" not in json.dumps(listed)
    by_device = api.client.get("/cameras/device/DEV_sp").json()
    assert {c["id"]: c["stream_source"]["kind"] for c in by_device} == {"CAM_P1": "provider", "CAM_RTSP": "url"}


def test_rtsp_and_hls_cameras_are_stored_unchanged(api):
    hls = "https://cdn.example/live/cam.m3u8?token=a@b:c"
    assert _camera(api, "CAM_HLS", hls)["rtsp_url"] == hls
    assert _camera(api, "CAM_R2", camera_url())["rtsp_url"] == camera_url()
    assert api.rig.lumana.log == [] and not api.rig.redis.keys(engine.PREFIX + "*")


def test_camera_listing_survives_a_redis_outage(api):
    provider = _create(api)["id"]
    _camera(api, "CAM_P1", make_provider_ref(provider, "cam1"))
    api.rig.fake_redis.stop()
    api.rig.redis.connection_pool.disconnect()
    response = api.client.get("/cameras")
    assert response.status_code == 200
    assert {c["id"]: c["stream_source"].get("status") for c in response.json()}["CAM_P1"] is None


# --------------------------------------------------------------------------- #
# The test-stream endpoint and snapshots
# --------------------------------------------------------------------------- #


class _NoCapture:
    opened = []

    def __init__(self, url):
        _NoCapture.opened.append(url)

    def isOpened(self):
        return False

    def release(self):
        pass


@pytest.fixture
def no_tools(api):
    """ffprobe and OpenCV are not run: the test is about which address they get."""
    import cv2

    ran = []

    async def no_ffprobe(*cmd, **_kwargs):
        ran.append(list(cmd))
        raise FileNotFoundError("ffprobe")

    _NoCapture.opened = []
    api.monkeypatch.setattr(api.stream.asyncio, "create_subprocess_exec", no_ffprobe)
    api.monkeypatch.setattr(cv2, "VideoCapture", _NoCapture)
    return ran


def test_test_stream_endpoint_for_a_provider_camera(api, no_tools):
    provider = _create(api)["id"]
    reference = make_provider_ref(provider, "cam1")
    _camera(api, "CAM_P1", reference)
    result = api.client.get("/stream/test/CAM_P1").json()
    assert result["rtsp_url"] == reference  # the reference, never the minted address
    assert result["tests"]["provider"] == {"status": "success", "message": "The provider gave a stream address"}
    minted = api.rig.addr(reference)["url"]
    assert no_tools == [["ffprobe", minted]] and _NoCapture.opened == [minted]  # HLS: no -rtsp_transport
    assert "token" not in json.dumps(result) and "/v2/file/" not in json.dumps(result)
    # It shares the session every station uses: a second test opens nothing new.
    api.client.get("/stream/test/CAM_P1")
    assert api.rig.lumana.count("start") == 1
    # RTSP is tested as before.
    result = api.client.get("/stream/test/CAM_RTSP").json()
    assert result["rtsp_url"] == "rtsp://<redacted>@192.0.2.10/stream1" and "provider" not in result["tests"]
    assert no_tools[-1] == ["ffprobe", "-rtsp_transport", "tcp", camera_url()]


def test_test_stream_endpoint_reports_the_provider_error(api, no_tools):
    provider = _create(api, key="wrong-" + new_key())["id"]
    _camera(api, "CAM_P1", make_provider_ref(provider, "cam1"))
    result = api.client.get("/stream/test/CAM_P1").json()
    assert result["overall_status"] == "failed"
    assert result["tests"] == {"provider": {"status": "failed", "code": "key-refused",
                                            "message": "Lumana main: Lumana refused the API key"}}
    assert no_tools == [] and _NoCapture.opened == []


def test_live_view_resolves_a_provider_camera(api):
    provider = _create(api)["id"]
    reference = make_provider_ref(provider, "cam1")
    _camera(api, "CAM_P1", reference)
    api.monkeypatch.setattr(api.stream, "SessionLocal", api.rig.Session)
    assert asyncio.run(api.stream._resolve_camera_rtsp("CAM_P1")) == api.rig.addr(reference)["url"]
    assert asyncio.run(api.stream._resolve_camera_rtsp("CAM_RTSP")) == camera_url()
    assert asyncio.run(api.stream._resolve_camera_rtsp("missing")) is None
    api.rig.lumana.down = True
    _camera(api, "CAM_P2", make_provider_ref(provider, "cam2"))
    assert asyncio.run(api.stream._resolve_camera_rtsp("CAM_P2")) is None


def test_live_view_notes_its_provider_address_as_in_use(api):
    """While a live view plays, the address is stamped as used, so the tick
    does not stop the vendor session under it after 5 minutes (LUM review)."""
    from app.services.stream_providers import engine

    provider = _create(api)["id"]
    reference = make_provider_ref(provider, "cam1")
    _camera(api, "CAM_P1", reference)
    api.monkeypatch.setattr(api.stream, "SessionLocal", api.rig.Session)
    use_key = engine.PREFIX + engine._ref_key("use", reference)

    async def view(camera_id):
        await api.stream._resolve_camera_rtsp(camera_id)
        api.rig.clock.advance(250)
        async with api.stream._provider_address_in_use(camera_id):
            for _ in range(100):
                if float(api.rig.redis.get(use_key) or 0) == api.rig.clock.t:
                    return True
                await asyncio.sleep(0.02)
        return False

    assert asyncio.run(view("CAM_P1")) is True
    api.rig.clock.advance(250)  # 500 s after the mint, 250 s after the note
    assert api.rig.tick()["released"] == 0 and api.rig.lumana.count("stop") == 0
    assert asyncio.run(view("CAM_RTSP")) is False  # any other camera: nothing is noted


def test_snapshot_skips_a_provider_camera_without_an_address(api):
    provider = _create(api)["id"]
    reference = make_provider_ref(provider, "cam1")
    _camera(api, "CAM_P1", reference)
    captured = []
    api.monkeypatch.setattr(snapshot_service, "get_camera_snapshot", lambda url: captured.append(url))
    with api.rig.db() as db:
        result = snapshot_service.process_camera(db.get(Camera, "CAM_P1"), db, int(time.time()) - 86400)
        assert result == {"camera_id": "CAM_P1", "status": "skipped",
                          "reason": "provider camera with no address in use"}
        assert captured == [] and api.rig.lumana.log == []  # a snapshot never opens a vendor session
        engine.resolve_refs(db, [reference], budget_sec=5)  # a station uses it now
        snapshot_service.process_camera(db.get(Camera, "CAM_P1"), db, int(time.time()) - 86400)
        snapshot_service.process_camera(db.get(Camera, "CAM_RTSP"), db, int(time.time()) - 86400)
    assert captured == [api.rig.addr(reference)["url"], camera_url()] and api.rig.lumana.count("start") == 1


def test_screenshot_skips_a_provider_camera_without_an_address(api):
    from app.models.screenshot import Screenshot
    from app.services import screenshot_service

    Screenshot.__table__.create(api.rig.db_engine)
    provider = _create(api)["id"]
    _camera(api, "CAM_P1", make_provider_ref(provider, "cam1"))
    api.monkeypatch.setattr(screenshot_service, "get_camera_screenshot", lambda url: pytest.fail("captured"))
    with api.rig.db() as db:
        result = screenshot_service.process_camera(db.get(Camera, "CAM_P1"), db, int(time.time()) - 86400)
    assert result["status"] == "skipped" and api.rig.lumana.log == []


# --------------------------------------------------------------------------- #
# PCs
# --------------------------------------------------------------------------- #


def test_hls_capable_on_create_update_and_read(api):
    created = api.client.post("/pcs", json={"id": "PC_X", "name": "x", "team_id": TEAM})
    assert created.status_code == 201 and created.json()["hls_capable"] is None  # not confirmed
    assert api.client.put("/pcs/PC_X", json={"hls_capable": True}).json()["hls_capable"] is True
    assert api.client.get("/pcs/PC_X").json()["hls_capable"] is True
    assert api.client.put("/pcs/PC_X", json={"name": "renamed"}).json()["hls_capable"] is True  # left alone
    assert api.client.put("/pcs/PC_X", json={"hls_capable": False}).json()["hls_capable"] is False
    made = api.client.post("/pcs", json={"id": "PC_Y", "name": "y", "team_id": TEAM, "hls_capable": True})
    assert made.json()["hls_capable"] is True


def test_pc_health_carries_the_provider_block(api):
    rig = api.rig
    provider = _create(api)["id"]
    rig.add_pc("PC_A", hls_capable=None)
    rig.add_pc("PC_B")
    rig.add_pc("PC_C")
    with rig.db() as db:
        for pc_id in ("PC_A", "PC_B", "PC_C"):
            db.add(PcHealth(pc_id=pc_id, registered_at=int(time.time())))
        db.commit()
    for pc_id in ("PC_A", "PC_B"):
        rig.set_layout(pc_id, [ref(provider, 1), ref(provider, 2)])
    rig.set_layout("PC_C", [])
    rig.deploy("PC_A", "PC_B", "PC_C")
    old_core = api.client.get("/pcs/PC_A/health").json()["provider_streams"]
    assert old_core["cameras"] == 2 and old_core["blocked"] is None
    assert [(u["camera_id"], u["camera_name"], u["code"]) for u in old_core["unresolved"]] == [
        ("CAM_0", "Camera 0", "core-too-old"), ("CAM_1", "Camera 1", "core-too-old")]
    assert old_core["unresolved"][0]["message"] == ("This station is not confirmed to run TrueLive Live 1.3.0 or "
                                                    "later; provider cameras are not sent to it.")
    good = api.client.get("/pcs/PC_B/health").json()["provider_streams"]
    assert good == {"cameras": 2, "unresolved": [], "last_delivery_at": rig.clock.t, "acknowledged": True,
                    "next_renewal_at": rig.clock.t + 9000, "blocked": None}
    assert api.client.get("/pcs/PC_C/health").json()["provider_streams"] is None  # no provider camera
    assert "token" not in json.dumps([old_core, good])


# --------------------------------------------------------------------------- #
# Service accounts (VMS): stream_providers:manage
# --------------------------------------------------------------------------- #


@pytest.fixture
def sa(api):
    """A service-account principal; its tokens start with ``tlsa_``."""
    from app.api import deps

    account = SimpleNamespace(id="sa-vms", name="vms", scopes=["stream_providers:manage"])

    async def current_sa(*_args, **_kwargs):
        return account

    api.monkeypatch.setattr(deps, "get_current_service_account", current_sa)
    api.client.headers["Authorization"] = "Bearer tlsa_fixture"
    return account


def test_service_account_with_the_scope_manages_providers(api, sa):
    created = _create(api)
    provider = created["id"]
    assert [p["id"] for p in api.client.get(BASE).json()] == [provider]
    assert api.client.get(f"{BASE}/types").json()[0]["type"] == "lumana"
    assert api.client.get(f"{BASE}/options").status_code == 200
    updated = api.client.put(f"{BASE}/{provider}", json={"name": "Lumana VMS", "settings": {}})
    assert updated.status_code == 200 and updated.json()["name"] == "Lumana VMS"
    assert api.client.post(f"{BASE}/{provider}/test").json()["ok"] is True
    with api.rig.db() as db:
        row = db.get(StreamProvider, provider)
        assert (row.created_by_type, row.created_by_id, row.created_by_label) == ("service_account", "sa-vms", "vms")
        assert row.updated_by_type == "service_account"
    assert {a["actor"] for a in api.audits if a["resource_type"] == "stream_provider"} == {
        ("service_account", "sa-vms", "vms")}
    # A camera of the provider through cameras:manage, then the provider goes once the camera is unset.
    sa.scopes = ["stream_providers:manage", "cameras:manage"]
    camera = _camera(api, "CAM_VMS", make_provider_ref(provider, "cam1"))
    assert camera["stream_source"]["provider_id"] == provider
    assert api.client.delete(f"{BASE}/{provider}").status_code == 409
    assert api.client.put("/cameras/CAM_VMS", json={"rtsp_url": camera_url()}).status_code == 200
    assert api.client.delete(f"{BASE}/{provider}").status_code == 204
    assert api.client.get(BASE).json() == []


def test_service_account_without_the_scope_is_refused(api, sa):
    provider = _create(api)["id"]
    sa.scopes = ["cameras:read", "sites:read", "teams:manage"]
    body = {"name": "n", "type": "lumana", "settings": {"api_key": "k"}}
    calls = [("get", f"{BASE}/types", None), ("get", BASE, None), ("post", BASE, body),
             ("put", f"{BASE}/{provider}", body), ("post", f"{BASE}/{provider}/test", None),
             ("delete", f"{BASE}/{provider}", None), ("get", f"{BASE}/options", None)]
    for method, url, payload in calls:
        response = api.client.request(method, url, json=payload)
        assert response.status_code == 403, (method, url, response.status_code)
        assert "stream_providers:manage" in response.json()["detail"]
    # cameras:manage alone reads the options (the camera form's list), nothing else.
    sa.scopes = ["cameras:manage"]
    assert api.client.get(f"{BASE}/options").json() == [{"id": provider, "name": "Lumana main", "type": "lumana"}]
    assert api.client.get(BASE).status_code == 403
    # The admin JWT still works.
    api.client.headers["Authorization"] = "Bearer fixture"
    assert api.client.get(BASE).status_code == 200


def test_service_account_never_gets_the_key(api, sa):
    key = new_key()
    api.rig.lumana.keys.add(key)
    created = _create(api, key=key)
    replies = [created, api.client.get(BASE).json(), api.client.get(f"{BASE}/options").json(),
               api.client.put(f"{BASE}/{created['id']}", json={"name": "R", "settings": {"api_key": key}}).json(),
               api.client.post(f"{BASE}/{created['id']}/test").json()]
    text = json.dumps(replies)
    assert key not in text and created["secrets_set"] == {"api_key": True}
    assert key not in json.dumps(api.audits, default=str)


def test_scope_is_a_valid_service_account_scope():
    from app.schemas.service_account import ServiceAccountCreate

    assert ServiceAccountCreate(name="vms", scopes=["stream_providers:manage"]).scopes == ["stream_providers:manage"]
