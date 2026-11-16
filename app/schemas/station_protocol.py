"""
Station protocol contract, version 1 (plan 06, task D1).

One module describes every message that crosses a station boundary:

* Client -> relay (Socket.IO): ``register`` (RegisterV1), ``deploy_result``
  (DeployResultV1) and ``station_status`` (StationStatusV1).
* Relay -> Client (Socket.IO): ``registered`` (RegisteredV1) and the ACK that a
  report handler returns (ReportAckV1).
* Core -> Client (local report websockets, see StreamSwitcher
  docs/LOGS_AND_HEARTBEAT.md): hello, heartbeat, config-deploy ack, stream events
  and log lines (Core*V1).

Rules shared by every model:

* Every model allows extra fields, so a newer sender never breaks an older
  receiver. Known fields are validated strictly (no type coercion).
* Station models (Client <-> backend) never carry credentials: any string, in a
  known or an extra field, at any depth, that holds a URI with userinfo
  (``scheme://user[:pass]@``) is rejected, and so is a dict key that does.
  Core models apply the same rule to their ``uri`` fields, which Core documents
  as credential-free. Core ``log`` text is not constrained: receivers redact it.
* Validation errors never echo input values (``hide_input_in_errors``); use
  :func:`safe_errors` when logging them.

The JSON Schemas exported by ``scripts/export_station_protocol.py`` are generated
from these models by :func:`json_schemas`. They express the same rules (strings
get ``not: {pattern: USERINFO_PATTERN}``, extra fields must be credential-free
JSON values), so pydantic and a JSON Schema validator agree on every fixture.
``PROTOCOL_HASH`` is the sha256 of :func:`bundle_bytes`: the canonical JSON of
``{schema_name: schema}`` (sorted keys, no whitespace, ASCII only). A receiver
that holds a vendored copy recomputes it from the schema files.
"""

import hashlib
import json
import re
from functools import lru_cache
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Optional, Tuple, Type

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Protocol constants
# --------------------------------------------------------------------------- #

PROTOCOL_NAME = "truelive-station"
PROTOCOL_VERSION = 1
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_PREFIX = "urn:truelive:station-protocol:1:"

# Socket.IO event names (Client <-> relay).
EVENT_REGISTER = "register"
EVENT_REGISTERED = "registered"
EVENT_DEPLOY_RESULT = "deploy_result"
EVENT_STATION_STATUS = "station_status"

# Feature names a relay advertises in ``registered.features`` (D12). A Client
# enables a report stream only when its feature is listed (D9).
FEATURE_DEPLOY_RESULT = "deploy_result.v1"
FEATURE_STATION_STATUS = "station_status.v1"
FEATURE_STATION_CONFIG = "station_config.v1"
FEATURE_PC_TOKEN = "pc_token.v1"
FEATURE_STATION_REPORTS_DISABLED = "station_reports.disabled"
KNOWN_FEATURES: Tuple[str, ...] = (
    FEATURE_DEPLOY_RESULT,
    FEATURE_STATION_STATUS,
    FEATURE_STATION_CONFIG,
    FEATURE_PC_TOKEN,
    FEATURE_STATION_REPORTS_DISABLED,
)

# Core report events (docs/LOGS_AND_HEARTBEAT.md in StreamSwitcher).
CORE_EVENT_HELLO = "hello"
CORE_EVENT_HEARTBEAT = "heartbeat"
CORE_EVENT_ACK = "config-deploy"
CORE_EVENT_LOG = "log"
CORE_STREAM_EVENTS: Tuple[str, ...] = (
    "stream_started",
    "stream_failed",
    "stream_recovered",
    "stream_stopped",
)

# Bounds the relay enforces before validation (D15); documented here so both
# sides share one number.
MAX_REPORT_BYTES = 64 * 1024
MAX_FAILED_SOURCE_IDS = 50
MAX_CAPABILITIES = 16
MAX_CAPABILITY_LENGTH = 32
MAX_CONFIG_ID_LENGTH = 64
MAX_DETAIL_LENGTH = 500

DEPLOY_STATUSES: Tuple[str, ...] = (
    "delivered",
    "accepted",
    "applied",
    "failed",
    "unconfirmed",
    "superseded",
)
HEALTH_STATES: Tuple[str, ...] = ("ok", "degraded", "unhealthy", "stopped", "unknown")
BUILD_CLASSES: Tuple[str, ...] = ("legacy", "reports_v1", "reports_v2", "unknown")
DELIVERY_POLICIES: Tuple[str, ...] = ("live_with_fallback", "file_restart")
TAP_STATES: Tuple[str, ...] = ("ok", "lossy", "dead")
REPORT_ACK_STATUSES: Tuple[str, ...] = ("ok", "duplicate", "rejected", "disabled")

# E20: version strings and capability names.
VERSION_PATTERN = r"^[0-9A-Za-z.+_-]{1,64}$"
CAPABILITY_PATTERN = r"^[A-Za-z0-9_.:+-]+$"

# A URI with userinfo: a scheme, "://", then an "@" before the next whitespace.
# Deliberately broad: a password may contain "/", "?" or "#" (X-19), so the
# authority is not assumed to end at the first "/". A URI with a literal "@"
# in its path is rejected too; that false positive is accepted.
USERINFO_PATTERN = r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]*@"
_USERINFO_RE = re.compile(USERINFO_PATTERN)

# Top-level keys a report must never carry (D9: no screen_details, no token, no
# layout body in any payload).
REPORT_FORBIDDEN_KEYS: Tuple[str, ...] = (
    "auth_token",
    "token",
    "password",
    "config",
    "screen_details",
)


def contains_userinfo(text: str) -> bool:
    """True when ``text`` holds a URI with userinfo (credentials)."""
    return bool(_USERINFO_RE.search(text))


_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _path_part(key: Any) -> str:
    if isinstance(key, int):
        return f"[{key}]"
    if isinstance(key, str) and _SAFE_KEY_RE.match(key):
        return f".{key}"
    return ".<key>"


def find_userinfo(value: Any) -> Optional[str]:
    """Return the path of the first string (or dict key) holding a URI with
    userinfo, or None. The path never contains the offending value.

    Iterative, so a deeply nested payload cannot exhaust the stack.
    """
    stack: List[Tuple[str, Any]] = [("$", value)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, str):
            if _USERINFO_RE.search(node):
                return path
        elif isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and _USERINFO_RE.search(key):
                    return path + ".<key>"
                stack.append((path + _path_part(key), child))
        elif isinstance(node, (list, tuple)):
            for index, child in enumerate(node):
                stack.append((path + _path_part(index), child))
    return None


def _reject_userinfo(value: str) -> str:
    if _USERINFO_RE.search(value):
        raise ValueError("URI with userinfo (credentials) is not allowed")
    return value


def safe_errors(exc: ValidationError) -> List[Dict[str, Any]]:
    """The errors of ``exc`` without their input values (safe to log)."""
    return [
        {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
        for err in exc.errors()
    ]


# --------------------------------------------------------------------------- #
# Field types
# --------------------------------------------------------------------------- #

PrintableId = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_CONFIG_ID_LENGTH, pattern=r"^[\x21-\x7e]+$")
]
ConfigIdOrEmpty = Annotated[
    str, StringConstraints(max_length=MAX_CONFIG_ID_LENGTH, pattern=r"^[\x21-\x7e]*$")
]
Token = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:+-]+$")]
ShortToken = Annotated[
    str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.:+-]+$")
]
VersionStr = Annotated[str, StringConstraints(pattern=VERSION_PATTERN)]
Capability = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_CAPABILITY_LENGTH, pattern=CAPABILITY_PATTERN),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
AuthToken = Annotated[
    str, StringConstraints(min_length=1, max_length=4096, pattern=r"^[A-Za-z0-9_.=+/-]+$")
]
SourceId = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
]
KeyName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
ChannelName = Annotated[
    str, StringConstraints(min_length=1, max_length=16, pattern=r"^[a-z][a-z0-9_]*$")
]
ReadinessCode = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Z0-9_]+$")
]
ExitCodeHex = Annotated[str, StringConstraints(pattern=r"^0x[0-9A-Fa-f]{1,8}$")]
CoreTimestamp = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")
]
Pid = Annotated[int, Field(ge=0, le=4294967295)]
Count = Annotated[int, Field(ge=0, le=1_000_000)]
# Unix seconds (may be fractional). The upper bound (2100-01-01) catches
# milliseconds sent by mistake.
EpochSeconds = Annotated[float, Field(ge=0, le=4102444800)]


def _text(max_length: int) -> Any:
    return Annotated[str, StringConstraints(max_length=max_length)]


CredentialFreeUri = Annotated[
    str,
    StringConstraints(max_length=2048),
    AfterValidator(_reject_userinfo),
    Field(json_schema_extra={"not": {"pattern": USERINFO_PATTERN}}),
]

BuildClass = Literal["legacy", "reports_v1", "reports_v2", "unknown"]


# --------------------------------------------------------------------------- #
# Base classes
# --------------------------------------------------------------------------- #


class _ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, hide_input_in_errors=True)


class StationModel(_ProtocolModel):
    """Base of every Client <-> backend message: no credentials anywhere."""

    FORBIDDEN_KEYS: ClassVar[Tuple[str, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def _no_credentials(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in cls.FORBIDDEN_KEYS:
                if key in data:
                    raise ValueError(f"key '{key}' is not allowed in {cls.__name__}")
            path = find_userinfo(data)
            if path is not None:
                raise ValueError(f"URI with userinfo (credentials) at {path}")
        return data


class CoreModel(_ProtocolModel):
    """Base of every Core report model. Extra fields are not constrained."""


# --------------------------------------------------------------------------- #
# Client <-> relay
# --------------------------------------------------------------------------- #


class RegisterV1(StationModel):
    """Client -> relay, Socket.IO event ``register``.

    Client 1.1.x sends only ``pc_id`` and ``auth_token``; that is a valid
    RegisterV1. The relay takes the authoritative pc_id from the verified token
    and the session, never from this payload (E20).
    """

    pc_id: PrintableId = Field(..., description="PC id the Client believes it is (the token decides)")
    auth_token: AuthToken = Field(..., description="PC JWT (pc_auth). Never logged or stored by receivers")
    name: Optional[_text(255)] = Field(None, description="Station display name (NEW-55)")
    client_version: Optional[VersionStr] = None
    client_build: Optional[VersionStr] = None
    core_version: Optional[VersionStr] = None
    core_installed_version: Optional[VersionStr] = None
    core_running_version: Optional[VersionStr] = None
    core_version_source: Optional[ShortToken] = Field(
        None, description="Where core_version came from: hello, stdout, manifest, versioninfo, markers, unknown"
    )
    core_exe_sha256: Optional[Sha256Hex] = None
    core_exe_size: Optional[Annotated[int, Field(ge=0, le=2**40)]] = None
    core_build_class: Optional[BuildClass] = None
    capabilities: List[Capability] = Field(default_factory=list, max_length=MAX_CAPABILITIES)
    delivery_policy: Optional[Literal["live_with_fallback", "file_restart"]] = None
    telemetry_mode: Optional[ShortToken] = Field(
        None, description="How the Client hears Core: stdout_only, listener, fanout, none"
    )
    running_config_id: Optional[ConfigIdOrEmpty] = Field(
        None, description="config_id the wall runs; empty for a legacy layout"
    )


class RegisteredV1(StationModel):
    """Relay -> Client, Socket.IO event ``registered``.

    A legacy relay sends only ``pc_id`` and ``message``: no features means
    "send no reports" (D9).
    """

    pc_id: PrintableId
    message: Optional[_text(256)] = None
    features: List[Token] = Field(default_factory=list, max_length=32)
    protocol_hash: Optional[Sha256Hex] = Field(
        None, description="PROTOCOL_HASH of the relay's contract; a mismatch is flagged, not fatal"
    )


class ReportAckV1(StationModel):
    """Relay -> Client: the ACK a ``deploy_result`` or ``station_status``
    handler returns. ``duplicate`` means the message_id was already accepted;
    ``disabled`` means RELAY_STATION_REPORTS is off (D15)."""

    status: Literal["ok", "duplicate", "rejected", "disabled"]
    message_id: Optional[PrintableId] = None
    reason: Optional[_text(200)] = None


class DeployResultV1(StationModel):
    """Client -> relay, Socket.IO event ``deploy_result`` (D7, D9, D15).

    The relay takes pc_id from the session. ``ts`` is the Client's time of the
    transition, in Unix seconds.
    """

    FORBIDDEN_KEYS: ClassVar[Tuple[str, ...]] = REPORT_FORBIDDEN_KEYS

    message_id: PrintableId = Field(..., description="Unique per report; the relay deduplicates on it")
    config_id: PrintableId = Field(..., description="The deploy's config_id (64 characters max)")
    status: Literal["delivered", "accepted", "applied", "failed", "unconfirmed", "superseded"]
    stage: Optional[ShortToken] = Field(None, description="Where the deploy ended or failed")
    source: Optional[ShortToken] = Field(None, description="What confirmed it: stdout, channel, timeout, ...")
    mode: Optional[ShortToken] = Field(None, description="Delivery mode: live, fallback_file, file_restart, ...")
    detail: Optional[_text(MAX_DETAIL_LENGTH)] = None
    core_version: Optional[VersionStr] = None
    core_pid: Optional[Pid] = None
    persisted: Optional[bool] = None
    restored_config_id: Optional[ConfigIdOrEmpty] = None
    ts: EpochSeconds


class CoreStatusV1(StationModel):
    """``StationStatusV1.core``: the supervised Core process."""

    state: Optional[Token] = Field(None, description="CoreState phase (B7b), e.g. RUNNING")
    pid: Optional[Pid] = None
    build_class: Optional[BuildClass] = None
    exe_sha256: Optional[Sha256Hex] = None
    uptime_sec: Optional[Count] = None
    restarts_24h: Optional[Count] = None
    last_exit: Optional[ExitCodeHex] = Field(None, description="Last exit code as hex, e.g. 0x544C0002")


class TelemetryChannelV1(StationModel):
    """One Core report channel as the Client sees it."""

    target: Optional[ShortToken] = Field(None, description="listener, pulse, none, unknown")
    connected: Optional[bool] = None
    last_message_age_sec: Optional[Count] = None


class TelemetryV1(StationModel):
    """``StationStatusV1.telemetry``."""

    mode: Optional[ShortToken] = None
    tap_state: Optional[Literal["ok", "lossy", "dead"]] = None
    channels: Dict[ChannelName, TelemetryChannelV1] = Field(default_factory=dict, max_length=8)


class WallV1(StationModel):
    """``StationStatusV1.wall``: what the wall shows. ``streams_failed`` is
    capped at 50 source ids; ``streams_failed_count`` carries the true count."""

    running_config_id: Optional[ConfigIdOrEmpty] = None
    deploying: Optional[bool] = None
    screens: Optional[Annotated[int, Field(ge=0, le=64)]] = None
    streams_total: Optional[Count] = None
    streams_playing: Optional[Count] = None
    streams_connecting: Optional[Count] = None
    streams_failed: List[SourceId] = Field(default_factory=list, max_length=MAX_FAILED_SOURCE_IDS)
    streams_failed_count: Optional[Count] = None


class ReadinessIssueV1(StationModel):
    """One station readiness issue (B14), e.g. AUTOLOGON_OFF."""

    code: ReadinessCode
    detail: Optional[_text(200)] = None


class ConfigStatusV1(StationModel):
    """``StationStatusV1.config_status``: C23's managed-config status block.
    Key names only; never values."""

    core_config_mode: Optional[ShortToken] = None
    mode_set_by: Optional[ShortToken] = None
    core_revision: Optional[ConfigIdOrEmpty] = None
    core_version: Optional[VersionStr] = None
    core_caps: List[Capability] = Field(default_factory=list, max_length=32)
    migration_status: Optional[ShortToken] = None
    drift_key_names: List[KeyName] = Field(default_factory=list, max_length=32)
    legacy_available: Optional[bool] = None
    last_config_error: Optional[_text(200)] = None
    data_dir_acl_warning: Optional[bool] = None
    fleet_errors_count: Optional[Count] = None


class StationStatusV1(StationModel):
    """Client -> relay, Socket.IO event ``station_status`` (D9, D15).

    Sent on register, every 60 s and on change. The relay takes pc_id from the
    session and stores only allow-listed fields (C23).
    """

    FORBIDDEN_KEYS: ClassVar[Tuple[str, ...]] = REPORT_FORBIDDEN_KEYS

    message_id: PrintableId
    ts: EpochSeconds
    health: Literal["ok", "degraded", "unhealthy", "stopped", "unknown"]
    reasons: List[Token] = Field(default_factory=list, max_length=16)
    client_version: Optional[VersionStr] = None
    client_build: Optional[VersionStr] = None
    core_installed_version: Optional[VersionStr] = None
    core_running_version: Optional[VersionStr] = None
    core_version_source: Optional[ShortToken] = None
    core: Optional[CoreStatusV1] = None
    telemetry: Optional[TelemetryV1] = None
    wall: Optional[WallV1] = None
    flags: Dict[Token, bool] = Field(default_factory=dict, max_length=32)
    readiness: List[ReadinessIssueV1] = Field(default_factory=list, max_length=32)
    config_status: Optional[ConfigStatusV1] = None


# --------------------------------------------------------------------------- #
# Core -> Client (local report channels)
# --------------------------------------------------------------------------- #


class _CoreMessageV1(CoreModel):
    """Fields Core puts on every message (Message.cpp ``msg::Base``)."""

    event: str
    timestamp: CoreTimestamp
    message_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    app_id: _text(1024)
    station_id: _text(1024)
    station_name: _text(1024)
    pc_id: _text(1024)
    pc_name: _text(1024)
    version: VersionStr
    pid: Optional[Pid] = Field(None, description="Core's own pid (D21); absent on 1.1.0 builds")


class CoreHelloV1(_CoreMessageV1):
    """First message on every logs/heartbeat connection."""

    event: Literal["hello"]
    channel: Literal["logs", "heartbeat"]
    uptime_sec: Count = Field(..., description="Seconds since the process started")
    host_server_port: _text(16)
    is_master_app: bool
    config_id: _text(MAX_CONFIG_ID_LENGTH)
    capabilities: Optional[List[Capability]] = Field(None, max_length=32)


class CoreSourceDetailV1(CoreModel):
    source_id: _text(256)
    site_id: _text(256)
    site_name: _text(1024)
    camera_id: _text(256)
    camera_name: _text(1024)
    uri: CredentialFreeUri
    status: Literal["playing", "connecting", "failed"]
    status_code: int
    reconnect_attempts: int
    pos_x: int
    pos_y: int


class CoreViewDetailV1(CoreModel):
    view_n: int
    view_id: _text(256)
    view_name: _text(1024)
    n_rows: int
    n_cols: int
    sources: List[CoreSourceDetailV1]


class CoreScreenDetailV1(CoreModel):
    idx: int
    id: _text(512)
    title: _text(1024)
    display_idx: int
    current_view: int
    views: List[CoreViewDetailV1]


class CoreHeartbeatV1(_CoreMessageV1):
    """Heartbeat. While ``deploying`` is true, the counts and screen_details
    are absent."""

    event: Literal["heartbeat"]
    type: Literal["heartbeat"]
    status: int
    uptime_sec: Count
    config_id: _text(MAX_CONFIG_ID_LENGTH)
    is_master_app: bool
    deploying: bool
    screens: Optional[Count] = None
    streams_total: Optional[Count] = None
    streams_playing: Optional[Count] = None
    streams_connecting: Optional[Count] = None
    streams_failed: Optional[List[_text(256)]] = None
    screen_details: Optional[List[CoreScreenDetailV1]] = None


class CoreAckV1(_CoreMessageV1):
    """``config-deploy`` ack: OK once a layout parses; Failed (optionally with
    a reason, D21) when it does not parse or fails to start."""

    event: Literal["config-deploy"]
    config_id: _text(MAX_CONFIG_ID_LENGTH)
    status: Literal["OK", "Failed"]
    reason: Optional[_text(200)] = None


class CoreStreamEventV1(_CoreMessageV1):
    """stream_started / stream_failed / stream_recovered / stream_stopped."""

    event: Literal["stream_started", "stream_failed", "stream_recovered", "stream_stopped"]
    log_level: Annotated[int, Field(ge=0, le=4)]
    group: _text(64)
    message: _text(65536)
    source_id: _text(256)
    site_id: _text(256)
    site_name: _text(1024)
    camera_id: _text(256)
    camera_name: _text(1024)
    uri: CredentialFreeUri
    reason: _text(1024)
    screen: int
    view_n: int
    view_name: _text(1024)
    cell: int
    reconnect_attempts: int
    outage_since_epoch: Optional[int] = None


class CoreLogV1(_CoreMessageV1):
    """One Core log line. ``message`` may contain anything Core logs; receivers
    redact it before storing or forwarding (D4)."""

    event: Literal["log"]
    log_level: Annotated[int, Field(ge=0, le=4)]
    group: _text(64)
    message: _text(65536)


# --------------------------------------------------------------------------- #
# Registry, JSON Schema export, protocol hash
# --------------------------------------------------------------------------- #

#: Station models, in export order. Their schemas forbid userinfo everywhere.
STATION_MODELS: Tuple[Type[StationModel], ...] = (
    RegisterV1,
    RegisteredV1,
    ReportAckV1,
    DeployResultV1,
    StationStatusV1,
)
#: Core models, in export order.
CORE_MODELS: Tuple[Type[CoreModel], ...] = (
    CoreHelloV1,
    CoreHeartbeatV1,
    CoreAckV1,
    CoreStreamEventV1,
    CoreLogV1,
)
MODELS: Dict[str, Type[BaseModel]] = {m.__name__: m for m in STATION_MODELS + CORE_MODELS}

_DESCRIPTIONS: Dict[str, str] = {
    "RegisterV1": "Client -> relay, Socket.IO event 'register'.",
    "RegisteredV1": "Relay -> Client, Socket.IO event 'registered'.",
    "ReportAckV1": "Relay -> Client, ACK of a deploy_result or station_status call.",
    "DeployResultV1": "Client -> relay, Socket.IO event 'deploy_result'.",
    "StationStatusV1": "Client -> relay, Socket.IO event 'station_status'.",
    "CoreHelloV1": "Core -> Client, report channels logs/heartbeat: hello.",
    "CoreHeartbeatV1": "Core -> Client, report channel heartbeat: heartbeat.",
    "CoreAckV1": "Core -> Client, report channel ack: config-deploy.",
    "CoreStreamEventV1": "Core -> Client, report channel logs: stream events.",
    "CoreLogV1": "Core -> Client, report channel logs: log line.",
}

_NO_USERINFO = {"not": {"pattern": USERINFO_PATTERN}}
_SAFE_DEFS = {
    "SafeString": {
        "description": "A string that holds no URI with userinfo (credentials).",
        "type": "string",
        "not": {"pattern": USERINFO_PATTERN},
    },
    "SafeValue": {
        "description": "Any JSON value whose strings and keys hold no URI with userinfo.",
        "anyOf": [
            {"$ref": "#/$defs/SafeString"},
            {"type": ["number", "boolean", "null"]},
            {"type": "array", "items": {"$ref": "#/$defs/SafeValue"}},
            {
                "type": "object",
                "propertyNames": {"not": {"pattern": USERINFO_PATTERN}},
                "additionalProperties": {"$ref": "#/$defs/SafeValue"},
            },
        ],
    },
}


def _harden(node: Any, station: bool) -> Any:
    """Make a pydantic-generated schema express this module's rules.

    * Dict fields with key patterns reject non-matching keys
      (pydantic emits patternProperties only).
    * Station schemas: every string gets ``not: {pattern: USERINFO}``, every
      object rejects userinfo keys, and extra fields must be SafeValue.
    """
    if isinstance(node, list):
        return [_harden(item, station) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: _harden(value, station) for key, value in node.items()}
    if "patternProperties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    if station:
        if out.get("type") == "string" and "enum" not in out and "const" not in out:
            out.setdefault("not", {"pattern": USERINFO_PATTERN})
        if out.get("type") == "object":
            out.setdefault("propertyNames", dict(_NO_USERINFO))
            if out.get("additionalProperties") is True:
                out["additionalProperties"] = {"$ref": "#/$defs/SafeValue"}
    return out


def _forbidden_keys_clause(keys: Tuple[str, ...]) -> Dict[str, Any]:
    return {"not": {"anyOf": [{"required": [key]} for key in keys]}}


def json_schema(model: Type[BaseModel]) -> Dict[str, Any]:
    """The exported JSON Schema (draft 2020-12) of one protocol model."""
    station = issubclass(model, StationModel)
    raw = model.model_json_schema(mode="validation")
    schema = _harden(raw, station)
    if station:
        defs = dict(schema.get("$defs", {}))
        defs.update(json.loads(json.dumps(_SAFE_DEFS)))
        schema["$defs"] = defs
        forbidden = getattr(model, "FORBIDDEN_KEYS", ())
        if forbidden:
            schema["not"] = _forbidden_keys_clause(tuple(forbidden))["not"]
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = SCHEMA_ID_PREFIX + model.__name__
    schema["description"] = _DESCRIPTIONS[model.__name__]
    schema["x-truelive-protocol"] = f"{PROTOCOL_NAME}/{PROTOCOL_VERSION}"
    return schema


@lru_cache(maxsize=1)
def _schemas_json() -> str:
    return json.dumps(
        {name: json_schema(model) for name, model in MODELS.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def json_schemas() -> Dict[str, Dict[str, Any]]:
    """``{schema_name: schema}`` for every protocol model (a fresh copy)."""
    return json.loads(_schemas_json())


def canonical_json(obj: Any) -> bytes:
    """The canonical encoding the protocol hash is computed over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def bundle_bytes() -> bytes:
    """Canonical JSON of ``{schema_name: schema}``; PROTOCOL_HASH is its sha256."""
    return canonical_json(json_schemas())


@lru_cache(maxsize=1)
def protocol_hash() -> str:
    """sha256 hex of :func:`bundle_bytes`. The relay sends it in ``registered``."""
    return hashlib.sha256(bundle_bytes()).hexdigest()


def validate(name: str, payload: Any) -> BaseModel:
    """Validate ``payload`` against the model called ``name``.

    Raises KeyError for an unknown name and pydantic.ValidationError for an
    invalid payload (log it with :func:`safe_errors`).
    """
    return MODELS[name].model_validate(payload)
