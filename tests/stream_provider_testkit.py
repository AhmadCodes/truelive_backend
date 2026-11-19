"""
Test kit of the stream provider tests (LUM-SRV).

``Rig`` wires the engine to a FakeRedis (tests/fake_redis.py), a FakeLumana
(tests/fake_lumana.py), a fake clock and a SQLite database that holds only
the tables the engine reads. ``Station`` stands in for the relay: it records
what is sent to a PC (deploys and ``refresh_config``) and acknowledges it by
writing the relay's ack key. ``FixedProvider`` is the second provider type
that exists only in tests: a 600 s expiry, no session, no release.
"""

import json
import secrets
from typing import Any, Dict, List, Optional

import redis
from sqlalchemy import MetaData
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.models.camera import Camera
from app.models.device import Device
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth
from app.models.screen_layout import ScreenLayout
from app.models.site import Site
from app.models.stream_provider import StreamProvider
from app.models.team import Team
from app.services.stream_providers import engine, registry
from app.services.stream_providers.base import Address, ProviderError, ProviderField
from app.services.stream_providers.base import StreamProvider as ProviderBase
from app.services.system_settings_service import system_settings_service
from app.utils.url_processor import make_provider_ref
from tests.deploy_testkit import camera_url, sqlite_engine
from tests.fake_lumana import FakeLumana
from tests.fake_redis import FakeRedis

TEAM = "TEAM_sp"
LAYOUT = "LAYOUT_sp"
T0 = 1_800_000_000.0


def new_key() -> str:
    """A provider key for one test (never a real one)."""
    return "fixture-key-" + secrets.token_hex(8)


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


class FixedProvider(ProviderBase):
    """Test-only type: a static address per camera, valid 600 s, nothing to release."""

    type_id = "fixed"
    label = "Fixed"
    fields = (ProviderField("host", "Host", "url"), ProviderField("pin", "PIN", "secret", required=False),
              ProviderField("channel", "Channel", "int", required=False, default=1))

    def __init__(self):
        self.mints: List[str] = []
        self.fail: Optional[ProviderError] = None

    def check(self, settings, http):
        if self.fail:
            raise self.fail
        return "Fixed is fine"

    def mint(self, settings, camera_id, previous, http, last_info=None):
        if self.fail:
            raise self.fail
        self.mints.append(camera_id)
        return Address(url=f"{settings['host']}/fixed/{camera_id}.m3u8?n={len(self.mints)}",
                       expires_at=engine.now() + 600, handle={}, info={})

    def probe(self, address, http):
        return True


def create_tables(db_engine, *models) -> None:
    meta = MetaData()
    for model in models:
        model.__table__.to_metadata(meta)
    meta.create_all(db_engine)


class Station:
    """The relay and the stations behind it, for the engine."""

    def __init__(self, rig: "Rig"):
        self.rig = rig
        self.offline: set = set()
        self.silent: set = set()  # stations that receive but never acknowledge
        self.deploys: List[Dict[str, Any]] = []
        self.refreshes: List[Dict[str, Any]] = []
        self.order: List[Dict[str, Any]] = []  # everything delivered, oldest first

    def __call__(self, requests):
        from app.services import deploy_service

        out = {}
        for request in requests:
            is_refresh = "refresh_id" in request
            delivery_id = request["refresh_id"] if is_refresh else request["config_id"]
            pc_id = request["targetId"]
            if pc_id in self.offline:
                out[delivery_id] = {"status": deploy_service.DELIVERY_OFFLINE}
                continue
            (self.refreshes if is_refresh else self.deploys).append(request)
            self.order.append(request)
            if pc_id not in self.silent and (is_refresh or request.get("track_ack")):
                self.rig.redis.set(f"{engine.PREFIX}ack:{pc_id}", delivery_id)
            out[delivery_id] = ({"status": "delivered"} if is_refresh else
                                {"status": "delivered", "config_id": delivery_id, "confirmable": False})
        return out

    def last(self, pc_id: str) -> Dict[str, Any]:
        """The layout the station got last."""
        sent = [r for r in self.order if r["targetId"] == pc_id]
        assert sent, f"nothing was sent to {pc_id}"
        return sent[-1]["content"]


class Rig:
    def __init__(self, monkeypatch, tmp_path=None):
        self.monkeypatch = monkeypatch
        self.clock = Clock()
        self.fake_redis = FakeRedis()
        self.redis = redis.Redis.from_url(self.fake_redis.url)
        self.key = new_key()
        self.lumana = FakeLumana({self.key}, {f"cam{i}" for i in range(1, 31)}, clock=self.clock)
        self.cfg = dict(engine.DEFAULTS)
        self.connected: List[str] = []
        self.layouts: Dict[str, Dict[str, Any]] = {}
        self.queued: List[tuple] = []

        self.db_engine = sqlite_engine()
        create_tables(self.db_engine, Team, ScreenLayout, PC, PcDeployment, PcHealth, StreamProvider, Site, Device,
                      Camera)
        self.Session = sessionmaker(bind=self.db_engine, autoflush=False)
        with self.Session() as db:
            db.add(Team(id=TEAM, name="SP team"))
            db.add(ScreenLayout(id=LAYOUT, name="SP wall", team_id=TEAM))
            db.commit()

        monkeypatch.setattr(engine, "_redis", self.redis)
        monkeypatch.setattr(engine, "now", self.clock)
        monkeypatch.setattr(engine, "config", lambda db: dict(self.cfg))
        monkeypatch.setattr(engine, "connected_pcs", lambda: list(self.connected))
        monkeypatch.setattr("app.services.config_loader.load_pc_config", lambda pc_id, db: {"pc": pc_id})
        monkeypatch.setattr("app.services.config_generator.generate_config",
                            lambda site_config, db: json.loads(json.dumps(self.layouts[site_config["pc"]])))
        monkeypatch.setattr("app.services.deploy_service.audit_service.record_event", lambda *a, **k: None)
        monkeypatch.setattr("app.services.catchup.send_task",
                            lambda name, args, countdown=None: self.queued.append((name, args, countdown)) or "task")
        self.station = Station(self)
        self.sender = self.station

    def close(self) -> None:
        self.lumana.stop()
        self.redis.close()
        self.fake_redis.stop()

    # -- data -------------------------------------------------------------------

    def db(self):
        return self.Session()

    def add_provider(self, name="Lumana main", key=None, type_id="lumana", settings=None, secrets_=None) -> str:
        with self.Session() as db:
            row = StreamProvider(
                name=name, type=type_id,
                settings=settings if settings is not None else {"base_url": self.lumana.url},
                secrets=system_settings_service._encrypt_value(json.dumps(
                    secrets_ if secrets_ is not None else {"api_key": key or self.key})))
            db.add(row)
            db.commit()
            return row.id

    def add_pc(self, pc_id: str, hls_capable: Optional[bool] = True, connected: bool = True, **health) -> None:
        with self.Session() as db:
            db.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id=TEAM, screen_layout_id=LAYOUT,
                      hls_capable=hls_capable, auth_token="fixture-token-" + secrets.token_hex(4)))
            if health:
                db.add(PcHealth(pc_id=pc_id, **health))
            db.commit()
        if connected:
            self.connected.append(pc_id)

    def add_camera(self, camera_id: str, url: str, name: Optional[str] = None) -> None:
        with self.Session() as db:
            if db.get(Site, "SITE_sp") is None:
                db.add(Site(id="SITE_sp", name="SP site"))
                db.flush()
                db.add(Device(id="DEV_sp", name="SP device", site_id="SITE_sp", nvr_username="fixture",
                              nvr_password="fixture"))
                db.flush()
            db.add(Camera(id=camera_id, device_id="DEV_sp", name=name or camera_id, rtsp_url=url))
            db.commit()

    def set_layout(self, pc_id: str, refs: List[str], title: str = "Screen 1") -> Dict[str, Any]:
        """One screen: a cell per reference (each lists every reference in
        LocationUris, as the generator does) plus one RTSP cell."""
        rtsp = camera_url()
        uris = [{"url": ref, "osd_text": f"Camera {i}"} for i, ref in enumerate(refs)] + [
            {"url": rtsp, "osd_text": "RTSP camera"}]
        cells = [{"id": f"src-{i}", "camera_id": f"CAM_{i}", "camera_name": f"Camera {i}", "url": ref,
                  "LocationUris": json.loads(json.dumps(uris)), "use_tcp": False} for i, ref in enumerate(refs)]
        cells.append({"id": "src-rtsp", "camera_id": "CAM_RTSP", "camera_name": "RTSP camera", "url": rtsp,
                      "LocationUris": json.loads(json.dumps(uris)), "use_tcp": False})
        self.layouts[pc_id] = {"pc_id": pc_id, "pc_name": f"{pc_id} wall", "width": 640, "height": 480,
                               "screens": [{"id": f"{pc_id}_screen1", "display_idx": 0, "switchInterval": 10,
                                            "title": title, "source_groups": [cells]}]}
        return self.layouts[pc_id]

    def deploy(self, *pc_ids: str) -> Dict[str, Any]:
        from app.services import deploy_service

        with self.Session() as db:
            pcs = [db.get(PC, pc_id) for pc_id in pc_ids]
            return deploy_service.deploy_config_to_pcs(pcs, db, trigger="pc_deploy", sender=self.sender)

    def tick(self) -> Dict[str, Any]:
        with self.Session() as db:
            return engine.tick(db, self.sender)

    def refresh(self, pc_id: str, reason: str) -> Dict[str, Any]:
        with self.Session() as db:
            return engine.refresh_pc(db, pc_id, reason, self.sender)

    def addr(self, ref: str) -> Optional[dict]:
        return engine._load(engine._ref_key("addr", ref))


def cell_urls(content: Dict[str, Any]) -> Dict[str, str]:
    return {source["id"]: source["url"] for source in engine._sources(content)}


def ref(provider_id: str, n: int) -> str:
    return make_provider_ref(provider_id, f"cam{n}")


def register_fixed() -> FixedProvider:
    provider = FixedProvider()
    registry.register(provider)
    return provider
