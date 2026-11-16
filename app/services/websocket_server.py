"""
WebSocket server for real-time communication with PC applications.

This Socket.IO server handles:
- PC client registration and authentication
- Configuration deployment to specific PCs
- Online/offline status tracking
- Message routing between portal and PCs

Trust baseline (plan 06 D12, OD-23):

* Two kinds of session. A *sender* session is the portal API pushing layouts;
  it authenticates in the CONNECT auth payload with RELAY_SENDER_SECRET
  (app/services/relay_auth.py). Every other session is a *client*: a PC
  before or after ``register``. With the secret unset the relay runs
  legacy-open: any session that never registered as a PC may still send.
* ``register`` accepts only a valid ``pc_auth`` token for a PC that exists in
  the database. The token's pc_id is authoritative. A token that differs from
  ``pcs.auth_token`` sets ``token_outdated`` (OD-23 (a)), or is rejected when
  RELAY_REQUIRE_CURRENT_TOKEN is true (OD-23 (b)). A second registration of a
  pc_id takes over and is flagged; the old session keeps its connection but no
  longer counts as that PC (``_registered_pc``).
* ``message`` is accepted from sender sessions only, for the types in
  SENDER_MESSAGE_TYPES. A rejected message gets an ``error`` event; nobody is
  disconnected.
* The presence list (clients_update, clients_list) goes to sender sessions
  only; PCs never receive it.
* ``registered`` carries ``features`` and ``protocol_hash`` (D1). A feature is
  advertised only once the handler that serves it exists (``enable_feature``).
* Engine.IO / Socket.IO packet logging follows RELAY_DEBUG_LOGGING (off by
  default): packets carry tokens and layouts (NEW-56).

The relay never rotates JWT_SECRET (OD-23: rotation only through D30).
"""
import functools
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import jwt
import socketio

from app.core.config import settings
from app.database import get_db
from app.models.pc import PC
from app.schemas import station_protocol
from app.services import relay_auth

logger = logging.getLogger(__name__)

# The relay runs under eventlet (production, Python 3.11). "threading" exists
# only so the handlers can be imported and unit-tested where eventlet 0.33
# cannot load (Python 3.12); run_server() refuses it.
ASYNC_MODE_ENV = "TRUELIVE_RELAY_ASYNC_MODE"
ASYNC_MODE = os.environ.get(ASYNC_MODE_ENV, "eventlet")
if ASYNC_MODE not in ("eventlet", "threading"):
    raise ValueError(f"{ASYNC_MODE_ENV} must be 'eventlet' or 'threading'")

# Session roles.
ROLE_SENDER = "sender"
ROLE_CLIENT = "client"
SENDERS_ROOM = "senders"

# Message types a sender may route to a PC (NEW-41). C10/C12 pull station
# config through their own call, so only layouts are pushed today.
SENDER_MESSAGE_TYPES = frozenset({"config"})

PC_TOKEN_TYPE = "pc_auth"
MAX_TOKEN_LENGTH = 4096
MAX_NAME_LENGTH = 255

# Relay-side flags kept per session (and listed to senders).
FLAG_TOKEN_OUTDATED = "token_outdated"
FLAG_DUPLICATE = "duplicate_registration"
FLAG_DB_UNVERIFIED = "db_unverified"

# Messages shown to a PC whose registration is refused (Client 1.1.x shows
# them in a tray notification).
MSG_MISSING = "Missing pc_id or auth_token"
MSG_INVALID_TOKEN = "Invalid or expired authentication token"
MSG_NOT_PC_TOKEN = "Authentication token is not a PC token"
MSG_UNKNOWN_PC = "This PC is not registered in the portal"
MSG_TOKEN_REPLACED = "Authentication token has been replaced; enter the current PC token"
MSG_SENDER_REGISTER = "Sender sessions cannot register as a PC"
MSG_NOT_AUTHORIZED = "Not authorized"


def _packet_logger(name: str, debug_logging: bool) -> logging.Logger:
    """The logger python-socketio / python-engineio log packets to.

    Passing a Logger (not a bool) makes the level explicit: with a bool, the
    libraries set the level only while it is still NOTSET, so the first server
    created in a process would decide it for every later one.
    """
    packet_logger = logging.getLogger(name)
    packet_logger.setLevel(logging.INFO if debug_logging else logging.WARNING)
    return packet_logger


def _make_server(debug_logging: bool, async_mode: str) -> socketio.Server:
    """The relay's Socket.IO server. Packet logging (INFO) only with
    RELAY_DEBUG_LOGGING: packets carry tokens and layouts (NEW-56). Warnings
    and errors are always logged."""
    return socketio.Server(
        async_mode=async_mode,
        cors_allowed_origins="*",
        logger=_packet_logger("socketio.server", debug_logging),
        engineio_logger=_packet_logger("engineio.server", debug_logging),
        ping_timeout=60,
        ping_interval=25,
    )


# Create Socket.IO server
sio = _make_server(settings.RELAY_DEBUG_LOGGING, ASYNC_MODE)


# Create WSGI app wrapper for combined HTTP and WebSocket handling
class WSGIAppWithHTTP:
    """Combined WSGI app for Socket.IO and HTTP endpoints."""

    def __init__(self, socketio_app):
        self.socketio_app = socketio_app

    def __call__(self, environ, start_response):
        """Route requests to Socket.IO or HTTP handlers."""
        path = environ.get("PATH_INFO", "")

        # HTTP endpoint to get connected PCs status
        if path == "/api/connected-pcs" and environ.get("REQUEST_METHOD") == "GET":
            return self.get_connected_pcs(environ, start_response)

        # Default to Socket.IO
        return self.socketio_app(environ, start_response)

    def get_connected_pcs(self, environ, start_response):
        """HTTP endpoint to get list of connected PCs."""
        try:
            clients = []

            for pc_id, pc_sid in connected_pcs.items():
                client_data = session_data.get(pc_sid, {})
                clients.append(
                    {
                        "pc_id": pc_id,
                        "name": client_data.get("name", ""),
                        "connected_at": client_data.get("connected_at"),
                        "is_connected": True,
                    }
                )

            response_data = {"connected_pcs": clients, "total_connected": len(clients)}

            response_body = json.dumps(response_data).encode("utf-8")

            start_response(
                "200 OK",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(response_body))),
                    ("Access-Control-Allow-Origin", "*"),
                ],
            )

            return [response_body]

        except Exception as e:
            logger.error(f"Error in get_connected_pcs endpoint: {e}")
            error_response = json.dumps({"error": str(e)}).encode("utf-8")
            start_response(
                "500 Internal Server Error",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(error_response))),
                ],
            )
            return [error_response]


app = WSGIAppWithHTTP(socketio.WSGIApp(sio))

# PC client tracking: {pc_id: sid}
connected_pcs: Dict[str, str] = {}

# Session data tracking: {sid: {pc_id, name, connected_at, role, flags, ...}}
session_data: Dict[str, Dict[str, Any]] = {}

# Counters for the startup/ops view; never hold identifiers or secrets.
relay_counters: Dict[str, int] = {
    "duplicate_registrations": 0,
    "rejected_registrations": 0,
    "rejected_messages": 0,
}

_legacy_open_warned = False


# --------------------------------------------------------------------------- #
# Feature advertisement (D12, X-17)
# --------------------------------------------------------------------------- #

# Report features follow RELAY_STATION_REPORTS; with reports off they are
# replaced by station_reports.disabled (D9 pauses reporting).
REPORT_FEATURES = (station_protocol.FEATURE_DEPLOY_RESULT, station_protocol.FEATURE_STATION_STATUS)
ADVERTISABLE_FEATURES = (
    station_protocol.FEATURE_DEPLOY_RESULT,
    station_protocol.FEATURE_STATION_STATUS,
    station_protocol.FEATURE_STATION_CONFIG,
    station_protocol.FEATURE_PC_TOKEN,
)
# The relay event that serves each event-backed feature. A feature listed here
# is advertised only while that handler is registered, so a Client never calls
# an event the relay cannot answer (unknown events time out).
FEATURE_EVENTS = {
    station_protocol.FEATURE_DEPLOY_RESULT: "deploy_result",
    station_protocol.FEATURE_STATION_STATUS: "station_status",
    station_protocol.FEATURE_STATION_CONFIG: "get_station_config",
}
_enabled_features: set = set()


def enable_feature(name: str) -> None:
    """Advertise ``name`` in ``registered``. Call it next to the handler that
    serves the feature (D15: deploy_result.v1 / station_status.v1, C10:
    station_config.v1, D30: pc_token.v1)."""
    if name not in ADVERTISABLE_FEATURES:
        raise ValueError(f"unknown relay feature {name!r}")
    _enabled_features.add(name)


def _has_handler(event: str) -> bool:
    return event in sio.handlers.get("/", {})


def advertised_features() -> List[str]:
    """The ``features`` list of ``registered``, in a stable order."""
    reports_on = bool(settings.RELAY_STATION_REPORTS)
    features = []
    for name in ADVERTISABLE_FEATURES:
        if name not in _enabled_features:
            continue
        event = FEATURE_EVENTS.get(name)
        if event is not None and not _has_handler(event):
            continue
        if name in REPORT_FEATURES and not reports_on:
            continue
        features.append(name)
    if not reports_on:
        features.append(station_protocol.FEATURE_STATION_REPORTS_DISABLED)
    return features


def registered_payload(pc_id: str) -> Dict[str, Any]:
    """The ``registered`` event (RegisteredV1). Client 1.1.x reads only pc_id
    and message; the rest is additive."""
    return {
        "pc_id": pc_id,
        "message": "Successfully registered",
        "features": advertised_features(),
        "protocol_hash": station_protocol.protocol_hash(),
    }


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #


def _new_session(role: str) -> Dict[str, Any]:
    return {
        "pc_id": None,
        "name": None,
        "connected_at": datetime.utcnow().isoformat(),
        "role": role,
        "ever_registered": False,
        "flags": {},
        "superseded_by": None,
    }


def legacy_open() -> bool:
    """True when no sender secret is configured (pre-D12 trust model)."""
    return not settings.RELAY_SENDER_SECRET


def _is_sender(sid: str) -> bool:
    """A session that may route messages and see the presence list."""
    session = session_data.get(sid)
    if session is None:
        return False
    if session.get("role") == ROLE_SENDER:
        return True
    # Legacy-open: an unauthenticated session that never registered as a PC
    # (the portal before it learned to authenticate).
    return legacy_open() and not session.get("ever_registered") and session.get("pc_id") is None


def _registered_pc(sid: str) -> Optional[str]:
    """The pc_id this session is registered as, or None.

    The session must be the PC's current registration: a session replaced by a
    later register of the same pc_id (or never registered) gets None, so its
    reports are not accepted (NEW-43, D15).
    """
    session = session_data.get(sid)
    if not session:
        return None
    pc_id = session.get("pc_id")
    if pc_id and connected_pcs.get(pc_id) == sid:
        return pc_id
    return None


def requires_registered_pc(handler: Callable) -> Callable:
    """Decorator for PC report handlers (D15, C10). The wrapped handler is
    called as ``handler(sid, pc_id, data)`` only for the PC's current session;
    any other session gets a ``rejected`` ACK (ReportAckV1 shape)."""

    @functools.wraps(handler)
    def wrapper(sid, data=None):
        pc_id = _registered_pc(sid)
        if pc_id is None:
            return {"status": "rejected", "reason": "not registered"}
        return handler(sid, pc_id, data)

    return wrapper


def _client_entry(pc_id: str, pc_sid: str) -> Dict[str, Any]:
    client_data = session_data.get(pc_sid, {})
    return {
        "pc_id": pc_id,
        "name": client_data.get("name", ""),
        "connected_at": client_data.get("connected_at"),
        "flags": dict(client_data.get("flags") or {}),
    }


def _client_list() -> List[Dict[str, Any]]:
    return [_client_entry(pc_id, pc_sid) for pc_id, pc_sid in list(connected_pcs.items())]


# --------------------------------------------------------------------------- #
# Token and PC checks
# --------------------------------------------------------------------------- #


def validate_token(auth_token: str) -> Dict[str, Any]:
    """
    Validate JWT token and extract payload.

    Args:
        auth_token: JWT token string

    Returns:
        Token payload dict

    Raises:
        jwt.InvalidTokenError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(
            auth_token, settings.JWT_SECRET, algorithms=[settings.ALGORITHM]
        )
        return payload

    except jwt.ExpiredSignatureError:
        logger.error("Token has expired")
        raise

    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid token: {e}")
        raise


def _token_is_current(pc: PC, auth_token: str, db) -> bool:
    """OD-23: the presented token equals the token stored for the PC.

    D30 extends this to also accept the previous token of an unconfirmed
    renewal (pc_token_renewals.previous_token_sha256).
    """
    return relay_auth.same_token(auth_token, pc.auth_token)


class PcCheck:
    """Outcome of the database half of ``register``."""

    OK = "ok"
    UNKNOWN_PC = "unknown_pc"
    TOKEN_REPLACED = "token_replaced"
    DB_ERROR = "db_error"

    def __init__(self, status: str, token_current: Optional[bool] = None):
        self.status = status
        self.token_current = token_current


def _authorize_pc(pc_id: str, auth_token: str) -> PcCheck:
    """Look the PC up, compare its stored token and, when the registration is
    accepted, stamp last_connected / last_seen. One session, one commit.

    A database error is fail-open (``DB_ERROR``): the token signature and type
    were already verified, and refusing would leave a station connected but
    unregistered until its next reconnect.
    """
    db = None
    try:
        db = next(get_db())
        pc = db.query(PC).filter(PC.id == pc_id).first()
        if pc is None:
            return PcCheck(PcCheck.UNKNOWN_PC)
        current = _token_is_current(pc, auth_token, db)
        if not current and settings.RELAY_REQUIRE_CURRENT_TOKEN:
            return PcCheck(PcCheck.TOKEN_REPLACED, token_current=False)
        now_ts = int(time.time())  # Unix timestamp
        pc.last_connected = now_ts
        # Seed last_seen immediately so a freshly-connected PC isn't
        # up to one sweep-interval stale.
        pc.last_seen = now_ts
        db.commit()
        return PcCheck(PcCheck.OK, token_current=current)
    except Exception as db_error:
        logger.error(f"PC lookup failed for {pc_id}: {type(db_error).__name__}")
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        return PcCheck(PcCheck.DB_ERROR)
    finally:
        if db is not None:
            db.close()


def _refuse_registration(sid: str, message: str, log_reason: str) -> None:
    relay_counters["rejected_registrations"] += 1
    logger.warning(f"Registration refused for {sid}: {log_reason}")
    sio.emit("error", {"message": message}, room=sid)


# --------------------------------------------------------------------------- #
# Socket.IO events
# --------------------------------------------------------------------------- #


@sio.event
def connect(sid, environ, auth=None):
    """
    Handle new client connection.

    A session whose auth payload claims the sender role must present
    RELAY_SENDER_SECRET (when one is configured); otherwise the connection is
    refused. Every other session starts as a client.

    Args:
        sid: Session ID
        environ: WSGI environment dict
        auth: Socket.IO CONNECT auth payload (None for PCs)
    """
    role = ROLE_CLIENT
    if relay_auth.claims_sender(auth):
        if not legacy_open() and not relay_auth.secret_matches(
            auth.get(relay_auth.AUTH_SECRET_KEY), settings.RELAY_SENDER_SECRET
        ):
            logger.warning(f"Sender session refused: {sid} (invalid sender secret)")
            raise socketio.exceptions.ConnectionRefusedError("sender authentication failed")
        role = ROLE_SENDER

    logger.info(f"Client connected: {sid} ({role})")

    # Initialize session data
    session_data[sid] = _new_session(role)
    if role == ROLE_SENDER:
        sio.enter_room(sid, SENDERS_ROOM)


@sio.event
def disconnect(sid):
    """
    Handle client disconnection.

    Args:
        sid: Session ID
    """
    logger.info(f"Client disconnected: {sid}")

    # Get PC ID before removing
    pc_id = None
    if sid in session_data:
        pc_id = session_data[sid].get("pc_id")
        del session_data[sid]

    # Remove from connected PCs
    if pc_id and pc_id in connected_pcs:
        if connected_pcs[pc_id] == sid:
            del connected_pcs[pc_id]
            logger.info(f"PC {pc_id} removed from connected list")

            # Broadcast updated client list
            broadcast_client_list()


@sio.event
def register(sid, data):
    """
    Register PC client with authentication.

    Expected data format (RegisterV1; Client 1.1.x sends pc_id and auth_token):
        {
            'pc_id': str,
            'auth_token': str,
            'name': str (optional)
        }

    Args:
        sid: Session ID
        data: Registration data dict
    """
    try:
        session = session_data.get(sid)
        if session is None:
            logger.warning(f"register from unknown session {sid}")
            return
        if session.get("role") == ROLE_SENDER:
            _refuse_registration(sid, MSG_SENDER_REGISTER, "sender session")
            return

        if not isinstance(data, dict):
            _refuse_registration(sid, MSG_MISSING, "payload is not an object")
            return
        pc_id = data.get("pc_id")
        auth_token = data.get("auth_token")
        name = data.get("name", "")
        if not isinstance(name, str):
            name = ""
        name = name[:MAX_NAME_LENGTH]

        if not pc_id or not auth_token or not isinstance(pc_id, str) or not isinstance(auth_token, str):
            _refuse_registration(sid, MSG_MISSING, "missing pc_id or auth_token")
            return
        if len(auth_token) > MAX_TOKEN_LENGTH:
            _refuse_registration(sid, MSG_INVALID_TOKEN, "token too long")
            return

        # Validate JWT token
        try:
            payload = validate_token(auth_token)
        except jwt.InvalidTokenError as e:
            _refuse_registration(sid, MSG_INVALID_TOKEN, f"token validation failed ({type(e).__name__})")
            return

        # NEW-42: only PC tokens register (a locally minted or user token does not).
        if payload.get("type") != PC_TOKEN_TYPE:
            _refuse_registration(sid, MSG_NOT_PC_TOKEN, "token type is not pc_auth")
            return

        token_pc_id = payload.get("pc_id")
        if not isinstance(token_pc_id, str) or not token_pc_id:
            _refuse_registration(sid, MSG_INVALID_TOKEN, "token carries no pc_id")
            return

        # Verify PC ID matches token
        if token_pc_id != pc_id:
            logger.warning(f"PC ID mismatch: provided={pc_id}, token={token_pc_id}")
        # Use token's PC ID as authoritative
        pc_id = token_pc_id

        check = _authorize_pc(pc_id, auth_token)
        if check.status == PcCheck.UNKNOWN_PC:
            _refuse_registration(sid, MSG_UNKNOWN_PC, f"PC {pc_id} not found in database")
            return
        if check.status == PcCheck.TOKEN_REPLACED:
            _refuse_registration(sid, MSG_TOKEN_REPLACED, f"PC {pc_id} token differs from the stored token")
            return

        flags: Dict[str, bool] = {}
        if check.status == PcCheck.DB_ERROR:
            flags[FLAG_DB_UNVERIFIED] = True
            logger.warning(f"PC {pc_id} registered without a database check (db_unverified)")
        elif check.token_current is False:
            flags[FLAG_TOKEN_OUTDATED] = True
            logger.warning(f"PC {pc_id} registered with an outdated token (token_outdated, OD-23)")

        # NEW-43: a second live registration of this pc_id takes over; the old
        # session stays connected but is no longer this PC.
        previous_sid = connected_pcs.get(pc_id)
        if previous_sid is not None and previous_sid != sid and previous_sid in session_data:
            old = session_data[previous_sid]
            old["pc_id"] = None
            old["superseded_by"] = sid
            flags[FLAG_DUPLICATE] = True
            relay_counters["duplicate_registrations"] += 1
            logger.warning(f"Duplicate registration for PC {pc_id}: session {sid} replaces {previous_sid}")

        # A session that re-registers as another PC gives up the old one.
        old_pc = session.get("pc_id")
        if old_pc and old_pc != pc_id and connected_pcs.get(old_pc) == sid:
            del connected_pcs[old_pc]

        # Register PC
        connected_pcs[pc_id] = sid
        session["pc_id"] = pc_id
        session["name"] = name
        session["ever_registered"] = True
        session["superseded_by"] = None
        session["flags"] = flags

        logger.info(f"PC registered: {pc_id} (sid: {sid}, name: {name})")

        # Confirm registration to client
        sio.emit("registered", registered_payload(pc_id), room=sid)

        # Broadcast updated client list
        broadcast_client_list()

    except Exception as e:
        logger.error(f"Error during registration for {sid}: {type(e).__name__}: {e}")
        sio.emit("error", {"message": "Registration failed"}, room=sid)


def forward_to_pc(target_sid: str, message_type: str, content: Any, origin: str = "portal",
                  callback: Optional[Callable] = None) -> None:
    """Emit one routed message to a PC session. Client 1.1.x reads
    ``content`` from the ``config`` event and ACKs it (Socket.IO callback)."""
    sio.emit(
        message_type,
        {"type": message_type, "content": content, "from": origin},
        room=target_sid,
        callback=callback,
    )


def _warn_legacy_open_once() -> None:
    global _legacy_open_warned
    if not _legacy_open_warned:
        _legacy_open_warned = True
        logger.warning("Relay is legacy-open (RELAY_SENDER_SECRET unset): accepting an unauthenticated sender")


def _reject_message(sid: str, message: str, log_reason: str) -> None:
    relay_counters["rejected_messages"] += 1
    logger.warning(f"Rejected 'message' from {sid}: {log_reason}")
    sio.emit("error", {"message": message}, room=sid)


@sio.event
def message(sid, data):
    """
    Handle message routing to target PC (sender sessions only).

    Expected data format:
        {
            'type': 'config',
            'targetId': str (PC ID),
            'content': dict (message content)
        }

    The sender gets ``message_sent`` once the message is handed to the PC's
    session, and ``message_ack`` when the PC acknowledges it.

    Args:
        sid: Session ID
        data: Message data dict
    """
    try:
        if not _is_sender(sid):
            _reject_message(sid, MSG_NOT_AUTHORIZED, "not a sender session")
            return
        if session_data.get(sid, {}).get("role") != ROLE_SENDER:
            _warn_legacy_open_once()

        if not isinstance(data, dict):
            _reject_message(sid, "Invalid message", "payload is not an object")
            return
        message_type = data.get("type")
        target_id = data.get("targetId")
        content = data.get("content")

        if message_type not in SENDER_MESSAGE_TYPES:
            _reject_message(sid, "Message type not allowed", "type not in the allow-list")
            return

        if not target_id or not isinstance(target_id, str):
            logger.error(f"Message from {sid} missing targetId")
            sio.emit("error", {"message": "Missing targetId"}, room=sid)
            return

        if not isinstance(content, dict):
            _reject_message(sid, "Invalid message content", "content is not an object")
            return

        # Check if target PC is connected
        target_sid = connected_pcs.get(target_id)
        if target_sid is None:
            logger.warning(f"Target PC {target_id} is not connected")
            sio.emit(
                "error", {"message": f"Target PC {target_id} is not online"}, room=sid
            )
            return

        logger.info(
            f"Routing {message_type} message from {sid} to PC {target_id} (sid: {target_sid})"
        )

        def _on_pc_ack(*_ack):
            logger.info(f"PC {target_id} acknowledged {message_type}")
            if sid in session_data:
                sio.emit("message_ack", {"targetId": target_id, "status": "received"}, room=sid)

        # Forward message to target PC
        forward_to_pc(target_sid, message_type, content, callback=_on_pc_ack)

        # Send acknowledgment to sender
        sio.emit(
            "message_sent", {"targetId": target_id, "status": "delivered"}, room=sid
        )

    except Exception as e:
        logger.error(f"Error routing message from {sid}: {type(e).__name__}: {e}")
        sio.emit("error", {"message": "Message routing failed"}, room=sid)


@sio.event
def get_clients(sid, data=None):
    """
    Request list of connected clients (sender sessions only).

    Args:
        sid: Session ID
    """
    try:
        if not _is_sender(sid):
            logger.warning(f"Rejected get_clients from {sid}: not a sender session")
            sio.emit("error", {"message": MSG_NOT_AUTHORIZED}, room=sid)
            return

        sio.emit("clients_list", {"clients": _client_list()}, room=sid)

    except Exception as e:
        logger.error(f"Error getting client list for {sid}: {e}")
        sio.emit("error", {"message": "Failed to get client list"}, room=sid)


def broadcast_client_list():
    """
    Send the updated client list to sender sessions (never to PCs, NEW-57).
    """
    try:
        clients = _client_list()

        sio.emit("clients_update", {"clients": clients}, room=SENDERS_ROOM)

        logger.debug(f"Broadcasted client list: {len(clients)} clients")

    except Exception as e:
        logger.error(f"Error broadcasting client list: {e}")


def _last_seen_sweep(interval_seconds: int):
    """
    Rolling 'last seen' presence sweep.

    Every ``interval_seconds`` this stamps ``pcs.last_seen = now()`` for every PC
    currently in ``connected_pcs``. That set is authoritative: Engine.IO's
    ping/pong evicts a PC (fires ``disconnect``) within the pong timeout when it
    stops responding, so anything still listed is currently alive. One batched
    UPDATE per tick regardless of PC count.

    Runs forever as a background task (an eventlet greenlet in production); a
    DB error in one tick is logged and swallowed so the sweep (and the server)
    never dies.
    """
    logger.info(f"Starting last_seen presence sweep every {interval_seconds}s")
    while True:
        sio.sleep(interval_seconds)
        pc_ids = list(connected_pcs.keys())
        if not pc_ids:
            continue
        db = None
        try:
            db = next(get_db())
            now_ts = int(time.time())
            db.query(PC).filter(PC.id.in_(pc_ids)).update(
                {PC.last_seen: now_ts}, synchronize_session=False
            )
            db.commit()
            logger.debug(f"last_seen sweep updated {len(pc_ids)} PC(s)")
        except Exception as sweep_error:
            logger.error(f"last_seen sweep failed: {sweep_error}")
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
        finally:
            if db is not None:
                db.close()


def run_server(host: str = "0.0.0.0", port: int = 8080):
    """
    Run the WebSocket server.

    Args:
        host: Host to bind to
        port: Port to listen on
    """
    if ASYNC_MODE != "eventlet":
        raise RuntimeError(f"run_server needs eventlet ({ASYNC_MODE_ENV}={ASYNC_MODE})")
    import eventlet
    import eventlet.wsgi

    logger.info(f"Starting WebSocket server on {host}:{port}")

    # Shared-env self-check (D28): flags, secret fingerprints, key status, G-1,
    # and a broker PING. Never fatal: the relay starts with the broker down.
    from app.core.startup_check import run_startup_check

    run_startup_check("websocket", check_broker_reachable=True)
    if legacy_open():
        logger.warning("Relay is legacy-open: RELAY_SENDER_SECRET is unset, so any session may push layouts")
    logger.info(
        f"Relay trust: sender_auth={'legacy-open' if legacy_open() else 'required'} "
        f"require_current_token={'true' if settings.RELAY_REQUIRE_CURRENT_TOKEN else 'false'} "
        f"packet_logging={'on' if settings.RELAY_DEBUG_LOGGING else 'off'} "
        f"features={','.join(advertised_features()) or '-'}"
    )

    sweep_interval = int(os.environ.get("WS_LAST_SEEN_SWEEP_SECONDS", "30"))
    sio.start_background_task(_last_seen_sweep, sweep_interval)

    try:
        eventlet.wsgi.server(eventlet.listen((host, port)), app, log_output=True)
    except Exception as e:
        logger.error(f"WebSocket server error: {e}")
        raise


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Run server
    run_server(host=settings.WEBSOCKET_HOST, port=settings.WEBSOCKET_PORT)
