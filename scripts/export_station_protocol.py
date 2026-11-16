#!/usr/bin/env python3
"""
Export the station protocol contract (plan 06, task D1).

Writes, deterministically, from app/schemas/station_protocol.py:

    <out>/README.md
    <out>/PROTOCOL_HASH            sha256 of the canonical schema bundle
    <out>/MANIFEST.sha256          sha256 of every file above and below
    <out>/schemas/<Name>.schema.json
    <out>/fixtures/valid/<Name>__<case>.json
    <out>/fixtures/invalid/<Name>__<case>.json

The default <out> is docs/station_protocol in this repo. The Client keeps a
byte-identical vendored copy in shomer_client/tests/contract (same layout);
``golden/`` in either tree holds payloads the Client produces and is not
written here (D9 records them, D29 syncs them back).

Usage:
    python scripts/export_station_protocol.py                 # write docs/station_protocol
    python scripts/export_station_protocol.py --check         # exit 1 if it differs from a fresh export
    python scripts/export_station_protocol.py --out DIR [--check]

Needs no settings or database: it imports only the protocol module.
Fixtures use TEST-NET addresses (RFC 5737) and contain no credentials.
"""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas import station_protocol as sp  # noqa: E402

DEFAULT_OUT = BACKEND_ROOT / "docs" / "station_protocol"
GENERATED_DIRS = ("schemas", "fixtures")
GENERATED_TOP_FILES = ("README.md", "PROTOCOL_HASH", "MANIFEST.sha256")

# --------------------------------------------------------------------------- #
# Fixtures. Synthetic ids only; hosts are TEST-NET (192.0.2.0/24,
# 198.51.100.0/24, 203.0.113.0/24); no credentials of any kind.
# --------------------------------------------------------------------------- #

PC_ID = "00000000-0000-4000-8000-0000000000d1"
CONFIG_ID = "3f2b8c1e-0d1a-4c55-9e00-000000000001"
CONFIG_ID_2 = "3f2b8c1e-0d1a-4c55-9e00-000000000002"
CLIENT_CONFIG_ID = "client-00000000-0000-4000-8000-000000000003"
PLACEHOLDER_TOKEN = "FIXTURE-NOT-A-TOKEN"
EXE_SHA256 = hashlib.sha256(b"truelive fixture core exe").hexdigest()
TS = 1794850802.25
CORE_TS = "2026-11-17T09:40:02Z"
URI = "rtsp://203.0.113.10:554/Streaming/channels/402"
URI_2 = "rtsp://198.51.100.20:554/cam/realmonitor?channel=1&subtype=1"
# A URI with a user name but no password. Enough to prove that userinfo is
# rejected without storing anything that looks like a credential pair.
URI_WITH_USER = "rtsp://fixture-user@192.0.2.10:554/Streaming/channels/101"

CORE_BASE = {
    "timestamp": CORE_TS,
    "message_id": "6a1c0f3e-5b7d-4e2a-9c1f-00000000c0de",
    "app_id": "fixture-app",
    "station_id": PC_ID,
    "station_name": "Fixture Station 1",
    "pc_id": PC_ID,
    "pc_name": "Fixture Station 1",
    "version": "1.1.0+d53952a",
}


def _failed_ids(count: int) -> List[str]:
    return [f"SITE_FIXTURE_{index:04d}" for index in range(count)]


def _core(event: str, **fields: Any) -> Dict[str, Any]:
    message = dict(CORE_BASE)
    message["event"] = event
    message.update(fields)
    return message


def _source(source_id: str, uri: str, status: str, code: int, x: int, y: int) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "site_id": "SITE_FIXTURE",
        "site_name": "Fixture Site",
        "camera_id": source_id.rsplit("_", 1)[-1],
        "camera_name": f"Fixture Camera {source_id.rsplit('_', 1)[-1]}",
        "uri": uri,
        "status": status,
        "status_code": code,
        "reconnect_attempts": 0 if status == "playing" else 2,
        "pos_x": x,
        "pos_y": y,
    }


def _stream_event(event: str, level: int, reason: str, **extra: Any) -> Dict[str, Any]:
    source_id = "SITE_FIXTURE_12146_253271"
    message = f"{event} {source_id} (Fixture Site / Fixture Camera 4) screen 0 view 0: {reason}"
    return _core(
        event,
        log_level=level,
        group="Stream",
        message=message,
        source_id=source_id,
        site_id="SITE_FIXTURE",
        site_name="Fixture Site",
        camera_id="253271",
        camera_name="Fixture Camera 4",
        uri=URI,
        reason=reason,
        screen=0,
        view_n=0,
        view_name="1",
        cell=7,
        reconnect_attempts=1,
        **extra,
    )


def valid_fixtures() -> Dict[str, Dict[str, Any]]:
    """``{"<Schema>__<case>": payload}`` that every validator must accept."""
    register_full = {
        "pc_id": PC_ID,
        "auth_token": PLACEHOLDER_TOKEN,
        "name": "Fixture Station 1",
        "client_version": "1.4.0",
        "client_build": "1.4.0+0000000",
        "core_version": "1.1.0+d53952a",
        "core_installed_version": "1.1.0+d53952a",
        "core_running_version": "1.1.0+d53952a",
        "core_version_source": "stdout",
        "core_exe_sha256": EXE_SHA256,
        "core_exe_size": 2_867_200,
        "core_build_class": "reports_v1",
        "capabilities": ["deploy_result.v1", "station_status.v1", "pc_token.v1"],
        "delivery_policy": "live_with_fallback",
        "telemetry_mode": "stdout_only",
        "running_config_id": CONFIG_ID,
    }
    status_full = {
        "message_id": "status-0001",
        "ts": TS,
        "health": "degraded",
        "reasons": ["degraded", "no_video"],
        "client_version": "1.4.0",
        "client_build": "1.4.0+0000000",
        "core_installed_version": "1.1.0+d53952a",
        "core_running_version": "1.1.0+d53952a",
        "core_version_source": "stdout",
        "core": {
            "state": "RUNNING",
            "pid": 4321,
            "build_class": "reports_v1",
            "exe_sha256": EXE_SHA256,
            "uptime_sec": 86_400,
            "restarts_24h": 1,
            "last_exit": "0xC0000005",
        },
        "telemetry": {
            "mode": "stdout_only",
            "tap_state": "ok",
            "channels": {
                "heartbeat": {"target": "pulse", "connected": False},
                "logs": {"target": "none"},
            },
        },
        "wall": {
            "running_config_id": CONFIG_ID,
            "deploying": False,
            "screens": 2,
            "streams_total": 32,
            "streams_playing": 29,
            "streams_connecting": 0,
            "streams_failed": _failed_ids(3),
            "streams_failed_count": 3,
        },
        "flags": {"port_conflict": False, "token_outdated": False, "protocol_mismatch": False},
        "readiness": [
            {"code": "AUTOLOGON_OFF"},
            {"code": "MONITOR_SLEEP", "detail": "Display turns off after 10 min"},
        ],
        "config_status": {
            "core_config_mode": "legacy",
            "mode_set_by": "default",
            "core_revision": "",
            "core_version": "1.1.0+d53952a",
            "core_caps": ["ack", "payload_pc_id", "safe_reporting_clients"],
            "migration_status": "not_started",
            "drift_key_names": [],
            "legacy_available": True,
            "last_config_error": "",
            "data_dir_acl_warning": False,
            "fleet_errors_count": 0,
        },
    }
    heartbeat_playing = _core(
        "heartbeat",
        type="heartbeat",
        status=1,
        uptime_sec=114,
        config_id=CONFIG_ID,
        is_master_app=True,
        deploying=False,
        screens=1,
        streams_total=2,
        streams_playing=1,
        streams_connecting=0,
        streams_failed=["SITE_FIXTURE_12146_253272"],
        screen_details=[
            {
                "idx": 0,
                "id": f"pc{PC_ID}_layoutfixture_screen0",
                "title": "Monitor 1",
                "display_idx": 0,
                "current_view": 0,
                "views": [
                    {
                        "view_n": 0,
                        "view_id": "view-fixture-0",
                        "view_name": "1",
                        "n_rows": 1,
                        "n_cols": 2,
                        "sources": [
                            _source("SITE_FIXTURE_12146_253271", URI, "playing", 2, 0, 0),
                            _source("SITE_FIXTURE_12146_253272", URI_2, "failed", 4, 1, 0),
                        ],
                    }
                ],
            }
        ],
    )
    return {
        "RegisterV1__client_1_1_x": {"pc_id": PC_ID, "auth_token": PLACEHOLDER_TOKEN},
        "RegisterV1__client_full": register_full,
        "RegisterV1__extra_fields": {
            "pc_id": PC_ID,
            "auth_token": PLACEHOLDER_TOKEN,
            "future_field": {"nested": ["a", 1, True, None]},
        },
        "RegisteredV1__legacy_relay": {"pc_id": PC_ID, "message": "Successfully registered"},
        "RegisteredV1__current_relay": {
            "pc_id": PC_ID,
            "message": "Successfully registered",
            "features": [
                sp.FEATURE_DEPLOY_RESULT,
                sp.FEATURE_STATION_STATUS,
                sp.FEATURE_STATION_CONFIG,
                sp.FEATURE_PC_TOKEN,
            ],
            "protocol_hash": sp.protocol_hash(),
        },
        "RegisteredV1__reports_disabled": {
            "pc_id": PC_ID,
            "message": "Successfully registered",
            "features": [sp.FEATURE_STATION_REPORTS_DISABLED],
            "protocol_hash": sp.protocol_hash(),
        },
        "ReportAckV1__ok": {"status": "ok", "message_id": "deploy-0001"},
        "ReportAckV1__duplicate": {"status": "duplicate", "message_id": "deploy-0001"},
        "ReportAckV1__disabled": {"status": "disabled"},
        "ReportAckV1__rejected": {
            "status": "rejected",
            "message_id": "status-0002",
            "reason": "session is not the registered sid for this pc",
        },
        "DeployResultV1__applied": {
            "message_id": "deploy-0001",
            "config_id": CONFIG_ID,
            "status": "applied",
            "stage": "completed",
            "source": "stdout",
            "mode": "live",
            "core_version": "1.1.0+d53952a",
            "core_pid": 4321,
            "persisted": True,
            "ts": TS,
        },
        "DeployResultV1__failed_with_reason": {
            "message_id": "deploy-0002",
            "config_id": CONFIG_ID_2,
            "status": "failed",
            "stage": "start",
            "source": "stdout",
            "mode": "live",
            "detail": "Core reported Failed after OK: layout failed to start",
            "core_version": "1.1.0+d53952a",
            "core_pid": 4321,
            "persisted": False,
            "restored_config_id": CONFIG_ID,
            "ts": TS + 12.5,
        },
        "DeployResultV1__unconfirmed_client_id": {
            "message_id": "deploy-0003",
            "config_id": CLIENT_CONFIG_ID,
            "status": "unconfirmed",
            "source": "timeout",
            "detail": "tap lossy",
            "ts": TS + 300,
        },
        "DeployResultV1__superseded_minimal": {
            "message_id": "deploy-0004",
            "config_id": CONFIG_ID,
            "status": "superseded",
            "ts": TS + 1,
        },
        "StationStatusV1__full": status_full,
        "StationStatusV1__minimal": {"message_id": "status-0003", "ts": TS, "health": "unknown"},
        "StationStatusV1__legacy_core_stdout_only": {
            "message_id": "status-0004",
            "ts": TS,
            "health": "ok",
            "core": {"state": "RUNNING", "pid": 4321, "build_class": "legacy"},
            "telemetry": {"mode": "stdout_only", "tap_state": "lossy"},
            "wall": {"running_config_id": "", "deploying": False},
        },
        "StationStatusV1__50_failed_ids": {
            "message_id": "status-0005",
            "ts": TS,
            "health": "unhealthy",
            "reasons": ["no_video"],
            "wall": {
                "running_config_id": CONFIG_ID,
                "deploying": False,
                "streams_total": 64,
                "streams_playing": 0,
                "streams_connecting": 0,
                "streams_failed": _failed_ids(sp.MAX_FAILED_SOURCE_IDS),
                "streams_failed_count": 64,
            },
        },
        "CoreHelloV1__logs_reports_v1": _core(
            "hello",
            channel="logs",
            uptime_sec=87,
            host_server_port="9022",
            is_master_app=True,
            config_id="",
        ),
        "CoreHelloV1__heartbeat_reports_v2": _core(
            "hello",
            channel="heartbeat",
            uptime_sec=3,
            host_server_port="9022",
            is_master_app=True,
            config_id=CONFIG_ID,
            pid=4321,
            version="1.2.0+0000000",
            capabilities=["report_fanout", "config_id_on_success", "ack_reason", "pid", "atomic_persist"],
        ),
        "CoreHeartbeatV1__playing": heartbeat_playing,
        "CoreHeartbeatV1__deploying": _core(
            "heartbeat",
            type="heartbeat",
            status=1,
            uptime_sec=120,
            config_id=CONFIG_ID,
            is_master_app=True,
            deploying=True,
        ),
        "CoreAckV1__ok": _core("config-deploy", config_id=CONFIG_ID, status="OK"),
        "CoreAckV1__failed_with_reason": _core(
            "config-deploy",
            config_id=CONFIG_ID_2,
            status="Failed",
            reason="screens: expected an array",
            pid=4321,
        ),
        "CoreAckV1__legacy_layout_empty_id": _core("config-deploy", config_id="", status="OK"),
        "CoreStreamEventV1__started": _stream_event("stream_started", 1, "First frame arrived"),
        "CoreStreamEventV1__failed": _stream_event(
            "stream_failed", 3, "No video for 30 s", outage_since_epoch=1794850802
        ),
        "CoreStreamEventV1__recovered": _stream_event(
            "stream_recovered", 1, "Video flowing again after 70 s", outage_since_epoch=1794850802
        ),
        "CoreStreamEventV1__stopped": _stream_event("stream_stopped", 1, "Removed by config push"),
        "CoreLogV1__info": _core("log", log_level=1, group="Logger", message="Logger created"),
    }


def _set(path: Tuple[Any, ...], value: Any):
    def mutate(payload: Dict[str, Any]) -> Dict[str, Any]:
        node = payload
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        return payload

    return mutate


def _delete(key: str):
    def mutate(payload: Dict[str, Any]) -> Dict[str, Any]:
        del payload[key]
        return payload

    return mutate


# (case name, valid base, single mutation, rule it proves)
INVALID_CASES: Tuple[Tuple[str, str, Any, str], ...] = (
    ("StationStatusV1__51_failed_ids", "StationStatusV1__50_failed_ids",
     _set(("wall", "streams_failed"), _failed_ids(sp.MAX_FAILED_SOURCE_IDS + 1)),
     "streams_failed holds at most 50 source ids"),
    ("StationStatusV1__userinfo_in_extra_field", "StationStatusV1__full",
     _set(("telemetry", "channels", "logs", "address"), URI_WITH_USER),
     "no URI with userinfo, even in an extra field"),
    ("StationStatusV1__userinfo_in_dict_key", "StationStatusV1__minimal",
     _set(("debug",), {URI_WITH_USER: True}),
     "no URI with userinfo in a dict key"),
    ("StationStatusV1__screen_details_forbidden", "StationStatusV1__minimal",
     _set(("screen_details",), []),
     "station_status never carries screen_details"),
    ("StationStatusV1__bad_health", "StationStatusV1__minimal",
     _set(("health",), "green"),
     "health is one of the defined states"),
    ("StationStatusV1__bad_flag_key", "StationStatusV1__minimal",
     _set(("flags",), {"not a token": True}),
     "flag names are tokens"),
    ("StationStatusV1__bad_exit_code", "StationStatusV1__legacy_core_stdout_only",
     _set(("core", "last_exit"), "-1073741819"),
     "last_exit is a hex string"),
    ("DeployResultV1__config_id_65_chars", "DeployResultV1__applied",
     _set(("config_id",), "c" * 65),
     "config_id is at most 64 characters"),
    ("DeployResultV1__userinfo_in_detail", "DeployResultV1__failed_with_reason",
     _set(("detail",), f"camera {URI_WITH_USER} did not answer"),
     "no URI with userinfo in free text"),
    ("DeployResultV1__unknown_status", "DeployResultV1__applied",
     _set(("status",), "received"),
     "status is one of delivered, accepted, applied, failed, unconfirmed, superseded"),
    ("DeployResultV1__ts_in_milliseconds", "DeployResultV1__applied",
     _set(("ts",), TS * 1000),
     "ts is Unix seconds"),
    ("DeployResultV1__detail_501_chars", "DeployResultV1__failed_with_reason",
     _set(("detail",), "d" * 501),
     "detail is at most 500 characters"),
    ("DeployResultV1__token_key_forbidden", "DeployResultV1__applied",
     _set(("auth_token",), PLACEHOLDER_TOKEN),
     "reports never carry a token"),
    ("DeployResultV1__string_pid", "DeployResultV1__applied",
     _set(("core_pid",), "4321"),
     "no type coercion"),
    ("RegisterV1__missing_auth_token", "RegisterV1__client_1_1_x",
     _delete("auth_token"),
     "auth_token is required"),
    ("RegisterV1__bad_version", "RegisterV1__client_full",
     _set(("client_version",), "1.4.0 beta"),
     "versions match ^[0-9A-Za-z.+_-]{1,64}$"),
    ("RegisterV1__17_capabilities", "RegisterV1__client_full",
     _set(("capabilities",), [f"cap_{index}" for index in range(sp.MAX_CAPABILITIES + 1)]),
     "at most 16 capabilities"),
    ("RegisterV1__capability_33_chars", "RegisterV1__client_full",
     _set(("capabilities",), ["c" * (sp.MAX_CAPABILITY_LENGTH + 1)]),
     "a capability is at most 32 characters"),
    ("RegisterV1__userinfo_in_name", "RegisterV1__client_full",
     _set(("name",), URI_WITH_USER),
     "no URI with userinfo"),
    ("RegisterV1__bad_build_class", "RegisterV1__client_full",
     _set(("core_build_class",), "reports_v9"),
     "build class is legacy, reports_v1, reports_v2 or unknown"),
    ("RegisteredV1__bad_protocol_hash", "RegisteredV1__current_relay",
     _set(("protocol_hash",), "not-a-hash"),
     "protocol_hash is sha256 hex"),
    ("ReportAckV1__bad_status", "ReportAckV1__ok",
     _set(("status",), "accepted"),
     "ack status is ok, duplicate, rejected or disabled"),
    ("CoreStreamEventV1__uri_with_userinfo", "CoreStreamEventV1__failed",
     _set(("uri",), URI_WITH_USER),
     "Core stream uris are credential-free"),
    ("CoreHeartbeatV1__source_uri_with_userinfo", "CoreHeartbeatV1__playing",
     _set(("screen_details", 0, "views", 0, "sources", 0, "uri"), URI_WITH_USER),
     "Core heartbeat uris are credential-free"),
    ("CoreHeartbeatV1__string_uptime", "CoreHeartbeatV1__deploying",
     _set(("uptime_sec",), "120"),
     "no type coercion"),
    ("CoreAckV1__lowercase_status", "CoreAckV1__ok",
     _set(("status",), "ok"),
     "ack status is OK or Failed"),
    ("CoreHelloV1__local_timestamp", "CoreHelloV1__logs_reports_v1",
     _set(("timestamp",), "20261117_144002"),
     "timestamps are UTC ISO-8601 with Z"),
    ("CoreHelloV1__missing_version", "CoreHelloV1__logs_reports_v1",
     _delete("version"),
     "every Core message carries version"),
)


def invalid_fixtures() -> Dict[str, Dict[str, Any]]:
    """``{"<Schema>__<case>": payload}`` that every validator must reject.
    Each one is a valid fixture with exactly one mutation."""
    valid = valid_fixtures()
    out: Dict[str, Dict[str, Any]] = {}
    for name, base, mutate, _rule in INVALID_CASES:
        out[name] = mutate(copy.deepcopy(valid[base]))
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def dump(obj: Any) -> bytes:
    """Pretty, stable JSON: sorted keys, 2-space indent, ASCII, LF, trailing newline."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _readme(hash_hex: str) -> bytes:
    lines = [
        "# Station protocol contract (v1)",
        "",
        "Generated by `truelive_backend/scripts/export_station_protocol.py` from",
        "`truelive_backend/app/schemas/station_protocol.py`. **Do not edit by hand**: change the",
        "models, re-export, and copy the tree to `shomer_client/tests/contract/` (same layout).",
        "",
        f"PROTOCOL_HASH: `{hash_hex}`",
        "",
        "## Files",
        "",
        "| Path | Content |",
        "|---|---|",
        "| `schemas/<Name>.schema.json` | JSON Schema (draft 2020-12) of one message |",
        "| `fixtures/valid/<Name>__<case>.json` | payloads every validator must accept |",
        "| `fixtures/invalid/<Name>__<case>.json` | payloads every validator must reject"
        " (one mutation of a valid one) |",
        "| `PROTOCOL_HASH` | sha256 of the schema bundle (below) |",
        "| `MANIFEST.sha256` | sha256 of every generated file, `<hex>  <path>` |",
        "| `golden/<Name>__<case>.json` | not generated: payloads the Client really sends (D9), synced back by D29 |",
        "",
        "## Protocol hash",
        "",
        "The bundle is `{<Name>: <schema>}` for every file in `schemas/` (Name = file name without",
        "`.schema.json`), encoded as canonical JSON: keys sorted, separators `,` and `:` with no",
        "whitespace, ASCII only (`json.dumps(obj, sort_keys=True, separators=(',', ':'),",
        "ensure_ascii=True)`). PROTOCOL_HASH is the lowercase sha256 hex of those bytes. The relay",
        "sends it in `registered.protocol_hash`; a Client whose vendored hash differs flags the",
        "mismatch and warns once (D9). Any schema change, including a description, changes it.",
        "",
        "## Rules",
        "",
        "- Every model allows extra fields. Known fields are strict: no type coercion.",
        "- Client <-> relay messages never carry credentials: no string or key anywhere matches",
        f"  `{sp.USERINFO_PATTERN}` (a URI with userinfo). Reports never carry the top-level keys",
        "  " + ", ".join(f"`{key}`" for key in sp.REPORT_FORBIDDEN_KEYS) + ".",
        "- Core messages: `uri` fields are credential-free; log text is not constrained (redact it).",
        f"- `wall.streams_failed` holds at most {sp.MAX_FAILED_SOURCE_IDS} source ids; `config_id` is at most "
        f"{sp.MAX_CONFIG_ID_LENGTH} characters; at most {sp.MAX_CAPABILITIES} capabilities of "
        f"{sp.MAX_CAPABILITY_LENGTH} characters.",
        f"- The relay refuses a report larger than {sp.MAX_REPORT_BYTES} bytes before validating it.",
        "",
        "## Messages",
        "",
        "| Schema | Direction |",
        "|---|---|",
    ]
    for name, model in sp.MODELS.items():
        lines.append(f"| `{name}` | {sp._DESCRIPTIONS[name]} |")
    lines += [
        "",
        "Socket.IO events: "
        + ", ".join(
            f"`{event}`"
            for event in (sp.EVENT_REGISTER, sp.EVENT_REGISTERED, sp.EVENT_DEPLOY_RESULT, sp.EVENT_STATION_STATUS)
        )
        + ".",
        "Relay features (`registered.features`): " + ", ".join(f"`{f}`" for f in sp.KNOWN_FEATURES) + ".",
        "",
        "## Invalid fixtures",
        "",
        "| Fixture | Rule |",
        "|---|---|",
    ]
    for name, _base, _mutate, rule in INVALID_CASES:
        lines.append(f"| `{name}` | {rule} |")
    lines.append("")
    return ("\n".join(lines)).encode("ascii")


def build_export() -> Dict[str, bytes]:
    """Every generated file, ``{posix relative path: bytes}``."""
    files: Dict[str, bytes] = {}
    for name, schema in sp.json_schemas().items():
        files[f"schemas/{name}.schema.json"] = dump(schema)
    for name, payload in valid_fixtures().items():
        files[f"fixtures/valid/{name}.json"] = dump(payload)
    for name, payload in invalid_fixtures().items():
        files[f"fixtures/invalid/{name}.json"] = dump(payload)
    hash_hex = sp.protocol_hash()
    files["PROTOCOL_HASH"] = (hash_hex + "\n").encode("ascii")
    files["README.md"] = _readme(hash_hex)
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {path}\n" for path, data in sorted(files.items())
    )
    files["MANIFEST.sha256"] = manifest.encode("ascii")
    return files


def existing_generated(out: Path) -> Dict[str, bytes]:
    """The generated part of an existing tree (golden/ and other files ignored)."""
    found: Dict[str, bytes] = {}
    for top in GENERATED_TOP_FILES:
        path = out / top
        if path.is_file():
            found[top] = path.read_bytes()
    for sub in GENERATED_DIRS:
        root = out / sub
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    found[path.relative_to(out).as_posix()] = path.read_bytes()
    return found


def diff(out: Path, files: Dict[str, bytes]) -> List[str]:
    """Human-readable differences between ``out`` and a fresh export."""
    existing = existing_generated(out)
    problems = []
    for path in sorted(set(files) | set(existing)):
        if path not in existing:
            problems.append(f"missing: {path}")
        elif path not in files:
            problems.append(f"unexpected: {path}")
        elif existing[path] != files[path]:
            problems.append(f"changed: {path}")
    return problems


def write(out: Path, files: Dict[str, bytes]) -> List[str]:
    """Write the export into ``out``; remove stale generated files. Returns
    the relative paths that changed."""
    stale = [path for path in existing_generated(out) if path not in files]
    for path in stale:
        (out / path).unlink()
    changed = []
    for path, data in sorted(files.items()):
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)  # binary: LF on every platform
            changed.append(path)
    return changed + [f"removed {path}" for path in stale]


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="target tree (default: docs/station_protocol)")
    parser.add_argument("--check", action="store_true", help="exit 1 if the tree differs from a fresh export")
    args = parser.parse_args(argv)

    files = build_export()
    out = args.out.resolve()
    if args.check:
        problems = diff(out, files)
        if problems:
            print(f"station protocol export is out of date in {out}:")
            for problem in problems:
                print(f"  {problem}")
            rerun = "python scripts/export_station_protocol.py"
            if args.out != DEFAULT_OUT:
                rerun += f" --out {args.out}"
            print(f"re-run: {rerun}")
            return 1
        print(f"station protocol export is up to date in {out} (PROTOCOL_HASH {sp.protocol_hash()})")
        return 0
    changed = write(out, files)
    print(f"wrote {len(files)} files to {out} ({len(changed)} changed); PROTOCOL_HASH {sp.protocol_hash()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
