"""
Relay ingestion of station reports (plan 06 D15; C23 allow-list; E20 versions;
NEW-44). Pure logic, no eventlet and no Socket.IO, so it is tested directly.

* ``deploy_result`` (DeployResultV1): mapped to a pc_deployments transition
  (``deploy_result_transition``). The Client's own "delivered" (it handed the
  layout to Core) proves receipt, so it maps to ``received``.
* ``station_status`` (StationStatusV1): reduced to the allow-listed
  ``pc_health`` columns (``health_columns``); nothing else in the payload is
  stored, extra fields included. Free text that is stored (deploy detail,
  readiness detail, last_config_error) has every URL replaced by ``<url>``.
* Register (RegisterV1, E20): the version fields are checked one by one
  (``register_columns``); an invalid one is dropped (by name in the log,
  never by value) and the PC still registers. pc_id always comes from the
  session, never from a payload.
* ``HealthBuffer`` keeps the latest columns per PC (later values win per
  column) until the relay's flush writes them with ``upsert_health``: one
  dialect-aware ``INSERT ... ON CONFLICT (pc_id) DO UPDATE`` per column set
  (PostgreSQL and SQLite), in one transaction. Rows of PCs that no longer
  exist are dropped.
* ``MessageIdLRU`` deduplicates reports by (pc_id, message_id), 10 000 kept.
* Station input is untrusted (M2 review). Free text is cleaned before it is
  stored (``clean_text``: NUL and the other control characters except tab,
  newline and carriage return are removed, lone surrogates become U+FFFD),
  because PostgreSQL text columns refuse NUL and one such row would fail a
  whole health batch. ``scrub_report_text`` does this, and replaces every URL
  by ``<url>``, in the report's free-text fields *before* D1 validation, so a
  Core error message that quotes a camera URL with credentials is stored
  without them instead of losing the whole report (D15: "userinfo
  stripped"); a credential anywhere else still rejects the report (D1).
  Pattern checks use ``fullmatch``: ``$`` would also match before a trailing
  newline.
"""

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import func, select

from app.models.pc import PC
from app.models.pc_tracking import PcHealth
from app.schemas import station_protocol as sp
from app.services import deploy_tracking as dt

DEDUP_SIZE = 10_000
UPSERT_CHUNK = 500  # rows per statement (well under PostgreSQL's 65535 parameters)

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
# Every pattern below is applied with fullmatch (see the module docstring).
_VERSION_RE = re.compile(sp.VERSION_PATTERN)
_SHORT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_ID_OR_EMPTY_RE = re.compile(r"^[\x21-\x7e]{0,%d}$" % sp.MAX_CONFIG_ID_LENGTH)
_CAPABILITY_RE = re.compile(sp.CAPABILITY_PATTERN)
# Removed from stored free text: C0 controls except \t \n \r, and DEL.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# The report fields that hold free text (D1 ``_text``), as paths into the
# payload: a list step is written "[]". Only these are scrubbed before
# validation; every other field is a pattern-checked token or a number.
FREE_TEXT_PATHS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "DeployResultV1": (("detail",),),
    "StationStatusV1": (("config_status", "last_config_error"), ("readiness", "[]", "detail")),
}

# DeployResultV1.status -> pc_deployments target state.
REPORT_TARGETS: Dict[str, str] = {
    "delivered": dt.RECEIVED,
    "accepted": dt.ACCEPTED,
    "applied": dt.APPLIED,
    "failed": dt.FAILED,
    "unconfirmed": dt.UNCONFIRMED,
    "superseded": dt.SUPERSEDED,
}

# Client-minted ids (D7 ``ensure_config_id`` when the backend sent none) never
# have a row; they are not kept for retry.
CLIENT_MINTED_PREFIX = "client-"


def clean_text(text: str) -> str:
    """``text`` without the characters PostgreSQL or a log cannot take: NUL
    and the other control characters except tab, newline and carriage return
    are removed, and lone surrogates become U+FFFD."""
    return _LONE_SURROGATE_RE.sub("\ufffd", _CONTROL_RE.sub("", text))


def clean_value(value: Any) -> Any:
    """``value`` with :func:`clean_text` applied to every string in it (dict
    keys included), so nothing a station sent can make a row unwritable."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {clean_value(k): clean_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(clean_value(v) for v in value)
    return value


def strip_urls(text: Optional[str], max_length: int) -> Optional[str]:
    """Free text as stored: cleaned (:func:`clean_text`), every URL replaced
    by ``<url>``, bounded."""
    if text is None:
        return None
    return _URL_RE.sub("<url>", clean_text(str(text)))[:max_length]


def _scrub_path(node: Any, path: Tuple[str, ...]) -> Any:
    if not path:
        return _URL_RE.sub("<url>", clean_text(node)) if isinstance(node, str) else node
    step, rest = path[0], path[1:]
    if step == "[]":
        return [_scrub_path(item, rest) for item in node] if isinstance(node, list) else node
    if isinstance(node, dict) and step in node:
        out = dict(node)
        out[step] = _scrub_path(node[step], rest)
        return out
    return node


def scrub_report_text(name: str, data: Any) -> Any:
    """A copy of report ``data`` (DeployResultV1 / StationStatusV1) whose free
    text fields are cleaned and have every URL replaced by ``<url>``, so a
    credential quoted in an error message is never stored and does not get
    the report rejected (D15). Run before D1 validation; the bounds and
    every other field are left to the validation."""
    if not isinstance(data, dict):
        return data
    for path in FREE_TEXT_PATHS.get(name, ()):
        data = _scrub_path(data, path)
    return data


def report_size(data: Any) -> int:
    """Bytes of the report as compact JSON (the 64 KB bound, D1)."""
    try:
        return len(json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return sp.MAX_REPORT_BYTES + 1


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


class MessageIdLRU:
    """The last ``size`` report keys seen (thread-safe)."""

    def __init__(self, size: int = DEDUP_SIZE):
        self.size = size
        self._keys: "OrderedDict[Hashable, None]" = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._keys)

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            if key in self._keys:
                self._keys.move_to_end(key)
                return True
            return False

    def add(self, key: Hashable) -> None:
        with self._lock:
            self._keys[key] = None
            self._keys.move_to_end(key)
            while len(self._keys) > self.size:
                self._keys.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()


# --------------------------------------------------------------------------- #
# deploy_result
# --------------------------------------------------------------------------- #


def deploy_result_transition(model: "sp.DeployResultV1", client_version: Optional[str] = None
                             ) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """``(target_state, fields, detail)`` for one DeployResultV1.

    The station reported it, so the row is confirmable (it knows the id).
    """
    fields: Dict[str, Any] = {"confirmable": True}
    if model.mode is not None:
        fields["mode"] = model.mode
        fields["used_fallback"] = model.mode == "fallback_file"
    if model.persisted is not None:
        fields["persisted"] = model.persisted
    if model.source is not None:
        fields["source"] = model.source
    if model.core_version is not None:
        fields["core_version"] = model.core_version
    if client_version is not None:
        fields["client_version"] = client_version
    detail = strip_urls(model.detail, sp.MAX_DETAIL_LENGTH)
    if detail is None and model.stage:
        detail = f"stage {model.stage}"
    return REPORT_TARGETS[model.status], fields, detail


# --------------------------------------------------------------------------- #
# station_status -> pc_health (C23 allow-list)
# --------------------------------------------------------------------------- #

# Version fields a station_status shares with register: written only when the
# status carries them, so a status that omits one keeps the register value.
_SHARED_VERSION_FIELDS = ("client_version", "client_build", "core_installed_version", "core_running_version",
                          "core_version_source")

# Every pc_health column the relay may write. Anything else is never stored.
HEALTH_WRITABLE = frozenset(
    c.name for c in PcHealth.__table__.columns if c.name not in ("pc_id", "created_at", "updated_at")
)


def health_columns(model: "sp.StationStatusV1", received_at: int) -> Dict[str, Any]:
    """The allow-listed pc_health columns of one station_status."""
    core = model.core
    telemetry = model.telemetry
    wall = model.wall
    status = model.config_status
    cols: Dict[str, Any] = {
        "received_at": int(received_at),
        "reported_at": int(model.ts),
        "last_message_id": model.message_id,
        "health": model.health,
        "reasons": list(model.reasons),
        "core_state": core.state if core else None,
        "core_pid": core.pid if core else None,
        "core_uptime_sec": core.uptime_sec if core else None,
        "core_restarts_24h": core.restarts_24h if core else None,
        "core_last_exit": core.last_exit if core else None,
        "tap_state": telemetry.tap_state if telemetry else None,
        "telemetry_channels": (
            {
                name: {"target": ch.target, "connected": ch.connected,
                       "last_message_age_sec": ch.last_message_age_sec}
                for name, ch in telemetry.channels.items()
            }
            if telemetry else None
        ),
        "running_config_id": wall.running_config_id if wall else None,
        "deploying": wall.deploying if wall else None,
        "screens": wall.screens if wall else None,
        "streams_total": wall.streams_total if wall else None,
        "streams_playing": wall.streams_playing if wall else None,
        "streams_connecting": wall.streams_connecting if wall else None,
        "streams_failed": list(wall.streams_failed)[: sp.MAX_FAILED_SOURCE_IDS] if wall else None,
        "streams_failed_count": wall.streams_failed_count if wall else None,
        "flags": {str(k): bool(v) for k, v in model.flags.items()},
        "readiness": [{"code": r.code, "detail": strip_urls(r.detail, 200)} for r in model.readiness],
        "core_config_mode": status.core_config_mode if status else None,
        "mode_set_by": status.mode_set_by if status else None,
        "core_revision": status.core_revision if status else None,
        "config_core_version": status.core_version if status else None,
        "core_caps": list(status.core_caps) if status else None,
        "migration_status": status.migration_status if status else None,
        "drift_key_names": list(status.drift_key_names) if status else None,
        "legacy_available": status.legacy_available if status else None,
        "last_config_error": strip_urls(status.last_config_error, 200) if status else None,
        "data_dir_acl_warning": status.data_dir_acl_warning if status else None,
        "fleet_errors_count": status.fleet_errors_count if status else None,
    }
    for name in _SHARED_VERSION_FIELDS:
        value = getattr(model, name)
        if value is not None:
            cols[name] = value
    if core is not None and core.build_class is not None:
        cols["core_build_class"] = core.build_class
    if core is not None and core.exe_sha256 is not None:
        cols["core_exe_sha256"] = core.exe_sha256
    if telemetry is not None and telemetry.mode is not None:
        cols["telemetry_mode"] = telemetry.mode
    return cols


# --------------------------------------------------------------------------- #
# register -> pc_health (E20)
# --------------------------------------------------------------------------- #


def _version(value):
    return value if isinstance(value, str) and _VERSION_RE.fullmatch(value) else None


def _short_token(value):
    return value if isinstance(value, str) and _SHORT_TOKEN_RE.fullmatch(value) else None


def _sha256(value):
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _exe_size(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2 ** 40:
        return value
    return None


def _one_of(choices):
    return lambda value: value if isinstance(value, str) and value in choices else None


def _config_id_or_empty(value):
    return value if isinstance(value, str) and _CONFIG_ID_OR_EMPTY_RE.fullmatch(value) else None


REGISTER_FIELDS = {
    # RegisterV1 field -> (pc_health column, check)
    "client_version": ("client_version", _version),
    "client_build": ("client_build", _version),
    "core_version": ("core_version", _version),
    "core_installed_version": ("core_installed_version", _version),
    "core_running_version": ("core_running_version", _version),
    "core_version_source": ("core_version_source", _short_token),
    "core_exe_sha256": ("core_exe_sha256", _sha256),
    "core_exe_size": ("core_exe_size", _exe_size),
    "core_build_class": ("core_build_class", _one_of(sp.BUILD_CLASSES)),
    "delivery_policy": ("delivery_policy", _one_of(sp.DELIVERY_POLICIES)),
    "telemetry_mode": ("telemetry_mode", _short_token),
    "running_config_id": ("running_config_id", _config_id_or_empty),
}


def parse_capabilities(data: Any) -> Optional[List[str]]:
    """RegisterV1.capabilities: at most 16 names of at most 32 characters.
    ``None`` when the list is malformed (it then counts as no capability)."""
    caps = data.get("capabilities") if isinstance(data, dict) else None
    if caps is None:
        return []
    if not isinstance(caps, list) or len(caps) > sp.MAX_CAPABILITIES:
        return None
    out: List[str] = []
    for cap in caps:
        if (not isinstance(cap, str) or not cap or len(cap) > sp.MAX_CAPABILITY_LENGTH
                or not _CAPABILITY_RE.fullmatch(cap)):
            return None
        if cap not in out:
            out.append(cap)
    return out


def register_columns(data: Any, registered_at: int, relay_flags: Mapping[str, bool]
                     ) -> Tuple[Dict[str, Any], List[str]]:
    """``(columns, dropped)``: the pc_health columns of a register (every
    register field, None when absent or invalid, so a downgraded Client
    clears what a newer one reported) and the names of invalid fields."""
    payload = data if isinstance(data, dict) else {}
    cols: Dict[str, Any] = {"registered_at": int(registered_at), "relay_flags": dict(relay_flags)}
    dropped: List[str] = []
    for field, (column, check) in REGISTER_FIELDS.items():
        raw = payload.get(field)
        value = check(raw) if raw is not None else None
        if raw is not None and value is None:
            dropped.append(field)
        cols[column] = value
    caps = parse_capabilities(payload)
    if caps is None:
        dropped.append("capabilities")
        caps = []
    cols["capabilities"] = caps
    return cols, dropped


# --------------------------------------------------------------------------- #
# The health buffer and its flush
# --------------------------------------------------------------------------- #


class HealthBuffer:
    """Latest pending pc_health columns per PC (thread-safe)."""

    def __init__(self):
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def put(self, pc_id: str, columns: Mapping[str, Any]) -> None:
        unknown = set(columns) - HEALTH_WRITABLE
        if unknown:
            raise ValueError(f"not pc_health columns: {sorted(unknown)}")
        cleaned = {name: clean_value(value) for name, value in columns.items()}
        with self._lock:
            self._rows.setdefault(pc_id, {}).update(cleaned)

    def peek(self, pc_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._rows.get(pc_id)
            return dict(row) if row is not None else None

    def take(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows, self._rows = self._rows, {}
            return rows

    def restore(self, batch: Mapping[str, Mapping[str, Any]]) -> None:
        """Put a batch back after a failed write; newer values win."""
        with self._lock:
            for pc_id, columns in batch.items():
                merged = dict(columns)
                merged.update(self._rows.get(pc_id, {}))
                self._rows[pc_id] = merged

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


def _insert_for(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"no upsert for dialect {dialect!r}")
    return insert


def upsert_health(db, batch: Mapping[str, Mapping[str, Any]]) -> Tuple[int, int]:
    """Write ``batch`` (``{pc_id: columns}``) to pc_health. Returns
    ``(written, dropped)``; rows of PCs that no longer exist are dropped.
    Does not commit."""
    if not batch:
        return 0, 0
    ids = sorted(batch)
    existing = set(db.execute(select(PC.id).where(PC.id.in_(ids))).scalars())
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for pc_id in ids:
        if pc_id not in existing:
            continue
        columns = batch[pc_id]
        key = tuple(sorted(columns))
        groups.setdefault(key, []).append({"pc_id": pc_id, **columns})
    insert = _insert_for(db.get_bind().dialect.name)
    written = 0
    for key, rows in groups.items():
        for start in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[start:start + UPSERT_CHUNK]
            stmt = insert(PcHealth).values(chunk)
            update = {name: stmt.excluded[name] for name in key}
            update["updated_at"] = func.now()
            db.execute(stmt.on_conflict_do_update(index_elements=[PcHealth.pc_id], set_=update))
            written += len(chunk)
    return written, len(ids) - len([i for i in ids if i in existing])


@dataclass
class HealthWrite:
    """Outcome of :func:`write_health_batch`."""

    written: int = 0
    dropped: int = 0  # rows of PCs that no longer exist
    refused: List[Tuple[str, str]] = field(default_factory=list)  # (pc_id, error class): dropped, never retried
    restore: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # for the next flush (database unavailable)
    error: Optional[str] = None  # the error class of the first failure


def write_health_batch(open_db, batch: Mapping[str, Mapping[str, Any]]) -> HealthWrite:
    """Write ``batch`` with :func:`upsert_health` in one transaction.

    If the database refuses the batch for its data (``is_data_error``, e.g.
    a value PostgreSQL cannot store), the rows are written one PC per
    transaction, so one PC's bad row cannot keep every other PC's health
    out: the refused rows are dropped (``refused``), the others written. Any
    other error (the database is unavailable) leaves the rows not yet
    written in ``restore`` for the next flush, as before.
    """

    def write(rows: Mapping[str, Mapping[str, Any]]) -> Tuple[int, int]:
        db = open_db()
        try:
            counts = upsert_health(db, rows)
            db.commit()
            return counts
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            raise
        finally:
            db.close()

    out = HealthWrite()
    if not batch:
        return out
    try:
        out.written, out.dropped = write(batch)
        return out
    except Exception as exc:
        out.error = type(exc).__name__
        if not dt.is_data_error(exc):
            out.restore = {pc_id: dict(cols) for pc_id, cols in batch.items()}
            return out
    items = sorted(batch.items())
    for index, (pc_id, columns) in enumerate(items):
        try:
            written, dropped = write({pc_id: columns})
        except Exception as exc:
            if dt.is_data_error(exc):
                out.refused.append((pc_id, type(exc).__name__))
                continue
            out.restore = {p: dict(c) for p, c in items[index:]}
            break
        out.written += written
        out.dropped += dropped
    return out


def merge_reports(rows: Iterable[Tuple[str, Mapping[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Fold ``(pc_id, columns)`` in order into one batch (later wins)."""
    out: Dict[str, Dict[str, Any]] = {}
    for pc_id, columns in rows:
        out.setdefault(pc_id, {}).update(columns)
    return out
