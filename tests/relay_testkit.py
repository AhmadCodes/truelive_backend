"""
Test kit for relay handler tests (plan 06 section 6.1: relay handlers called
directly with ``sio.emit`` and ``get_db`` monkeypatched). Used by D12 and the
relay tasks after it (D14, D15, C10, D30).

* ``import_relay()`` imports app.services.websocket_server in its "threading"
  async mode when it is not imported yet, so it also loads where eventlet 0.33
  cannot (Python 3.12). The env variable is set only for the import and then
  restored, so relay subprocesses started by integration tests still run
  under eventlet.
* ``RelayHarness`` records every emit per recipient sid (rooms are expanded
  from ``enter_room`` calls), serves ``get_db`` from an in-memory SQLite
  database that holds only the ``pcs`` table, and mints PC tokens with the
  process's throwaway JWT_SECRET.
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import jwt
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

RELAY_MODULE = "app.services.websocket_server"
ASYNC_MODE_ENV = "TRUELIVE_RELAY_ASYNC_MODE"


def import_relay():
    if RELAY_MODULE in sys.modules:
        return sys.modules[RELAY_MODULE]
    previous = os.environ.get(ASYNC_MODE_ENV)
    os.environ[ASYNC_MODE_ENV] = "threading"
    try:
        import app.models  # noqa: F401  (registers every mapper the ORM needs)
        import app.services.websocket_server as relay
    finally:
        if previous is None:
            os.environ.pop(ASYNC_MODE_ENV, None)
        else:
            os.environ[ASYNC_MODE_ENV] = previous
    return relay


def make_pc_token(pc_id: str, secret: str, *, token_type: Optional[str] = "pc_auth", exp_delta: int = 3600,
                  extra: Optional[Dict[str, Any]] = None, algorithm: str = "HS256") -> str:
    payload: Dict[str, Any] = {"pc_id": pc_id, "name": f"{pc_id} name", "exp": int(time.time()) + exp_delta}
    if token_type is not None:
        payload["type"] = token_type
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret, algorithm=algorithm)


class RelayHarness:
    """Drives the relay's handlers in-process."""

    def __init__(self, relay, monkeypatch):
        self.relay = relay
        self.monkeypatch = monkeypatch
        self.emits: List[Tuple[str, Any, Optional[str], Any]] = []
        self.rooms: Dict[str, set] = {}
        self.disconnects: List[str] = []

        relay.connected_pcs.clear()
        relay.session_data.clear()
        for key in relay.relay_counters:
            relay.relay_counters[key] = 0
        monkeypatch.setattr(relay, "_enabled_features", set())
        monkeypatch.setattr(relay, "_legacy_open_warned", False)
        monkeypatch.setattr(relay.sio, "emit", self._emit)
        monkeypatch.setattr(relay.sio, "enter_room", self._enter_room)
        monkeypatch.setattr(relay.sio, "disconnect", lambda sid, *a, **k: self.disconnects.append(sid))

        from app.models.pc import PC

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        table = PC.__table__.to_metadata(MetaData())
        for constraint in list(table.foreign_key_constraints):
            table.constraints.discard(constraint)
        table.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.db_fails = False
        monkeypatch.setattr(relay, "get_db", self._get_db)
        self.secret = relay.settings.JWT_SECRET

    # -- fakes ----------------------------------------------------------------

    def _emit(self, event, data=None, to=None, room=None, skip_sid=None, namespace=None, callback=None, **_):
        self.emits.append((event, data, room if room is not None else to, callback))

    def _enter_room(self, sid, room, namespace=None):
        self.rooms.setdefault(room, set()).add(sid)

    def _get_db(self):
        if self.db_fails:
            raise RuntimeError("database unavailable (simulated)")
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    # -- helpers --------------------------------------------------------------

    def received(self, sid: str, event: Optional[str] = None) -> List[Any]:
        """Payloads delivered to ``sid`` (directly or through a room it joined)."""
        out = []
        for name, data, target, _cb in self.emits:
            if target is None:
                hit = True  # broadcast to everyone
            elif target == sid:
                hit = True
            else:
                hit = sid in self.rooms.get(target, set())
            if hit and (event is None or name == event):
                out.append(data)
        return out

    def events_for(self, sid: str) -> List[str]:
        names = []
        for name, _data, target, _cb in self.emits:
            if target is None or target == sid or sid in self.rooms.get(target, set()):
                names.append(name)
        return names

    def add_pc(self, pc_id: str, token: Optional[str] = "mint", **fields) -> Optional[str]:
        """Insert a PC row; ``token="mint"`` stores a freshly minted pc_auth token."""
        from app.models.pc import PC

        if token == "mint":
            token = make_pc_token(pc_id, self.secret)
        db = self.Session()
        try:
            db.add(PC(id=pc_id, name=fields.pop("name", f"{pc_id} name"), role="controller",
                      team_id=fields.pop("team_id", "TEAM_test"), auth_token=token, **fields))
            db.commit()
        finally:
            db.close()
        return token

    def pc_row(self, pc_id: str):
        from app.models.pc import PC

        db = self.Session()
        try:
            return db.query(PC).filter(PC.id == pc_id).first()
        finally:
            db.close()

    def connect(self, sid: str, auth=None):
        self.relay.connect(sid, {"REMOTE_ADDR": "127.0.0.1"}, auth)

    def connect_sender(self, sid: str = "sender-1", secret: Optional[str] = None):
        from app.services.relay_auth import sender_auth

        self.connect(sid, sender_auth(self.relay.settings.RELAY_SENDER_SECRET if secret is None else secret))

    def register(self, sid: str, pc_id: str, token: str, **extra):
        self.relay.register(sid, {"pc_id": pc_id, "auth_token": token, **extra})

    def errors(self, sid: str) -> List[str]:
        return [d.get("message") for d in self.received(sid, "error")]
