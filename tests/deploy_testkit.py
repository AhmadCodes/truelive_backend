"""
Test kit for the deploy path (plan 06 D14) and the relay ingestion after it.

* ``sqlite_engine(path)``: a SQLite engine with foreign keys on. With a file
  path, every session has its own connection, so a test can tell committed
  rows from uncommitted ones ("committed before the send").
* ``create_deploy_tables(engine)``: teams, screen_layouts, pcs and
  pc_deployments only (plan 6.1: only the tables a test needs; audit_logs
  uses PG UUID columns and is checked on Postgres).
* ``seed_fleet(session, pc_ids)``: a team, a layout and PCs on that layout.
* ``synthetic_layout(pc_id)``: a generated-layout lookalike whose camera URL
  carries credentials. The credential is assembled at run time, so no file
  in the repo matches ``rtsp://user:pass@``; it must never reach a response.
* ``FakeSender``: stands in for ``deploy_service.socketio_sender``. Per PC it
  answers delivered / offline / error, says nothing (timeout) or raises
  RelayUnreachable; ``on_send`` runs before the answers, to look at the
  database at send time or to let a "relay" write first.
* ``AuditRecorder``: replaces audit_service.record_event in deploy_service.
"""

import secrets
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every mapper the ORM needs)
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.models.screen_layout import ScreenLayout
from app.models.team import Team

LAYOUT_ID = "LAYOUT_d14"
LAYOUT_NAME = "D14 wall"
TEAM_ID = "TEAM_d14"
CAMERA_HOST = "192.0.2.10"  # TEST-NET-1


def camera_url() -> str:
    """A credentialed camera URL, built at run time (never written to a file)."""
    return "rtsp://" + "fixture-user" + ":" + "fixture-" + "pass" + "@" + CAMERA_HOST + "/stream1"


def synthetic_layout(pc_id: str, marker: str = "") -> Dict[str, Any]:
    url = camera_url()
    return {
        "pc_id": pc_id,
        "pc_name": f"{pc_id} wall",
        "width": 640,
        "height": 480,
        "screens": [
            {
                "id": f"pc{pc_id}_layout{LAYOUT_ID}_screen1",
                "display_idx": 0,
                "switchInterval": 10,
                "title": marker or "Screen 1",
                "source_groups": [[{"id": "src-1", "url": url, "LocationUris": [url], "use_tcp": False}]],
            },
            {"id": f"pc{pc_id}_layout{LAYOUT_ID}_screen2", "display_idx": 1, "switchInterval": 10,
             "title": "Screen 2", "source_groups": []},
        ],
    }


def sqlite_engine(path: Optional[str] = None):
    if path is None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_deploy_tables(engine) -> None:
    meta = MetaData()
    for model in (Team, ScreenLayout, PC, PcDeployment):
        model.__table__.to_metadata(meta)
    meta.create_all(engine)


def seed_fleet(session, pc_ids, *, layout: bool = True) -> None:
    session.add(Team(id=TEAM_ID, name="D14 team"))
    session.add(ScreenLayout(id=LAYOUT_ID, name=LAYOUT_NAME, team_id=TEAM_ID))
    session.flush()
    for pc_id in pc_ids:
        session.add(PC(id=pc_id, name=f"{pc_id} wall", role="controller", team_id=TEAM_ID,
                       screen_layout_id=LAYOUT_ID if layout else None,
                       auth_token="fixture-token-" + secrets.token_hex(4)))
    session.commit()


def patch_generator(monkeypatch, layouts: Optional[Dict[str, Dict[str, Any]]] = None,
                    fail_for=()) -> Dict[str, Dict[str, Any]]:
    """Patch config_loader / config_generator: PC ``p`` gets ``layouts[p]``
    (default synthetic_layout(p)); PCs in ``fail_for`` raise."""
    layouts = {} if layouts is None else layouts

    def load(pc_id, db):
        return {"pc": pc_id}

    def generate(site_config, db):
        pc_id = site_config["pc"]
        if pc_id in fail_for:
            raise RuntimeError("generation failed (simulated)")
        return dict(layouts.get(pc_id) or synthetic_layout(pc_id))

    monkeypatch.setattr("app.services.config_loader.load_pc_config", load)
    monkeypatch.setattr("app.services.config_generator.generate_config", generate)
    return layouts


class FakeSender:
    """``answers`` maps a PC id to delivered (default), delivered_confirmable,
    offline, error, timeout (no answer); ``unreachable=True`` raises."""

    def __init__(self, answers: Optional[Dict[str, str]] = None, *, unreachable: bool = False,
                 on_send: Optional[Callable[[List[Dict[str, Any]]], None]] = None):
        self.answers = answers or {}
        self.unreachable = unreachable
        self.on_send = on_send
        self.calls: List[List[Dict[str, Any]]] = []

    def __call__(self, requests):
        from app.services.deploy_service import RelayUnreachable

        self.calls.append(requests)
        if self.unreachable:
            raise RelayUnreachable("ConnectionError")
        if self.on_send is not None:
            self.on_send(requests)
        out: Dict[str, Optional[Any]] = {}
        for request in requests:
            kind = self.answers.get(request["targetId"], "delivered")
            config_id = request["config_id"]
            if kind == "timeout":
                out[config_id] = None
            elif kind == "delivered_confirmable":
                out[config_id] = {"status": "delivered", "config_id": config_id, "confirmable": True}
            elif kind == "error":
                out[config_id] = {"status": "error", "config_id": config_id, "confirmable": False,
                                  "reason": "invalid content"}
            else:
                out[config_id] = {"status": kind, "config_id": config_id, "confirmable": False}
        return out

    @property
    def requests(self) -> List[Dict[str, Any]]:
        return [r for call in self.calls for r in call]


class AuditRecorder:
    def __init__(self, monkeypatch):
        self.entries: List[Dict[str, Any]] = []
        monkeypatch.setattr("app.services.deploy_service.audit_service.record_event", self._record)

    def _record(self, db, *, action, resource_type, resource_id, actor, changes=None, commit=False):
        self.entries.append({"action": action, "resource_type": resource_type, "resource_id": resource_id,
                             "actor": actor, "changes": changes})


def find_key(obj: Any, key: str) -> bool:
    """True when ``key`` is a dict key anywhere in ``obj``."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                return True
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False
