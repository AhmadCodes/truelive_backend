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

Deploy path (plan 06 D14):

* ``deploy_config`` is a sender event the API calls with an ACK
  (``{config_id, targetId, content}``). The relay answers
  ``{status: delivered|offline|error, config_id, confirmable, reason?}`` and,
  when delivered, forwards ``content`` to the PC as the ``config`` event with
  a callback. The PC's ACK (Client 1.1.x ACKs every ``config``) moves the
  ``pc_deployments`` row to ``received``, and for a non-confirmable row
  (Client 1.1.x, which cannot report results) stamps ``pcs.last_applied``.
* ``confirmable``: the PC registered with the capability ``deploy_result.v1``,
  ``registered`` advertised that feature to it, and the payload carries the
  deploy's config_id at its root. Only then can the station report applied.
* Database writes from ACK callbacks run off the hub (eventlet.tpool) in a
  background task. An update that finds no row yet, or hits a database
  error, is kept in ``pending_deploy_events`` and retried every few seconds
  until it applies or expires (deploy_tracking.PendingTransitions).

Station reports (plan 06 D15; C23, E20, NEW-44, NEW-52):

* ``deploy_result`` and ``station_status`` are served only to the PC's
  current registered session (``requires_registered_pc``), and only while
  RELAY_STATION_REPORTS is on (else every call answers ``disabled``). Each
  report is bounded (64 KB), validated against D1 (no credentials anywhere),
  deduplicated by (pc_id, message_id) over the last 10 000, and answered with
  a ReportAckV1 (ok, duplicate, rejected, disabled).
* deploy_result moves the deploy's row (deploy_tracking: monotonic, late
  evidence upgrades unconfirmed, applied may become failed); a row of another
  PC is rejected. ``applied`` stamps pcs.last_applied. An id with no row
  updates only the PC's running_config_id (and is kept for retry unless the
  Client minted it).
* station_status keeps only the allow-listed pc_health columns
  (station_reports.health_columns) in ``health_buffer``; register adds the
  E20 version fields (each checked, invalid ones dropped). One background
  flush every RELAY_HEALTH_FLUSH_SECONDS (15) writes the buffer with one
  batched upsert, off the hub. A relay restart loses at most that much health.
* A timeout sweep every 30 s (deploy_tracking.sweep_timeouts): pending > 60 s
  becomes relay_failed; confirmable rows delivered or received for more than
  DEPLOY_ACK_TIMEOUT_SEC, or accepted for twice that, become unconfirmed;
  Client 1.1.x rows stay received.
* Every relay database call runs off the hub (``_run_blocking``), and the
  presence writes (register stamp, last_seen sweep) leave pcs.updated_at
  alone (NEW-52).

PC token renewal and the rotation window (plan 06 D30, relay part; OD-23,
NEW-77; app/services/pc_tokens.py):

* A token verifies with JWT_SECRET, and while JWT_ROTATION_GRACE_UNTIL is in
  the future also with JWT_SECRET_PREVIOUS (``token_previous_secret`` flag).
  After the window, previous-secret tokens are refused like a bad signature.
* The replaced token of an open renewal counts as current (OD-23 (b) accepts
  either until the PC confirms); registering with the stored (renewed) token
  confirms the renewal.
* After ``registered``, a PC whose token was signed with the previous secret
  or expires within PC_TOKEN_RENEW_BEFORE_DAYS (``token_expiring``) is sent
  ``pc_token_renewed {token, exp}`` on its own sid, with an ACK, when it
  declared the capability ``pc_token.v1`` and ``registered`` advertised it.
  The mint and the store run off the hub. ``{"status": "stored"}`` confirms
  the renewal and clears the session's token flags.
* A PC that needs a new token but cannot take one (no ``pc_token.v1``, or an
  outdated token, which is never renewed) is flagged ``token_drop_needed``
  (pc_health.relay_flags; ``python -m app.services.pc_tokens report`` lists
  them for B22's token drop or a technician).

The relay never rotates JWT_SECRET itself (OD-23): ops rotates it through the
window above (DOCKER_SETUP.md runbook).
"""
import functools
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import jwt
import socketio
from pydantic import ValidationError
from sqlalchemy import update

from app.core.config import settings
from app.database import get_db
from app.models.pc import PC
from app.schemas import station_protocol
from app.services import deploy_tracking, pc_tokens, relay_auth, station_reports

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
# D30: the token was signed with JWT_SECRET_PREVIOUS / expires within
# PC_TOKEN_RENEW_BEFORE_DAYS / needs a new token the relay cannot give it.
FLAG_TOKEN_PREVIOUS_SECRET = "token_previous_secret"
FLAG_TOKEN_EXPIRING = "token_expiring"
FLAG_TOKEN_DROP_NEEDED = "token_drop_needed"
# Flags a confirmed renewal clears (the PC now holds the stored token).
TOKEN_FLAGS = (FLAG_TOKEN_OUTDATED, FLAG_TOKEN_PREVIOUS_SECRET, FLAG_TOKEN_EXPIRING, FLAG_TOKEN_DROP_NEEDED)

# Messages shown to a PC whose registration is refused (Client 1.1.x shows
# them in a tray notification).
MSG_MISSING = "Missing pc_id or auth_token"
MSG_INVALID_TOKEN = "Invalid or expired authentication token"
MSG_NOT_PC_TOKEN = "Authentication token is not a PC token"
MSG_UNKNOWN_PC = "This PC is not registered in the portal"
MSG_TOKEN_REPLACED = "Authentication token has been replaced; enter the current PC token"
MSG_SENDER_REGISTER = "Sender sessions cannot register as a PC"
MSG_NOT_AUTHORIZED = "Not authorized"

# deploy_config (D14): the sender event and its answers.
DEPLOY_EVENT = "deploy_config"
DEPLOY_DELIVERED = "delivered"
DEPLOY_OFFLINE = "offline"
DEPLOY_ERROR = "error"
MAX_TARGET_ID_LENGTH = 64
_CONFIG_ID_RE = re.compile(r"^[\x21-\x7e]{1,%d}$" % station_protocol.MAX_CONFIG_ID_LENGTH)
_CAPABILITY_RE = re.compile(station_protocol.CAPABILITY_PATTERN)
# How often deploy events kept for a missing row (or a database error) are retried.
DEPLOY_EVENT_RETRY_SECONDS = 5
# Station reports (D15). The intervals can be shortened for tests by env.
HEALTH_FLUSH_SECONDS = float(os.environ.get("RELAY_HEALTH_FLUSH_SECONDS", "15"))
DEPLOY_SWEEP_SECONDS = float(os.environ.get("RELAY_DEPLOY_SWEEP_SECONDS", "30"))
REPORT_DEPLOY_RESULT = station_protocol.EVENT_DEPLOY_RESULT
REPORT_STATION_STATUS = station_protocol.EVENT_STATION_STATUS
_SAFE_LOC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


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
    "deploys_forwarded": 0,
    "deploys_offline": 0,
    "deploys_rejected": 0,
    "deploy_receipts": 0,
    "reports_ok": 0,
    "reports_duplicate": 0,
    "reports_rejected": 0,
    "reports_disabled": 0,
    "health_batches": 0,
    "health_rows": 0,
    "token_renewals_issued": 0,
    "token_renewals_resent": 0,
    "token_renewals_confirmed": 0,
    "token_renewals_refused": 0,
    "token_drop_needed": 0,
}

# Deploy events whose row did not exist yet, or whose write failed (D14).
pending_deploy_events = deploy_tracking.PendingTransitions()

# Latest pending pc_health columns per PC, flushed in batches (D15).
health_buffer = station_reports.HealthBuffer()
# (pc_id, message_id) of the last reports accepted (D15 deduplication).
report_ids = station_reports.MessageIdLRU()

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


def registered_payload(pc_id: str, features: Optional[List[str]] = None) -> Dict[str, Any]:
    """The ``registered`` event (RegisteredV1). Client 1.1.x reads only pc_id
    and message; the rest is additive."""
    return {
        "pc_id": pc_id,
        "message": "Successfully registered",
        "features": advertised_features() if features is None else list(features),
        "protocol_hash": station_protocol.protocol_hash(),
    }


# --------------------------------------------------------------------------- #
# Session helpers
# --------------------------------------------------------------------------- #


def _new_session(role: str) -> Dict[str, Any]:
    return {
        "pc_id": None,
        "name": None,
        "connected_at": datetime.now(timezone.utc).isoformat(),  # with its offset (NEW-51, D16)
        "role": role,
        "ever_registered": False,
        "flags": {},
        "superseded_by": None,
        # RegisterV1.capabilities as sent, and the features ``registered``
        # advertised to this session (D14: confirmable deploys).
        "capabilities": [],
        "features": [],
    }


def parse_capabilities(data: Any) -> List[str]:
    """RegisterV1.capabilities: at most 16 names of at most 32 characters
    (D1/E20). A malformed list counts as none, so such a station is never
    treated as able to confirm deploys."""
    return station_reports.parse_capabilities(data) or []


def session_confirmable(sid: str) -> bool:
    """The PC on ``sid`` can report deploy results (D14, D15): it declared
    ``deploy_result.v1`` and ``registered`` advertised it to that session."""
    session = session_data.get(sid) or {}
    feature = station_protocol.FEATURE_DEPLOY_RESULT
    return feature in (session.get("capabilities") or ()) and feature in (session.get("features") or ())


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
    return verify_token(auth_token)[0]


def verify_token(auth_token: str):
    """``(payload, secret)`` of a PC token: JWT_SECRET, or JWT_SECRET_PREVIOUS
    while the rotation window is open (D30; ``secret`` is
    pc_tokens.SECRET_CURRENT or SECRET_PREVIOUS). Raises jwt.InvalidTokenError;
    the log names the error class only."""
    try:
        return pc_tokens.verify_pc_token(auth_token, settings)

    except jwt.ExpiredSignatureError:
        logger.error("Token has expired")
        raise

    except jwt.InvalidTokenError as e:
        logger.error(f"Invalid token: {type(e).__name__}")
        raise


def _token_is_current(pc: PC, auth_token: str, db) -> bool:
    """OD-23: the presented token equals the token stored for the PC, or (D30)
    it is the replaced token of the PC's open renewal: either counts until the
    PC confirms the renewed token."""
    return pc_tokens.check_presented_token(db, pc.id, pc.auth_token, auth_token).current


class PcCheck:
    """Outcome of the database half of ``register``."""

    OK = "ok"
    UNKNOWN_PC = "unknown_pc"
    TOKEN_REPLACED = "token_replaced"
    DB_ERROR = "db_error"

    def __init__(self, status: str, token_current: Optional[bool] = None, via_renewal: Optional[int] = None,
                 renewals_confirmed: Optional[List[int]] = None):
        self.status = status
        self.token_current = token_current
        # D30: the presented token is the replaced token of this open renewal.
        self.via_renewal = via_renewal
        # D30: renewals the presented (stored, renewed) token confirmed.
        self.renewals_confirmed = list(renewals_confirmed or [])


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
        pc = db.query(PC.id, PC.auth_token).filter(PC.id == pc_id).first()
        if pc is None:
            return PcCheck(PcCheck.UNKNOWN_PC)
        presented = pc_tokens.check_presented_token(db, pc.id, pc.auth_token, auth_token)
        current = presented.current
        if not current and settings.RELAY_REQUIRE_CURRENT_TOKEN:
            db.rollback()
            return PcCheck(PcCheck.TOKEN_REPLACED, token_current=False)
        now_ts = int(time.time())  # Unix timestamp
        # Seed last_seen immediately so a freshly-connected PC isn't up to one
        # sweep-interval stale. A presence stamp, not an edit: updated_at is
        # kept (NEW-52).
        db.execute(_presence_update([pc_id], last_connected=now_ts, last_seen=now_ts))
        db.commit()
        return PcCheck(PcCheck.OK, token_current=current, via_renewal=presented.via_renewal,
                       renewals_confirmed=presented.confirmed)
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


def _presence_update(pc_ids: List[str], **values):
    """UPDATE pcs with presence columns only. ``updated_at`` is set to itself,
    so its onupdate default does not fire (NEW-52: presence is not an edit)."""
    table = PC.__table__
    return update(table).where(table.c.id.in_(pc_ids)).values(updated_at=table.c.updated_at, **values)


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

        # Validate JWT token (D30: the previous secret too, while the window is open)
        try:
            payload, secret_used = verify_token(auth_token)
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

        check = _run_blocking(_authorize_pc, pc_id, auth_token)
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
        if check.renewals_confirmed:
            relay_counters["token_renewals_confirmed"] += len(check.renewals_confirmed)
            logger.info(f"PC {pc_id} registered with its renewed token: renewal(s) "
                        f"{', '.join(str(i) for i in check.renewals_confirmed)} confirmed")

        # D30: a token signed with the previous secret, or close to expiry.
        renew_reason = pc_tokens.renewal_reason(payload, secret_used, settings)
        if secret_used == pc_tokens.SECRET_PREVIOUS:
            flags[FLAG_TOKEN_PREVIOUS_SECRET] = True
        if renew_reason == pc_tokens.REASON_EXPIRING:
            flags[FLAG_TOKEN_EXPIRING] = True

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

        features = advertised_features()
        capabilities = parse_capabilities(data)
        renew = _renewal_decision(pc_id, check, flags, renew_reason, capabilities, features)

        # Register PC
        connected_pcs[pc_id] = sid
        session["pc_id"] = pc_id
        session["name"] = name
        session["ever_registered"] = True
        session["superseded_by"] = None
        session["flags"] = flags
        session["capabilities"] = capabilities
        session["features"] = features
        session["status_seen"] = False  # D17: catch-up is decided on the first station_status
        session["token_renewal_id"] = None  # D30: the renewal sent on this session

        # E20: the register's version fields, each checked; stored with the
        # relay flags at the next health flush. pc_id is the session's.
        columns, dropped = station_reports.register_columns(data, int(time.time()), flags)
        session["client_version"] = columns.get("client_version")
        if dropped:
            logger.warning(f"PC {pc_id} register: ignored invalid field(s) {', '.join(sorted(dropped))}")
        health_buffer.put(pc_id, columns)

        logger.info(f"PC registered: {pc_id} (sid: {sid}, name: {name})")

        # Confirm registration to client
        sio.emit("registered", registered_payload(pc_id, features), room=sid)

        # Broadcast updated client list
        broadcast_client_list()

        # D30: mint (or resend) the PC's new token off the hub, after registered.
        if renew:
            _spawn(_renew_pc_token, sid, pc_id, auth_token, renew_reason)

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


# --------------------------------------------------------------------------- #
# Deploy path (D14)
# --------------------------------------------------------------------------- #


def _run_blocking(fn: Callable, *args) -> Any:
    """Run a blocking call (psycopg2) without stalling the hub: under eventlet
    on a real OS thread (eventlet.tpool), else inline."""
    if ASYNC_MODE == "eventlet":
        from eventlet import tpool

        return tpool.execute(fn, *args)
    return fn(*args)


def _spawn(fn: Callable, *args) -> None:
    """Run ``fn`` as a background task (greenlet / thread), so an ACK callback
    returns at once. Tests replace it with an inline call."""
    sio.start_background_task(fn, *args)


def _open_db():
    return next(get_db())


def _apply_and_commit(apply: Callable) -> deploy_tracking.TransitionResult:
    db = _open_db()
    try:
        result = apply(db)
        db.commit()
        return result
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def _record_receipt(config_id: str, pc_id: str, confirmable: bool, forwarded_at: int, received_at: int) -> None:
    """The PC ACKed deploy ``config_id``: mark it received (and, for a
    non-confirmable row, stamp pcs.last_applied). Kept for retry when the row
    does not exist yet or the database fails."""

    def apply(db):
        return deploy_tracking.record_receipt(db, config_id, pc_id, confirmable=confirmable, ts=received_at,
                                              forwarded_at=forwarded_at)

    try:
        result = _run_blocking(_apply_and_commit, apply)
    except Exception as exc:
        logger.error(f"Could not record receipt of deploy {config_id} by PC {pc_id}: {type(exc).__name__}; "
                     f"kept for retry")
        pending_deploy_events.add(config_id, apply, label="received")
        return
    if result.outcome == "missing":
        logger.warning(f"Receipt of deploy {config_id} by PC {pc_id} arrived before its row; kept for retry")
        pending_deploy_events.add(config_id, apply, label="received")
    elif result.outcome == "mismatch":
        logger.warning(f"Receipt of deploy {config_id} from PC {pc_id} does not match the row's PC; ignored")
    else:
        relay_counters["deploy_receipts"] += 1
        logger.info(f"Deploy {config_id} received by PC {pc_id} (status {result.status})")


def retry_pending_deploy_events() -> None:
    """Retry kept deploy events (one session for all of them)."""
    if not len(pending_deploy_events):
        return
    applied, kept = _run_blocking(pending_deploy_events.retry, _open_db)
    if applied or kept:
        logger.info(f"Deploy event retry: {applied} applied, {kept} still kept")


def _deploy_answer(status: str, config_id: Any = None, *, confirmable: bool = False,
                   reason: Optional[str] = None) -> Dict[str, Any]:
    answer: Dict[str, Any] = {"status": status, "config_id": config_id if isinstance(config_id, str) else None,
                              "confirmable": bool(confirmable)}
    if reason:
        answer["reason"] = reason
    return answer


@sio.on(DEPLOY_EVENT)
def deploy_config(sid, data=None):
    """
    Deliver one deploy to a PC (sender sessions only; the API calls it with
    an ACK, D14).

    Expected data: ``{config_id: str (<= 64 printable), targetId: str,
    content: dict}``. When ``content`` carries a root ``config_id``, it must
    equal ``config_id``.

    Returns (the caller's ACK) ``{status, config_id, confirmable, reason?}``:
    ``delivered`` when the layout was handed to the PC's current session,
    ``offline`` when the PC has none, ``error`` for a refused request. The
    layout itself is never logged.
    """
    try:
        if not _is_sender(sid):
            relay_counters["deploys_rejected"] += 1
            logger.warning(f"Rejected {DEPLOY_EVENT} from {sid}: not a sender session")
            return _deploy_answer(DEPLOY_ERROR, reason="not authorized")
        if session_data.get(sid, {}).get("role") != ROLE_SENDER:
            _warn_legacy_open_once()
        if not isinstance(data, dict):
            relay_counters["deploys_rejected"] += 1
            return _deploy_answer(DEPLOY_ERROR, reason="invalid request")
        config_id = data.get("config_id")
        target_id = data.get("targetId")
        content = data.get("content")
        if not isinstance(config_id, str) or not _CONFIG_ID_RE.match(config_id):
            relay_counters["deploys_rejected"] += 1
            return _deploy_answer(DEPLOY_ERROR, reason="invalid config_id")
        if not isinstance(target_id, str) or not target_id or len(target_id) > MAX_TARGET_ID_LENGTH:
            relay_counters["deploys_rejected"] += 1
            return _deploy_answer(DEPLOY_ERROR, config_id, reason="invalid targetId")
        if not isinstance(content, dict):
            relay_counters["deploys_rejected"] += 1
            return _deploy_answer(DEPLOY_ERROR, config_id, reason="invalid content")
        root_id = content.get("config_id")
        if root_id is not None and root_id != config_id:
            relay_counters["deploys_rejected"] += 1
            return _deploy_answer(DEPLOY_ERROR, config_id, reason="config_id mismatch")

        target_sid = connected_pcs.get(target_id)
        if target_sid is None or _registered_pc(target_sid) != target_id:
            relay_counters["deploys_offline"] += 1
            logger.info(f"Deploy {config_id}: PC {target_id} is not connected")
            return _deploy_answer(DEPLOY_OFFLINE, config_id)

        confirmable = session_confirmable(target_sid) and root_id == config_id
        forwarded_at = deploy_tracking.now_ts()

        def _on_pc_ack(*_ack):
            # Any ACK proves the Client's config handler ran (receipt); its
            # payload, if any, is not trusted for anything else.
            _spawn(_record_receipt, config_id, target_id, confirmable, forwarded_at, deploy_tracking.now_ts())

        forward_to_pc(target_sid, "config", content, callback=_on_pc_ack)
        relay_counters["deploys_forwarded"] += 1
        logger.info(f"Deploy {config_id} forwarded to PC {target_id} (sid: {target_sid}, "
                    f"confirmable={'yes' if confirmable else 'no'})")
        return _deploy_answer(DEPLOY_DELIVERED, config_id, confirmable=confirmable)
    except Exception as e:
        logger.error(f"Error in {DEPLOY_EVENT} from {sid}: {type(e).__name__}")
        return _deploy_answer(DEPLOY_ERROR, reason="relay error")


# --------------------------------------------------------------------------- #
# PC token renewal (D30)
# --------------------------------------------------------------------------- #


def _renewal_decision(pc_id: str, check: "PcCheck", flags: Dict[str, bool], reason: Optional[str],
                      capabilities: List[str], features: List[str]) -> bool:
    """True when this registration should get a new (or resent) token.

    Wanted when the token was signed with the previous secret, expires soon,
    or is the replaced token of an open renewal (the PC never got, or never
    stored, the new one). Deferred to the next registration for an
    unverified or duplicate session. Never for an outdated token or a Client
    without ``pc_token.v1``: those are flagged ``token_drop_needed``.
    """
    if reason is None and check.via_renewal is None:
        return False
    if check.status != PcCheck.OK:
        logger.info(f"PC {pc_id}: token renewal deferred to its next registration (no database check)")
        return False
    if flags.get(FLAG_DUPLICATE):
        logger.info(f"PC {pc_id}: token renewal deferred to its next registration (duplicate registration)")
        return False
    why = None
    if flags.get(FLAG_TOKEN_OUTDATED):
        why = "its token is outdated, and an outdated token is never renewed"
    elif not pc_tokens.can_take_renewal(capabilities, features):
        why = f"the Client does not support {pc_tokens.FEATURE}"
    if why is not None:
        flags[FLAG_TOKEN_DROP_NEEDED] = True
        relay_counters["token_drop_needed"] += 1
        logger.warning(f"PC {pc_id} needs a new token ({reason or 'renewal pending'}) but cannot get one from the "
                       f"relay: {why} (token_drop_needed; use a token drop or the portal)")
        return False
    return True


def _in_transaction(work: Callable) -> Any:
    db = _open_db()
    try:
        result = work(db)
        db.commit()
        return result
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def _renew_pc_token(sid: str, pc_id: str, presented_token: str, reason: Optional[str]) -> None:
    """Background task: mint and store (or look up) the PC's new token off the
    hub, then send it to the PC's current session only, with an ACK."""
    try:
        renewal = _run_blocking(_in_transaction,
                                lambda db: pc_tokens.issue_or_resume(db, pc_id, presented_token, reason))
    except Exception as exc:
        logger.error(f"PC {pc_id}: token renewal failed ({type(exc).__name__}); retried at its next registration")
        return
    if renewal is None:
        logger.info(f"PC {pc_id}: no token renewal issued (its token is no longer the stored one)")
        return
    relay_counters["token_renewals_issued" if renewal.minted else "token_renewals_resent"] += 1
    if _registered_pc(sid) != pc_id:
        logger.info(f"PC {pc_id}: token renewal {renewal.renewal_id} kept for its next registration "
                    f"(session {sid} is no longer the PC's)")
        return
    session_data[sid]["token_renewal_id"] = renewal.renewal_id
    renewal_id = renewal.renewal_id

    def _on_ack(*ack):
        _spawn(_renewal_acked, sid, pc_id, renewal_id, ack[0] if ack else None)

    sio.emit(pc_tokens.EVENT_RENEWED, {"token": renewal.token, "exp": renewal.exp}, to=sid, callback=_on_ack)
    expires = datetime.fromtimestamp(renewal.exp, tz=timezone.utc).date().isoformat()
    logger.info(f"PC {pc_id}: token renewal {renewal_id} sent ({renewal.reason}, "
                f"{'new token' if renewal.minted else 'same token again'}, expires {expires})")


def _clear_token_flags(sid: str, pc_id: str) -> None:
    """A confirmed renewal: the PC holds the stored token now."""
    if _registered_pc(sid) != pc_id:
        return
    flags = session_data[sid].setdefault("flags", {})
    removed = [flags.pop(name) for name in TOKEN_FLAGS if name in flags]
    if not removed:
        return
    health_buffer.put(pc_id, {"relay_flags": dict(flags)})
    broadcast_client_list()


def _renewal_acked(sid: str, pc_id: str, renewal_id: int, ack: Any) -> None:
    """Background task: the PC answered ``pc_token_renewed``."""
    status, reason = pc_tokens.parse_renewal_ack(ack)
    if status == pc_tokens.ACK_STORED:
        try:
            confirmed = _run_blocking(_in_transaction,
                                      lambda db: pc_tokens.confirm_renewal(db, pc_id, renewal_id, via="ack"))
        except Exception as exc:
            logger.error(f"PC {pc_id}: could not confirm token renewal {renewal_id} ({type(exc).__name__}); "
                         f"it is confirmed when the PC registers with the new token")
            return
        if confirmed:
            relay_counters["token_renewals_confirmed"] += 1
            logger.info(f"PC {pc_id} confirmed token renewal {renewal_id}")
        else:
            logger.info(f"PC {pc_id} confirmed token renewal {renewal_id}, which is already closed or cancelled")
        _clear_token_flags(sid, pc_id)
    elif status == pc_tokens.ACK_REFUSED:
        relay_counters["token_renewals_refused"] += 1
        logger.warning(f"PC {pc_id} refused token renewal {renewal_id} ({reason}); its current token stays valid")
        try:
            _run_blocking(_in_transaction, lambda db: pc_tokens.record_refusal(db, pc_id, renewal_id, reason))
        except Exception as exc:
            logger.error(f"PC {pc_id}: could not audit the refused renewal {renewal_id} ({type(exc).__name__})")
    else:
        logger.warning(f"PC {pc_id} answered token renewal {renewal_id} without a status; not confirmed")


# pc_token.v1 is a push (no event handler): advertise it once the push exists.
enable_feature(station_protocol.FEATURE_PC_TOKEN)


# --------------------------------------------------------------------------- #
# Station reports (D15)
# --------------------------------------------------------------------------- #


def _ack(status: str, message_id: Any = None, reason: Optional[str] = None) -> Dict[str, Any]:
    """A ReportAckV1."""
    ack: Dict[str, Any] = {"status": status}
    if isinstance(message_id, str) and _CONFIG_ID_RE.match(message_id):
        ack["message_id"] = message_id
    if reason:
        ack["reason"] = reason
    relay_counters[f"reports_{status}"] = relay_counters.get(f"reports_{status}", 0) + 1
    return ack


def reports_gate(handler: Callable) -> Callable:
    """RELAY_STATION_REPORTS=off: every report handler answers ``disabled``
    (checked first, before the session check)."""

    @functools.wraps(handler)
    def wrapper(sid, data=None):
        if not settings.RELAY_STATION_REPORTS:
            return _ack("disabled")
        return handler(sid, data)

    return wrapper


def _validated_report(name: str, pc_id: str, data: Any):
    """``(model, None)`` or ``(None, rejected_ack)``: size bound, D1
    validation (no credentials, capped lists), never logging values."""
    message_id = data.get("message_id") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return None, _ack("rejected", reason="report is not an object")
    if station_reports.report_size(data) > station_protocol.MAX_REPORT_BYTES:
        logger.warning(f"Rejected {name} from PC {pc_id}: larger than {station_protocol.MAX_REPORT_BYTES} bytes")
        return None, _ack("rejected", message_id, "report too large")
    try:
        return station_protocol.validate(name, data), None
    except ValidationError as exc:
        # Field paths only; a part that is not a plain name (a dict key the
        # station chose) is shown as <key>, so no payload text reaches the log.
        fields = sorted({".".join(str(p) if isinstance(p, int) or _SAFE_LOC_RE.match(str(p)) else "<key>"
                                  for p in e["loc"]) or "$"
                         for e in station_protocol.safe_errors(exc)})
        logger.warning(f"Rejected {name} from PC {pc_id}: invalid ({', '.join(fields)[:300]})")
        return None, _ack("rejected", message_id, "invalid report")


def _apply_deploy_result(pc_id: str, model, received_at: int, client_version: Optional[str]):
    """The database half of deploy_result: one conditional transition and,
    when the row becomes applied, the pcs.last_applied stamp."""
    to, fields, detail = station_reports.deploy_result_transition(model, client_version)

    def apply(db):
        result = deploy_tracking.transition(db, model.config_id, to, ts=received_at, pc_id=pc_id, detail=detail,
                                            fields=fields)
        if result.changed and to == deploy_tracking.APPLIED:
            deploy_tracking.stamp_last_applied(db, pc_id, received_at)
        return result

    return to, apply


@sio.on(REPORT_DEPLOY_RESULT)
@reports_gate
@requires_registered_pc
def deploy_result(sid, pc_id, data):
    """
    DeployResultV1 from the PC's current session (D15). Answers a ReportAckV1:
    ``ok`` once the result is applied to its row (or kept for retry),
    ``duplicate`` for a message_id already accepted, ``rejected`` for an
    invalid or oversized report or a config_id of another PC.
    """
    try:
        model, rejected = _validated_report("DeployResultV1", pc_id, data)
        if rejected is not None:
            return rejected
        key = (pc_id, model.message_id)
        if key in report_ids:
            return _ack("duplicate", model.message_id)
        received_at = deploy_tracking.now_ts()
        session = session_data.get(sid) or {}
        to, apply = _apply_deploy_result(pc_id, model, received_at, session.get("client_version"))
        try:
            result = _run_blocking(_apply_and_commit, apply)
        except Exception as exc:
            logger.error(f"Could not record deploy_result {model.config_id} of PC {pc_id}: {type(exc).__name__}; "
                         f"kept for retry")
            pending_deploy_events.add(model.config_id, apply, label=f"deploy_result.{model.status}")
            report_ids.add(key)
            return _ack("ok", model.message_id)
        if result.outcome == "mismatch":
            logger.warning(f"Rejected deploy_result from PC {pc_id}: {model.config_id} is another PC's deploy")
            return _ack("rejected", model.message_id, "config_id belongs to another PC")
        if result.outcome == "missing":
            # An unknown id updates only running_config_id (and a backend id is
            # kept in case its row is still being committed).
            if not model.config_id.startswith(station_reports.CLIENT_MINTED_PREFIX):
                pending_deploy_events.add(model.config_id, apply, label=f"deploy_result.{model.status}")
            logger.info(f"deploy_result {model.status} from PC {pc_id} for unknown deploy {model.config_id}")
        else:
            logger.info(f"deploy_result {model.status} from PC {pc_id} for {model.config_id}: "
                        f"{result.outcome} (status {result.status})")
        if to == deploy_tracking.APPLIED and result.outcome in ("changed", "missing"):
            health_buffer.put(pc_id, {"running_config_id": model.config_id})
        report_ids.add(key)
        return _ack("ok", model.message_id)
    except Exception as exc:
        logger.error(f"Error in deploy_result from PC {pc_id}: {type(exc).__name__}")
        return _ack("rejected", reason="relay error")


@sio.on(REPORT_STATION_STATUS)
@reports_gate
@requires_registered_pc
def station_status(sid, pc_id, data):
    """
    StationStatusV1 from the PC's current session (D15). Only the allow-listed
    pc_health columns are kept (C23), with the relay's own flags for the
    session; they are written by the next health flush.
    """
    try:
        model, rejected = _validated_report("StationStatusV1", pc_id, data)
        if rejected is not None:
            return rejected
        key = (pc_id, model.message_id)
        if key in report_ids:
            return _ack("duplicate", model.message_id)
        session = session_data.get(sid) or {}
        columns = station_reports.health_columns(model, deploy_tracking.now_ts())
        columns["relay_flags"] = dict(session.get("flags") or {})
        health_buffer.put(pc_id, columns)
        session["status_seen"] = True
        report_ids.add(key)
        return _ack("ok", model.message_id)
    except Exception as exc:
        logger.error(f"Error in station_status from PC {pc_id}: {type(exc).__name__}")
        return _ack("rejected", reason="relay error")


# The handlers exist: advertise their features (D12 gates them on the handlers).
enable_feature(station_protocol.FEATURE_DEPLOY_RESULT)
enable_feature(station_protocol.FEATURE_STATION_STATUS)


def flush_health_once() -> int:
    """Write the buffered health rows (one batched upsert, off the hub).
    On a database error the batch goes back into the buffer (newer values
    win) for the next flush. Returns the number of rows written."""
    batch = health_buffer.take()
    if not batch:
        return 0

    def write():
        db = _open_db()
        try:
            written, dropped = station_reports.upsert_health(db, batch)
            db.commit()
            return written, dropped
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            db.close()

    try:
        written, dropped = _run_blocking(write)
    except Exception as exc:
        logger.error(f"Health flush failed ({len(batch)} row(s), kept for the next flush): {type(exc).__name__}")
        health_buffer.restore(batch)
        return 0
    relay_counters["health_batches"] += 1
    relay_counters["health_rows"] += written
    if dropped:
        logger.info(f"Health flush: dropped {dropped} row(s) of PCs that no longer exist")
    logger.debug(f"Health flush: {written} row(s)")
    return written


def sweep_deploys_once() -> Dict[str, int]:
    """One timeout sweep of pc_deployments (off the hub)."""

    def sweep(db):
        return deploy_tracking.sweep_timeouts(db, ack_timeout=settings.DEPLOY_ACK_TIMEOUT_SEC)

    counts = _run_blocking(_apply_and_commit, sweep)
    moved = {k: v for k, v in counts.items() if v}
    if moved:
        logger.info(f"Deploy timeout sweep: {moved}")
    return counts


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
        if pc_ids:
            _run_blocking(presence_sweep_once, pc_ids)


def presence_sweep_once(pc_ids: List[str]) -> None:
    """One presence tick: ``pcs.last_seen = now`` for ``pc_ids`` in one UPDATE
    that leaves ``updated_at`` alone (NEW-52). Errors are logged and swallowed."""
    db = None
    try:
        db = next(get_db())
        db.execute(_presence_update(pc_ids, last_seen=int(time.time())))
        db.commit()
        logger.debug(f"last_seen sweep updated {len(pc_ids)} PC(s)")
    except Exception as sweep_error:
        logger.error(f"last_seen sweep failed: {type(sweep_error).__name__}")
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db is not None:
            db.close()


def _deploy_events_loop(interval_seconds: float):
    """Background task: retry kept deploy events (D14). Never dies."""
    while True:
        sio.sleep(interval_seconds)
        try:
            retry_pending_deploy_events()
        except Exception as exc:
            logger.error(f"Deploy event retry loop error: {type(exc).__name__}")


def _health_flush_loop(interval_seconds: float):
    """Background task: one health batch per interval (D15). Never dies."""
    while True:
        sio.sleep(interval_seconds)
        try:
            flush_health_once()
        except Exception as exc:
            logger.error(f"Health flush loop error: {type(exc).__name__}")


def _deploy_sweep_loop(interval_seconds: float):
    """Background task: the deploy timeout sweep (D15). Never dies."""
    while True:
        sio.sleep(interval_seconds)
        try:
            sweep_deploys_once()
        except Exception as exc:
            logger.error(f"Deploy timeout sweep error: {type(exc).__name__}")


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
    window = pc_tokens.window_state(settings)
    until = pc_tokens.grace_until(settings)
    logger.info(
        f"Relay tokens: rotation_window={window}"
        + (f" until={datetime.fromtimestamp(until, tz=timezone.utc).isoformat()}" if until is not None else "")
        + f" renew_before_days={settings.PC_TOKEN_RENEW_BEFORE_DAYS}"
    )

    sweep_interval = int(os.environ.get("WS_LAST_SEEN_SWEEP_SECONDS", "30"))
    sio.start_background_task(_last_seen_sweep, sweep_interval)
    sio.start_background_task(_deploy_events_loop, DEPLOY_EVENT_RETRY_SECONDS)
    sio.start_background_task(_health_flush_loop, HEALTH_FLUSH_SECONDS)
    sio.start_background_task(_deploy_sweep_loop, DEPLOY_SWEEP_SECONDS)
    logger.info(
        f"Relay reports: station_reports={'on' if settings.RELAY_STATION_REPORTS else 'off'} "
        f"health_flush={HEALTH_FLUSH_SECONDS:g}s deploy_sweep={DEPLOY_SWEEP_SECONDS:g}s "
        f"ack_timeout={settings.DEPLOY_ACK_TIMEOUT_SEC}s"
    )

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
