"""
Fleet Core settings and the relay call ``get_station_config`` (plan 06 C10;
OD-26 (c), OD-4 (c), E2, N1, NEW-48, NEW-53, X-17).

Category ``core`` of ``system_settings`` holds the fleet-wide values TrueLive
Client (C12, from 1.5.0) uses when it generates Core's config.json (C5). The
backend only stores, validates and serves them; every value ships empty
(unmanaged), so nothing changes on a station until ops sets one.

=================================  =========  =====  ====================================
key                                type       enc.   meaning
=================================  =========  =====  ====================================
core.client_managed_config         boolean    no     fleet kill switch (default true)
core.notification_address          string     yes    Core ``notification_address``
core.logs_server_address           string     yes    Core ``logs_server_address``
core.heart_beat_server_address     string     yes    Core ``heart_beat_server_address``
core.ack_address                   string     yes    Core ``ack_address``
core.heartbeat_interval_sec        integer    no     0 = unmanaged, else 5..3600
core.client_admin_password_hash    string     yes    fleet admin hash for the Client gate
=================================  =========  =====  ====================================

Address values (OD-26 (c), C5): ``''`` means unmanaged (the station keeps its
own value), ``none`` forces the key empty, anything else is a ``ws://`` or
``wss://`` URL of at most 2048 characters, without whitespace, control
characters or ``@`` (no credentials; D1 rejects station reports that carry
userinfo). The placeholders ``{pc_id}`` and ``{pc_name_urlenc}`` are expanded
by the Client per station; no other ``{...}`` is allowed.

The admin hash (OD-4 (c)) is ``''`` (each station keeps its local password)
or ``pbkdf2_sha256$<iterations>$<salt>$<hash>``: ``hash`` is the standard
base64 (padded) of ``hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
salt.encode("utf-8"), iterations, 32)``, the salt is 8-128 characters of
``[A-Za-z0-9+/=._-]`` used as its UTF-8 bytes, and iterations are
100 000..10 000 000 (the portal uses 200 000; the upper bound keeps a
station's password check fast). :func:`make_admin_password_hash` is the
reference implementation for the portal (C11) and the Client (C12).

The relay call (one Socket.IO event, ACK only)::

    get_station_config(<anything>) -> {"status": "ok", "revision": "<sha256 hex>",
                                       "values": {key: value, ...},
                                       "errors": [{"key": key, "error": code}, ...]}

* Served only to the PC's current registered session (pc_id from the relay
  session, never from the payload); any other session gets
  ``{"status": "rejected", "reason": "not registered", "revision": None,
  "values": {}, "errors": []}``. A database failure answers ``status: error``
  (``reason: unavailable``) in the same shape; both are transient for the
  Client, which keeps its cached values.
* ``values`` holds every key that could be read, typed: a bool, an int, and
  strings that are ``''`` rather than None. A key whose row is missing
  (``missing``), whose value cannot be decrypted (``decrypt_failed``), that is
  encrypted while the process has no valid SETTINGS_ENCRYPTION_KEY
  (``no_key`` / ``invalid_key``, even when it is empty), or whose stored value
  breaks the rules above (``invalid_value``) is left out of ``values`` and
  listed in ``errors``. No ciphertext ever leaves the relay.
* ``revision`` is the sha256 of ``values`` as canonical JSON (sorted keys,
  ``,``/``:`` separators, ASCII), so it changes only when a served value does,
  never when the same value is re-encrypted.
* The relay reads with one query that bypasses the settings cache (NEW-48)
  and keeps the result for 60 s; concurrent calls share one read.
* ``registered`` advertises ``station_config.v1`` once the handler exists
  (D12, X-17): the Client calls only then.
"""

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from app.core.startup_check import KEY_INVALID, KEY_MISSING, KEY_OK, resolve_settings_encryption_key, settings_key_status
from app.models.system_setting import SystemSetting

CATEGORY = "core"
EVENT_GET_STATION_CONFIG = "get_station_config"

KEY_CLIENT_MANAGED_CONFIG = "core.client_managed_config"
KEY_NOTIFICATION_ADDRESS = "core.notification_address"
KEY_LOGS_SERVER_ADDRESS = "core.logs_server_address"
KEY_HEART_BEAT_SERVER_ADDRESS = "core.heart_beat_server_address"
KEY_ACK_ADDRESS = "core.ack_address"
KEY_HEARTBEAT_INTERVAL_SEC = "core.heartbeat_interval_sec"
KEY_CLIENT_ADMIN_PASSWORD_HASH = "core.client_admin_password_hash"

KIND_BOOL = "bool"
KIND_ADDRESS = "address"
KIND_INTERVAL = "interval"
KIND_ADMIN_HASH = "admin_hash"

UNMANAGED = ""
FORCE_EMPTY = "none"
# Encrypted values the settings API shows as they are: they say whether a key
# is managed, never what a set value is (the C11 badges).
UNMASKED_VALUES = (UNMANAGED, FORCE_EMPTY)

PLACEHOLDERS = ("pc_id", "pc_name_urlenc")
ADDRESS_MAX_LENGTH = 2048
HEARTBEAT_MIN_SEC = 5
HEARTBEAT_MAX_SEC = 3600

ADMIN_HASH_ALGORITHM = "pbkdf2_sha256"
ADMIN_HASH_ITERATIONS = 200_000
ADMIN_HASH_MIN_ITERATIONS = 100_000
ADMIN_HASH_MAX_ITERATIONS = 10_000_000
ADMIN_HASH_BYTES = 32

ERROR_MISSING = "missing"
ERROR_NO_KEY = KEY_MISSING  # "no_key"
ERROR_INVALID_KEY = KEY_INVALID  # "invalid_key"
ERROR_DECRYPT = "decrypt_failed"
ERROR_INVALID_VALUE = "invalid_value"

STATUS_OK = "ok"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

_ADDRESS_RE = re.compile(r"^wss?://\S+$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_PLACEHOLDER_SAMPLES = {"pc_id": "PC_1", "pc_name_urlenc": "Wall%201"}
_INTERVAL_RE = re.compile(r"^(0|[1-9][0-9]{0,3})$")
_ADMIN_HASH_RE = re.compile(
    r"^pbkdf2_sha256\$([1-9][0-9]{0,8})\$([A-Za-z0-9+/=._-]{8,128})\$([A-Za-z0-9+/]{43}=)$"
)
_SALT_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


@dataclass(frozen=True)
class CoreSetting:
    key: str
    kind: str
    data_type: str
    encrypted: bool
    default: str
    description: str


_ADDRESS_HELP = (
    "ws:// or wss:// URL. Empty = unmanaged (each station keeps its own value); none = force empty. "
    "{pc_id} and {pc_name_urlenc} are expanded per station. A change restarts Core once on every "
    "managed station (TrueLive Client 1.5.0+)"
)

CORE_SETTINGS: Tuple[CoreSetting, ...] = (
    CoreSetting(
        KEY_CLIENT_MANAGED_CONFIG, KIND_BOOL, "boolean", False, "true",
        "Fleet kill switch for the Core config generated by TrueLive Client 1.5.0+. false returns stations "
        "that still have a legacy Core config to it (with the latest layout); stations without one ignore it",
    ),
    CoreSetting(KEY_NOTIFICATION_ADDRESS, KIND_ADDRESS, "string", True, "",
                "Core alert (notification) server. " + _ADDRESS_HELP),
    CoreSetting(KEY_LOGS_SERVER_ADDRESS, KIND_ADDRESS, "string", True, "",
                "Core logs server (sent only to Core builds that report safely). " + _ADDRESS_HELP),
    CoreSetting(KEY_HEART_BEAT_SERVER_ADDRESS, KIND_ADDRESS, "string", True, "",
                "Core heartbeat server (sent only to Core builds that report safely). " + _ADDRESS_HELP),
    CoreSetting(KEY_ACK_ADDRESS, KIND_ADDRESS, "string", True, "",
                "Core deploy acknowledgement target. " + _ADDRESS_HELP),
    CoreSetting(KEY_HEARTBEAT_INTERVAL_SEC, KIND_INTERVAL, "integer", False, "0",
                "Core heartbeat interval in seconds: 0 = unmanaged, else 5-3600. A change restarts Core once"),
    CoreSetting(
        KEY_CLIENT_ADMIN_PASSWORD_HASH, KIND_ADMIN_HASH, "string", True, "",
        "Fleet admin password for TrueLive Client 1.5.0+, as pbkdf2_sha256$iterations$salt$hash (the portal "
        "computes it; never the password itself). Empty = each station keeps its local admin password",
    ),
)
SPECS: Dict[str, CoreSetting] = {spec.key: spec for spec in CORE_SETTINGS}
KEYS: Tuple[str, ...] = tuple(spec.key for spec in CORE_SETTINGS)
ADDRESS_KEYS: Tuple[str, ...] = tuple(spec.key for spec in CORE_SETTINGS if spec.kind == KIND_ADDRESS)


def seed_entries() -> List[Dict[str, Any]]:
    """The ``core`` rows for the settings seeder (settings_seeder.DEFAULT_SETTINGS)."""
    return [
        {"key": spec.key, "value": spec.default, "category": CATEGORY, "description": spec.description,
         "is_encrypted": spec.encrypted, "data_type": spec.data_type}
        for spec in CORE_SETTINGS
    ]


# --------------------------------------------------------------------------- #
# Validation (API writes and the relay's read use the same rules)
# --------------------------------------------------------------------------- #


def _check_address(value: str) -> Optional[str]:
    if value in UNMASKED_VALUES:
        return None
    if len(value) > ADDRESS_MAX_LENGTH:
        return f"is longer than {ADDRESS_MAX_LENGTH} characters"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "contains control characters"
    if any(m.group(1) not in PLACEHOLDERS for m in _PLACEHOLDER_RE.finditer(value)):
        return "uses an unknown placeholder (only {pc_id} and {pc_name_urlenc} are expanded)"
    rest = _PLACEHOLDER_RE.sub("", value)
    if "{" in rest or "}" in rest:
        return "has an unmatched { or }"
    sample = _PLACEHOLDER_RE.sub(lambda m: _PLACEHOLDER_SAMPLES[m.group(1)], value)
    if not _ADDRESS_RE.match(sample):
        return "must be a ws:// or wss:// URL without spaces, empty (unmanaged) or none (force empty)"
    if "@" in sample:
        return "must not contain '@' (no credentials in the URL)"
    return None


def _check_interval(value: str) -> Optional[str]:
    if _INTERVAL_RE.match(value):
        number = int(value)
        if number == 0 or HEARTBEAT_MIN_SEC <= number <= HEARTBEAT_MAX_SEC:
            return None
    return f"must be 0 (unmanaged) or a whole number of seconds from {HEARTBEAT_MIN_SEC} to {HEARTBEAT_MAX_SEC}"


def _check_bool(value: str) -> Optional[str]:
    return None if value in ("true", "false") else "must be true or false"


def _check_admin_hash(value: str) -> Optional[str]:
    if value == UNMANAGED:
        return None
    # The pattern fixes the digest at 43 base64 characters plus one "=": 32 bytes.
    match = _ADMIN_HASH_RE.match(value)
    if not match:
        return (f"must be empty or {ADMIN_HASH_ALGORITHM}$<iterations>$<salt>$<hash> as computed by the portal "
                "(never the password itself)")
    iterations = int(match.group(1))
    if not ADMIN_HASH_MIN_ITERATIONS <= iterations <= ADMIN_HASH_MAX_ITERATIONS:
        return f"must use {ADMIN_HASH_MIN_ITERATIONS} to {ADMIN_HASH_MAX_ITERATIONS} iterations"
    return None


_CHECKS: Dict[str, Callable[[str], Optional[str]]] = {
    KIND_BOOL: _check_bool,
    KIND_ADDRESS: _check_address,
    KIND_INTERVAL: _check_interval,
    KIND_ADMIN_HASH: _check_admin_hash,
}


def check_value(key: str, value: Any) -> Optional[str]:
    """Why ``value`` is not acceptable for the fleet key ``key``, or None.

    Keys outside the fleet set are not checked (None). The reason never
    repeats the value, which may be secret."""
    spec = SPECS.get(key)
    if spec is None:
        return None
    if not isinstance(value, str):
        return "must be a string"
    return _CHECKS[spec.kind](value)


def parse_value(key: str, plaintext: str) -> Any:
    """The typed value served to stations: bool, int or str (never None).
    Raises ValueError (without the value) when it breaks the rules."""
    reason = check_value(key, plaintext)
    if reason is not None:
        raise ValueError(f"{key} {reason}")
    kind = SPECS[key].kind
    if kind == KIND_BOOL:
        return plaintext == "true"
    if kind == KIND_INTERVAL:
        return int(plaintext)
    return plaintext


def make_admin_password_hash(password: str, salt: Optional[str] = None,
                             iterations: int = ADMIN_HASH_ITERATIONS) -> str:
    """Reference encoding of ``core.client_admin_password_hash`` (C11 computes
    the same in WebCrypto; the Client verifies with hashlib.pbkdf2_hmac)."""
    if salt is None:
        salt = "".join(secrets.choice(_SALT_ALPHABET) for _ in range(22))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations,
                                 ADMIN_HASH_BYTES)
    return f"{ADMIN_HASH_ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode('ascii')}"


# --------------------------------------------------------------------------- #
# The loader
# --------------------------------------------------------------------------- #


def revision(values: Mapping[str, Any]) -> str:
    """sha256 hex of the served values as canonical JSON."""
    canonical = json.dumps(dict(values), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class StationConfig:
    """One read of the fleet keys: what can be served, and what cannot."""

    values: Mapping[str, Any]
    errors: Tuple[Tuple[str, str], ...]
    revision: str

    def ack(self) -> Dict[str, Any]:
        """The ACK of ``get_station_config`` (fresh containers per call)."""
        return {
            "status": STATUS_OK,
            "revision": self.revision,
            "values": dict(self.values),
            "errors": [{"key": key, "error": code} for key, code in self.errors],
        }


def refusal_ack(status: str, reason: str) -> Dict[str, Any]:
    """A non-ok ACK in the same shape: nothing to apply."""
    return {"status": status, "reason": reason, "revision": None, "values": {}, "errors": []}


def load_station_config(db, *, key_status: str, decrypt: Optional[Callable[[str], str]]) -> StationConfig:
    """Read every fleet key with one query (never the settings cache, NEW-48)
    and decrypt strictly. ``key_status`` is this process's
    SETTINGS_ENCRYPTION_KEY status (ok, no_key, invalid_key) and ``decrypt``
    the strict decryptor (``SystemSettingsService.decrypt_strict``), which
    raises instead of returning ciphertext."""
    rows = {
        row.key: row
        for row in db.query(SystemSetting.key, SystemSetting.value, SystemSetting.is_encrypted)
        .filter(SystemSetting.key.in_(KEYS))
        .all()
    }
    values: Dict[str, Any] = {}
    errors: List[Tuple[str, str]] = []
    for spec in CORE_SETTINGS:
        row = rows.get(spec.key)
        if row is None:
            errors.append((spec.key, ERROR_MISSING))
            continue
        stored = row.value or ""
        if row.is_encrypted:
            if key_status != KEY_OK or decrypt is None:
                errors.append((spec.key, key_status if key_status in (ERROR_NO_KEY, ERROR_INVALID_KEY)
                               else ERROR_DECRYPT))
                continue
            try:
                plaintext = decrypt(stored)
            except Exception:
                errors.append((spec.key, ERROR_DECRYPT))
                continue
        else:
            plaintext = stored
        try:
            values[spec.key] = parse_value(spec.key, plaintext)
        except ValueError:
            errors.append((spec.key, ERROR_INVALID_VALUE))
    return StationConfig(values=values, errors=tuple(errors), revision=revision(values))


def process_decryptor() -> Tuple[str, Optional[Callable[[str], str]]]:
    """``(key_status, decrypt)`` for this process, resolved as every backend
    process resolves SETTINGS_ENCRYPTION_KEY (app/core/startup_check). The
    settings service is imported only with a valid key, so a relay with an
    invalid key keeps running (D28) and serves the plain keys."""
    from app.core.config import settings as app_settings

    status = settings_key_status(resolve_settings_encryption_key(app_settings))
    if status != KEY_OK:
        return status, None
    from app.services.system_settings_service import system_settings_service

    if system_settings_service.key_status != KEY_OK:
        return system_settings_service.key_status, None
    return KEY_OK, system_settings_service.decrypt_strict


# --------------------------------------------------------------------------- #
# The relay's cache
# --------------------------------------------------------------------------- #


class StationConfigCache:
    """The last :class:`StationConfig`, reloaded at most once per ``ttl``.

    Concurrent callers share the one load in flight (single flight), so a
    reconnect storm costs one database read. A failed load is remembered for
    ``error_ttl`` (callers get None) so a database outage is not hammered.
    ``event_factory`` must match the server's async mode (the relay passes
    ``sio.eio.create_event``: green under eventlet). The guard lock is held
    only for bookkeeping that never yields, so it is safe under eventlet."""

    def __init__(self, ttl_seconds: float = 60.0, error_ttl_seconds: float = 5.0, wait_seconds: float = 8.0,
                 clock: Callable[[], float] = time.monotonic, event_factory: Callable[[], Any] = threading.Event):
        self.ttl_seconds = ttl_seconds
        self.error_ttl_seconds = error_ttl_seconds
        self.wait_seconds = wait_seconds
        self._clock = clock
        self._event_factory = event_factory
        self._guard = threading.Lock()
        self._entry: Optional[Tuple[float, Optional[StationConfig]]] = None
        self._inflight = None
        self.loads = 0
        self.failures = 0
        self.hits = 0
        self.timeouts = 0

    def invalidate(self) -> None:
        with self._guard:
            self._entry = None

    def get(self, load: Callable[[], StationConfig]) -> Optional[StationConfig]:
        """The cached result, calling ``load`` when it is older than the TTL.
        Returns None after a failed load (within ``error_ttl``) or when the
        load in flight takes longer than ``wait_seconds``. The caller that ran
        a failing load gets its exception."""
        with self._guard:
            entry = self._entry
            if entry is not None and self._clock() < entry[0]:
                self.hits += 1
                return entry[1]
            inflight = self._inflight
            leader = inflight is None
            if leader:
                inflight = self._inflight = self._event_factory()
        if not leader:
            if not inflight.wait(self.wait_seconds):
                with self._guard:
                    self.timeouts += 1
                return None
            with self._guard:
                self.hits += 1
                return self._entry[1] if self._entry is not None else None
        try:
            result = load()
        except BaseException:
            self._finish(None, self.error_ttl_seconds, inflight, failed=True)
            raise
        self._finish(result, self.ttl_seconds, inflight)
        return result

    def _finish(self, result: Optional[StationConfig], ttl: float, inflight, failed: bool = False) -> None:
        with self._guard:
            self._entry = (self._clock() + ttl, result)
            self._inflight = None
            if failed:
                self.failures += 1
            else:
                self.loads += 1
        inflight.set()
