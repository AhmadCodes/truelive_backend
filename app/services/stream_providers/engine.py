"""
The stream provider engine: resolves provider cameras to playable addresses,
caches them, renews them before they expire and re-delivers a station's
layout when its addresses changed. Written against base.StreamProvider only.

A camera of a provider is stored as ``provider://<provider id>/<camera id>``
(the *reference*). Stations never see a reference: the deploy path
(deploy_service) and ``refresh_pc`` replace it with the minted address, or
with ``http://127.0.0.1:9/stream-unavailable?code=<code>`` so the cell fails
alone with a reason. Only cameras that are a cell of the station's wall get an
address; a camera that is merely listed in ``LocationUris`` gets the
placeholder (code ``not-on-wall``) and costs no vendor session.

Redis (prefix ``truelive:sp:``); every key can be lost and is rebuilt:

* ``addr:<provider>:<camera>``  the minted address, its expiry, the
  provider's handle and the last error (encrypted; written only under the
  ``mint:`` lock; TTL 7 days)
* ``use:`` / ``probe:<provider>:<camera>``  when a connected station last
  needed the address; the server-side check's bookkeeping
* ``lock:pc:<pc id>``  held while a layout is sent to the PC (a deploy or a
  refresh), so the two never cross
* ``pinfo:<provider>``  ``info`` of the provider's latest mint
* ``pc:<pc id>``  the unresolved layout as last deployed with what was sent
  (encrypted), or a marker that the deploy has no provider camera, or
  ``blocked``. A record whose ``config_id`` is not the deploy the station
  holds (the newest one that reached it) is rebuilt from the database, and
  only when the regenerated layout hashes to that deploy's ``config_hash``: a
  renewal never deploys edits nobody deployed.
* ``ack:<pc id>``  id of the last delivery the station acknowledged (written
  by the relay)

Nothing here logs a URL, a token or a key. httpx logs request URLs at INFO,
and a probe URL holds the token, so its logger is raised to WARNING.
"""
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple
from urllib.parse import quote, unquote

import httpx

from app.core.config import settings
from app.services.stream_providers import registry
from app.services.stream_providers.base import (AUTH, INVALID, LIMIT, NOT_FOUND, UNAVAILABLE, Address,
                                                ProviderError, StreamProvider)
from app.utils.url_processor import is_provider_ref, make_provider_ref, parse_provider_ref

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

PREFIX = "truelive:sp:"
# The code is in the query: Core 1.3.1 adopts a query-only change without rebuilding the view.
PLACEHOLDER = "http://127.0.0.1:9/stream-unavailable?code="

OK = "ok"
CODE_REMOVED = "provider-removed"
CODE_KEY = "key-refused"
CODE_CAMERA = "camera-not-found"
CODE_LIMIT = "limit-reached"
CODE_UNAVAILABLE = "provider-unavailable"
CODE_OLD_CORE = "core-too-old"
CODE_PENDING = "pending"
CODE_NOT_ON_WALL = "not-on-wall"
ERROR_CODES = {AUTH: CODE_KEY, NOT_FOUND: CODE_CAMERA, LIMIT: CODE_LIMIT, UNAVAILABLE: CODE_UNAVAILABLE,
               INVALID: CODE_UNAVAILABLE}

MSG_REMOVED = "The stream provider of this camera was removed."
MSG_OLD_CORE = ("This station is not confirmed to run TrueLive Live 1.3.0 or later; provider cameras are not "
                "sent to it.")
MSG_PENDING = "The provider has not answered yet; the address follows when it does."
MSG_NOT_PLAYING = "The provider gave an address, but it does not play (is the camera online?)"
MSG_DISABLED = "Stream providers are turned off on this server."
MSG_BLOCKED = "The layout changed since the last deploy; deploy to this PC to keep its provider cameras renewed."
MSG_KEY_UNREADABLE = "The provider's key cannot be read with this server's SETTINGS_ENCRYPTION_KEY; enter it again."

DEFAULTS: Dict[str, float] = {
    "renew_fraction": 0.5,
    "renew_min_margin_sec": 1800,
    "watch_interval_sec": 60,
    "release_after_sec": 300,
    "deploy_resolve_budget_sec": 6,
    "request_timeout_sec": 8,
    "pc_refresh_min_interval_sec": 60,
}
SETTINGS_CATEGORY = "stream_providers"

ADDR_TTL_SEC = 7 * 86400
MINT_LOCK_SEC = 60         # more than a mint and two probes at the largest request timeout
PC_LOCK_SEC = 30
PC_LOCK_WAIT_SEC = 8.0
MAX_REQUEST_TIMEOUT_SEC = 10.0
MINT_WAIT_SEC = 3.0
TICK_LOCK_SEC = 120
POOL_SIZE = 8
BACKOFF_FIRST_SEC = 30
BACKOFF_MAX_SEC = 900
EXPIRY_GUARD_SEC = 30      # an address this close to its expiry is not handed out
EXPIRING_SEC = 1800        # status "expiring" from here on when no fresh address came
NO_EXPIRY_LIFE_SEC = 3600  # renewal rhythm of an address whose provider gives no expiry
REFRESH_BUDGET_SEC = 10
IMMEDIATE_PROBE_MIN_SEC = 10  # a connect check does not repeat a probe that just answered
RENEW_BUDGET_SEC = 30
ACK_RETRIES = 2            # re-deliveries of unchanged addresses to a station that does not acknowledge
MIN_CORE = (1, 3, 0)


class Resolved(NamedTuple):
    url: Optional[str]
    code: str
    message: str


def now() -> float:
    """The engine's clock (tests replace it)."""
    return time.time()


# --------------------------------------------------------------------------- #
# Redis and encryption
# --------------------------------------------------------------------------- #

_redis = None


def redis_client():
    global _redis
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    return _redis


def _crypto():
    from app.services.system_settings_service import system_settings_service

    return system_settings_service


def _load(key: str) -> Optional[dict]:
    """A stored record, or None (missing, or written with another key)."""
    raw = redis_client().get(PREFIX + key)
    if not raw:
        return None
    try:
        value = json.loads(_crypto().decrypt_strict(raw.decode("ascii")))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _save(key: str, value: dict, ttl: Optional[int] = None) -> None:
    redis_client().set(PREFIX + key, _crypto()._encrypt_value(json.dumps(value, default=str)), ex=ttl)


def _get_text(key: str) -> Optional[str]:
    raw = redis_client().get(PREFIX + key)
    return raw.decode("utf-8", "replace") if raw else None


def _lock(name: str, ttl: int) -> Optional[str]:
    """Take the lock ``name``; the returned value proves ownership to ``_unlock``."""
    owner = uuid.uuid4().hex
    return owner if redis_client().set(PREFIX + name, owner, nx=True, ex=ttl) else None


def _unlock(name: str, owner: Optional[str]) -> None:
    """Release the lock only when it is still ours (it may have expired and
    been taken by another worker)."""
    # shortcut: get-then-delete is not atomic; use a Lua compare-and-delete if work ever outlives the TTL often.
    client = redis_client()
    if owner and (client.get(PREFIX + name) or b"").decode("ascii", "replace") == owner:
        client.delete(PREFIX + name)


def lock_pcs(pc_ids: Iterable[str], wait_sec: float = PC_LOCK_WAIT_SEC) -> Dict[str, str]:
    """Take the send lock of each PC, waiting at most ``wait_sec`` in total
    for refreshes that are sending right now. A deploy holds these from before
    its send until its answers are recorded; a lock that could not be taken
    is left out (the deploy goes on)."""
    deadline = time.monotonic() + wait_sec
    held: Dict[str, str] = {}
    for pc_id in sorted(set(pc_ids)):
        while True:
            owner = _lock(f"lock:pc:{pc_id}", PC_LOCK_SEC)
            if owner or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if owner:
            held[pc_id] = owner
    return held


def unlock_pcs(held: Dict[str, str]) -> None:
    for pc_id, owner in held.items():
        try:
            _unlock(f"lock:pc:{pc_id}", owner)
        except Exception:
            pass  # it expires by itself


def _camera_key(kind: str, provider_id: str, camera_id: str) -> str:
    return f"{kind}:{provider_id}:{quote(camera_id, safe='')}"


def _ref_key(kind: str, ref: str) -> Optional[str]:
    parsed = parse_provider_ref(ref)
    return _camera_key(kind, *parsed) if parsed else None


# --------------------------------------------------------------------------- #
# Settings and providers
# --------------------------------------------------------------------------- #


def config(db) -> Dict[str, float]:
    """The seven settings (category ``stream_providers``), read from the
    database each time so a change in the portal reaches every process."""
    out = dict(DEFAULTS)
    try:
        from app.models.system_setting import SystemSetting

        rows = db.query(SystemSetting.key, SystemSetting.value).filter(
            SystemSetting.category == SETTINGS_CATEGORY).all()
        for key, value in rows:
            name = key.split(".", 1)[-1]
            if name in out and value not in (None, ""):
                try:
                    out[name] = float(value)
                except (TypeError, ValueError):
                    logger.warning(f"Setting {key} is not a number; using the default")
    except Exception as exc:
        logger.warning(f"Stream provider settings not readable ({type(exc).__name__}); using the defaults")
    out["renew_fraction"] = min(0.95, max(0.05, out["renew_fraction"]))
    out["request_timeout_sec"] = min(MAX_REQUEST_TIMEOUT_SEC, max(1.0, out["request_timeout_sec"]))
    return out


class _Provider(NamedTuple):
    id: str
    name: str
    impl: Optional[StreamProvider]
    settings: dict
    error: Optional[str]


def decrypt_secrets(row) -> dict:
    """The provider's secret fields. Raises when they cannot be read."""
    if not row.secrets:
        return {}
    value = json.loads(_crypto().decrypt_strict(row.secrets))
    if not isinstance(value, dict):
        raise ValueError("secrets")
    return value


def load_providers(db, provider_ids: Iterable[str]) -> Dict[str, _Provider]:
    from app.models.stream_provider import StreamProvider as Row

    ids = sorted(set(provider_ids))
    out: Dict[str, _Provider] = {}
    if not ids:
        return out
    for row in db.query(Row).filter(Row.id.in_(ids)).all():
        impl = registry.get(row.type)
        merged = dict(row.settings or {})
        error = None
        if impl is None:
            error = f"{row.name}: provider type '{row.type}' is not installed on this server."
        else:
            try:
                merged.update(decrypt_secrets(row))
            except Exception:
                error = f"{row.name}: {MSG_KEY_UNREADABLE}"
        out[row.id] = _Provider(row.id, row.name, impl, merged, error)
    return out


def load_provider(db, provider_id: str) -> Optional[Tuple[StreamProvider, dict]]:
    entry = load_providers(db, [provider_id]).get(provider_id)
    if entry is None or entry.impl is None or entry.error:
        return None
    return entry.impl, entry.settings


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def renew_at(minted_at: float, expires_at: Optional[float], fraction: float, min_margin: float) -> float:
    """When to renew: at ``fraction`` of the lifetime, and never later than
    ``min_margin`` before the expiry (half the lifetime for a short one)."""
    if expires_at is None:
        expires_at = minted_at + NO_EXPIRY_LIFE_SEC
    life = max(0.0, expires_at - minted_at)
    return min(minted_at + fraction * life, expires_at - min(min_margin, life / 2))


def _version(value: Any) -> Optional[Tuple[int, int, int]]:
    match = re.match(r"\s*v?(\d+)\.(\d+)(?:\.(\d+))?", str(value or ""))
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)) if match else None


def core_supports_hls(pc, health) -> bool:
    """True when the station is known to run Core 1.3.0 or later. A version a
    Client reported decides; without one, the admin-set ``pcs.hls_capable``."""
    for name in ("core_running_version", "core_installed_version", "core_version"):
        parsed = _version(getattr(health, name, None)) if health is not None else None
        if parsed is not None:
            return parsed >= MIN_CORE
    return getattr(pc, "hls_capable", None) is True


def _sources(payload: dict) -> Iterator[dict]:
    """Every source of a layout: ``screens[].source_groups[][]`` and
    ``screens[].views.*.sources[]``."""
    for screen in payload.get("screens") or []:
        if not isinstance(screen, dict):
            continue
        for group in screen.get("source_groups") or []:
            for source in group if isinstance(group, list) else []:
                if isinstance(source, dict):
                    yield source
        views = screen.get("views")
        for view in (views.values() if isinstance(views, dict) else views if isinstance(views, list) else []):
            for source in (view.get("sources") or []) if isinstance(view, dict) else []:
                if isinstance(source, dict):
                    yield source


def _visit(payload: dict, swap) -> None:
    """Call ``swap(ref, source, location_item)`` for every reference and put
    its result in place. ``source`` is set for a cell's own URL."""
    for source in _sources(payload):
        if is_provider_ref(source.get("url")):
            source["url"] = swap(source["url"].strip(), source, None)
        uris = source.get("LocationUris")
        for index, item in enumerate(uris if isinstance(uris, list) else []):
            if isinstance(item, dict) and is_provider_ref(item.get("url")):
                item["url"] = swap(item["url"].strip(), None, item)
            elif is_provider_ref(item):
                uris[index] = swap(item.strip(), None, None)


def layout_index(payload: dict) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """``({source id: reference} for the cells, {reference: {camera_id,
    camera_name}} for every reference, LocationUris included)``."""
    cells: Dict[str, str] = {}
    cameras: Dict[str, dict] = {}

    def note(ref, source, item):
        entry = cameras.setdefault(ref, {"camera_id": None, "camera_name": None})
        if source is not None:
            cells[str(source.get("id"))] = ref
            entry["camera_id"] = entry["camera_id"] or source.get("camera_id")
            entry["camera_name"] = entry["camera_name"] or source.get("camera_name")
        elif item is not None:
            entry["camera_name"] = entry["camera_name"] or item.get("osd_text")
        return ref

    _visit(payload, note)
    return cells, cameras


def layout_refs(payload: dict) -> List[str]:
    """Every reference of the layout, LocationUris included."""
    return sorted(layout_index(payload)[1])


def wall_refs(payload: dict) -> List[str]:
    """The references that are a cell of the wall: the only ones that get an
    address (and so a vendor session)."""
    return sorted(set(layout_index(payload)[0].values()))


def _record_refs(rec: dict) -> List[str]:
    return sorted(set((rec.get("refs") or {}).values()))


def resolve_layout(payload: dict, resolved: Dict[str, Resolved], core_ok: bool = True):
    """``(copy with every reference replaced, {source id: reference},
    [{ref, camera_id, camera_name, code, message}] of the cells that are not
    playable)``. A reference that is no cell of this wall (only listed in
    ``LocationUris``) becomes the ``not-on-wall`` placeholder and is not
    reported. The input is not changed."""
    cells, cameras = layout_index(payload)
    wall = set(cells.values())
    out = copy.deepcopy(payload)
    unresolved: Dict[str, dict] = {}

    def swap(ref, _source, _item):
        if ref not in wall:
            return PLACEHOLDER + CODE_NOT_ON_WALL
        result = (resolved.get(ref) or Resolved(None, CODE_PENDING, MSG_PENDING)) if core_ok else Resolved(
            None, CODE_OLD_CORE, MSG_OLD_CORE)
        if result.url:
            return result.url
        unresolved.setdefault(ref, {"ref": ref, **cameras.get(ref, {}), "code": result.code,
                                    "message": result.message})
        return PLACEHOLDER + result.code

    _visit(out, swap)
    return out, cells, [unresolved[ref] for ref in sorted(unresolved)]


def fingerprint(refs: Iterable[str], resolved: Dict[str, Resolved], core_ok: bool = True) -> str:
    """sha256 over the sorted (reference, address or unresolved code) pairs."""
    pairs = []
    for ref in sorted(set(refs)):
        result = resolved.get(ref) if core_ok else None
        pairs.append([ref, (result.url or result.code) if result else (CODE_PENDING if core_ok else CODE_OLD_CORE)])
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def _usable(rec: Optional[dict], t: float) -> bool:
    if not rec or not rec.get("url"):
        return False
    return rec.get("expires_at") is None or rec["expires_at"] - t > EXPIRY_GUARD_SEC


def _address(rec: dict) -> Address:
    return Address(url=rec.get("url") or "", expires_at=rec.get("expires_at"), handle=rec.get("handle") or {},
                   info=rec.get("info") or {})


def _failed(rec: dict, provider_name: str = "") -> Resolved:
    code = ERROR_CODES.get(rec.get("last_error_code") or "", CODE_UNAVAILABLE)
    message = rec.get("last_error") or "The provider gave no address."
    return Resolved(None, code, f"{provider_name}: {message}" if provider_name else message)


def _same_path(a: str, b: str) -> bool:
    return bool(a) and a.split("?", 1)[0] == b.split("?", 1)[0]


# --------------------------------------------------------------------------- #
# Minting
# --------------------------------------------------------------------------- #


def _mint_one(entry: _Provider, camera_id: str, cfg: Dict[str, float], revive: bool = False) -> Resolved:
    """Mint (or renew) one address under the camera's lock and store it.

    ``revive``: the address stopped answering. When the provider gives the
    same address path back and the stored one is not due for renewal and
    answers again, the stored one is kept, so no station needs a delivery.
    When neither the stored nor the fresh address plays, the stored one is
    kept as well and the attempt counts as a failure (error text, backoff):
    a camera that is down at the site is not minted and delivered for ever.
    """
    key = _camera_key("addr", entry.id, camera_id)
    lock = _camera_key("mint", entry.id, camera_id)
    client = redis_client()
    owner = _lock(lock, MINT_LOCK_SEC)
    if not owner:
        # Another worker mints this camera: wait for its result.
        deadline = time.monotonic() + MINT_WAIT_SEC
        while time.monotonic() < deadline and client.exists(PREFIX + lock):
            time.sleep(0.05)
        rec = _load(key) or {}
        if _usable(rec, now()):
            return Resolved(rec["url"], OK, "")
        return _failed(rec, entry.name) if rec.get("last_error_code") else Resolved(None, CODE_PENDING, MSG_PENDING)
    try:
        rec = _load(key) or {}
        previous = _address(rec) if (rec.get("url") or rec.get("handle")) else None
        last_info = (_load(f"pinfo:{entry.id}") or {}).get("info")
        timeout = cfg["request_timeout_sec"]
        error: Optional[ProviderError] = None
        address: Optional[Address] = None
        keep = dead = False
        try:
            with httpx.Client(timeout=timeout) as http:
                address = entry.impl.mint(entry.settings, camera_id, previous, http, last_info)
                if revive:
                    held = bool(previous) and _usable(rec, now())
                    keep = (held and _same_path(previous.url, address.url)
                            and now() < (rec.get("renew_at") or 0) and _plays(entry, previous, http))
                    # The fresh address does not play either (camera or edge down): the stations
                    # keep the one they have, so a dead camera costs no delivery, and the backoff applies.
                    dead = not keep and not _plays(entry, address, http)
                    if dead and held:
                        address = previous
                        keep = True
        except ProviderError as exc:
            error = exc
        except Exception as exc:
            error = ProviderError(UNAVAILABLE, f"unexpected error ({type(exc).__name__})")
        t = now()
        if error is not None:
            failures = int(rec.get("failures") or 0) + 1
            rec.update(last_error=error.message, last_error_code=error.code, last_error_at=t, failures=failures,
                       not_playing=False,
                       next_try_at=t + min(BACKOFF_FIRST_SEC * 2 ** (failures - 1), BACKOFF_MAX_SEC))
            _save(key, rec, ADDR_TTL_SEC)
            logger.warning(f"Stream provider {entry.name}: no address for a camera ({error.code}: {error.message}); "
                           f"next try in {int(rec['next_try_at'] - t)} s")
            return Resolved(rec["url"], OK, "") if _usable(rec, t) else _failed(rec, entry.name)
        if not keep:
            rec.update(url=address.url, expires_at=address.expires_at, minted_at=t,
                       renew_at=renew_at(t, address.expires_at, cfg["renew_fraction"], cfg["renew_min_margin_sec"]))
        rec.update(handle=address.handle, info=address.info)
        if dead:
            failures = int(rec.get("failures") or 0) + 1
            rec.update(last_error=MSG_NOT_PLAYING, last_error_code=UNAVAILABLE, last_error_at=t, failures=failures,
                       not_playing=True,
                       next_try_at=t + min(BACKOFF_FIRST_SEC * 2 ** (failures - 1), BACKOFF_MAX_SEC))
            logger.warning(f"Stream provider {entry.name}: a camera's address does not play; "
                           f"next try in {int(rec['next_try_at'] - t)} s")
        else:
            rec.update(last_error=None, last_error_code=None, last_error_at=None, failures=0, next_try_at=None,
                       not_playing=False)
            _save(f"pinfo:{entry.id}", {"info": address.info, "at": t}, ADDR_TTL_SEC)
            client.delete(PREFIX + _camera_key("probe", entry.id, camera_id))
        _save(key, rec, ADDR_TTL_SEC)
        client.set(PREFIX + _camera_key("use", entry.id, camera_id), str(t), ex=ADDR_TTL_SEC)
        return Resolved(rec["url"], OK, "")
    finally:
        _unlock(lock, owner)


def _plays(entry: _Provider, address: Address, http) -> bool:
    try:
        return bool(entry.impl.probe(address, http))
    except Exception:  # a probe that raises is a probe that failed, never a failed mint
        return False


def _plays_again(entry: _Provider, camera_id: str) -> None:
    """An address that did not play answers again: drop that error."""
    key = _camera_key("addr", entry.id, camera_id)
    lock = _camera_key("mint", entry.id, camera_id)
    owner = _lock(lock, MINT_LOCK_SEC)
    if not owner:
        return  # a mint is running; the next check does it
    try:
        rec = _load(key)
        if rec and rec.get("not_playing"):
            rec.update(last_error=None, last_error_code=None, last_error_at=None, failures=0, next_try_at=None,
                       not_playing=False)
            _save(key, rec, ADDR_TTL_SEC)
    finally:
        _unlock(lock, owner)


def touch(ref: str) -> None:
    """Note that the address of ``ref`` is in use right now (the portal's
    live view calls this while it plays), so the tick does not give it back."""
    key = _ref_key("use", ref)
    if key and settings.STREAM_PROVIDERS_ENABLED:
        redis_client().set(PREFIX + key, str(now()), ex=ADDR_TTL_SEC)


def resolve_refs(db, refs: Iterable[str], *, budget_sec: Optional[float] = None, allow_mint: bool = True,
                 force: bool = False, cfg: Optional[Dict[str, float]] = None) -> Dict[str, Resolved]:
    """Addresses for references. A cached address costs no request; the rest
    is minted in parallel under one deadline (``budget_sec``), and what is not
    ready by then is ``pending`` (its mint finishes in the background and
    fills the cache). ``force`` renews addresses that are still good. A
    provider that failed is not asked again before its backoff ends."""
    refs = list(dict.fromkeys(refs))
    out: Dict[str, Resolved] = {}
    if not refs:
        return out
    if not settings.STREAM_PROVIDERS_ENABLED:
        return {ref: Resolved(None, CODE_UNAVAILABLE, MSG_DISABLED) for ref in refs}
    cfg = cfg or config(db)
    parsed = {ref: parse_provider_ref(ref) for ref in refs}
    providers = load_providers(db, [p[0] for p in parsed.values() if p])
    t = now()
    jobs: List[Tuple[str, _Provider, str, Optional[str]]] = []
    for ref in refs:
        if parsed[ref] is None:
            out[ref] = Resolved(None, CODE_UNAVAILABLE, "This is not a provider camera reference.")
            continue
        provider_id, camera_id = parsed[ref]
        entry = providers.get(provider_id)
        if entry is None:
            out[ref] = Resolved(None, CODE_REMOVED, MSG_REMOVED)
            continue
        if entry.error:
            out[ref] = Resolved(None, CODE_UNAVAILABLE, entry.error)
            continue
        rec = _load(_camera_key("addr", provider_id, camera_id)) or {}
        usable = _usable(rec, t)
        backoff = (rec.get("next_try_at") or 0) > t
        if usable and (backoff or not force):
            out[ref] = Resolved(rec["url"], OK, "")
        elif backoff:
            out[ref] = _failed(rec, entry.name)
        elif not allow_mint:
            out[ref] = Resolved(None, CODE_PENDING, MSG_PENDING)
        else:
            jobs.append((ref, entry, camera_id, rec["url"] if usable else None))
    if jobs:
        pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="sp-mint")
        futures = {pool.submit(_mint_one, entry, camera_id, cfg): (ref, entry, current)
                   for ref, entry, camera_id, current in jobs}
        done, _late = wait(futures, timeout=budget_sec)
        for future, (ref, entry, current) in futures.items():
            if future not in done:
                out[ref] = Resolved(current, OK, "") if current else Resolved(None, CODE_PENDING, MSG_PENDING)
                continue
            try:
                out[ref] = future.result()
            except Exception as exc:
                logger.error(f"Stream provider {entry.name}: mint failed ({type(exc).__name__})")
                out[ref] = Resolved(current, OK, "") if current else Resolved(
                    None, CODE_UNAVAILABLE, f"{entry.name}: the address cache is not available.")
        pool.shutdown(wait=False)
    return out


def peek(ref: str) -> Optional[str]:
    """The cached address of a reference, or None. Never mints (snapshots
    must not open a vendor session)."""
    key = _ref_key("addr", ref)
    if key is None or not settings.STREAM_PROVIDERS_ENABLED:
        return None
    try:
        rec = _load(key)
    except Exception:
        return None
    return rec["url"] if _usable(rec, now()) else None


def check_provider(db, provider_id: str, cfg: Optional[Dict[str, float]] = None) -> str:
    """The provider's "test these settings". Raises ProviderError."""
    entry = load_providers(db, [provider_id]).get(provider_id)
    if entry is None:
        raise ProviderError(INVALID, MSG_REMOVED)
    if entry.error:
        raise ProviderError(INVALID, entry.error)
    cfg = cfg or config(db)
    try:
        with httpx.Client(timeout=cfg["request_timeout_sec"]) as http:
            return entry.impl.check(entry.settings, http)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(UNAVAILABLE, f"unexpected error ({type(exc).__name__})") from None


def forget_provider(db_entry: Optional[_Provider], provider_id: str, cfg: Dict[str, float]) -> int:
    """Drop every cached address of a provider (its settings changed or it
    was removed), releasing them when the provider is still known."""
    client = redis_client()
    removed = 0
    for raw in client.keys(f"{PREFIX}addr:{provider_id}:*"):
        key = raw.decode("utf-8")[len(PREFIX):]
        camera_id = unquote(key.split(":", 2)[2])
        rec = _load(key)
        if db_entry is not None and db_entry.impl is not None and not db_entry.error and rec and rec.get("url"):
            _release(db_entry, camera_id, rec, cfg)
        client.delete(PREFIX + key, PREFIX + _camera_key("use", provider_id, camera_id),
                      PREFIX + _camera_key("probe", provider_id, camera_id))
        removed += 1
    client.delete(f"{PREFIX}pinfo:{provider_id}")
    return removed


def _release(entry: _Provider, camera_id: str, rec: dict, cfg: Dict[str, float]) -> None:
    try:
        with httpx.Client(timeout=cfg["request_timeout_sec"]) as http:
            entry.impl.release(entry.settings, camera_id, _address(rec), http)
    except Exception as exc:
        logger.info(f"Stream provider {entry.name}: release not confirmed ({type(exc).__name__})")


# --------------------------------------------------------------------------- #
# The server-side check of addresses in use
# --------------------------------------------------------------------------- #


def _probe_one(entry: _Provider, camera_id: str, cfg: Dict[str, float], immediate: bool) -> None:
    rec = _load(_camera_key("addr", entry.id, camera_id))
    if not rec or not rec.get("url"):
        return
    with httpx.Client(timeout=cfg["request_timeout_sec"]) as http:
        healthy = _plays(entry, _address(rec), http)
    t = now()
    probe_key = _camera_key("probe", entry.id, camera_id)
    state = _load(probe_key) or {}
    state["at"] = t
    state["failures"] = 0 if healthy else int(state.get("failures") or 0) + 1
    if healthy:
        state["ok_at"] = t
    _save(probe_key, state, ADDR_TTL_SEC)
    if healthy and rec.get("not_playing"):
        _plays_again(entry, camera_id)
    if healthy or not (immediate or state["failures"] >= 2) or (rec.get("next_try_at") or 0) > t:
        return
    logger.info(f"Stream provider {entry.name}: an address in use stopped answering; asking for it again")
    _mint_one(entry, camera_id, cfg, revive=True)


def check_addresses(db, refs: Iterable[str], cfg: Dict[str, float], immediate: bool = False,
                    skip_recent: bool = False) -> int:
    """Probe cached addresses (each at most once per ``watch_interval_sec``;
    with ``immediate`` now, and with ``skip_recent`` not when it answered
    within the last IMMEDIATE_PROBE_MIN_SEC). One that fails twice in a row (once when
    ``immediate``) is minted again with its kept handle."""
    parsed = [p for p in (parse_provider_ref(ref) for ref in dict.fromkeys(refs)) if p]
    providers = load_providers(db, [p[0] for p in parsed])
    t = now()
    jobs = []
    for provider_id, camera_id in parsed:
        entry = providers.get(provider_id)
        if entry is None or entry.error:
            continue
        state = _load(_camera_key("probe", provider_id, camera_id)) or {}
        age = t - (state.get("at") or 0)
        if not immediate and age < cfg["watch_interval_sec"]:
            continue
        # Stations share addresses: when many (re)connect at once, or one flaps, an address that
        # just answered is not asked again for each of them.
        if skip_recent and age < IMMEDIATE_PROBE_MIN_SEC and not state.get("failures"):
            continue
        jobs.append((entry, camera_id))
    if jobs:
        with ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="sp-probe") as pool:
            list(pool.map(lambda job: _probe_one(job[0], job[1], cfg, immediate), jobs))
    return len(jobs)


# --------------------------------------------------------------------------- #
# Per PC: what was deployed and what was sent
# --------------------------------------------------------------------------- #


def note_deployed(pc_id: str, config_id: str, config_hash: Optional[str], unresolved_payload: dict,
                  sent_fingerprint: Optional[str]) -> None:
    """Keep the layout a deploy just delivered (references unresolved), so a
    renewal sends exactly what the station runs."""
    cells, cameras = layout_index(unresolved_payload)
    if not cameras:
        _save(f"pc:{pc_id}", {"config_id": config_id, "none": True})
        return
    _save(f"pc:{pc_id}", {
        "config_id": config_id, "config_hash": config_hash, "layout": unresolved_payload, "refs": cells,
        "cameras": cameras, "sent_id": config_id if sent_fingerprint else None,
        "sent_fingerprint": sent_fingerprint, "sent_at": now() if sent_fingerprint else None, "ack_retries": 0,
    })


def held_deployment(db, pc_id: str):
    """The newest deploy the station got (delivered, received, accepted or
    applied at some time, and not refused by Core), or None. A newer deploy
    that never reached the station (offline, relay_failed) does not count:
    the station still runs the older one, and that is what must be renewed."""
    from sqlalchemy import or_

    from app.models.pc_tracking import PcDeployment as Row
    from app.models.pc_tracking import latest_deployment_order

    return (db.query(Row)
            .filter(Row.pc_id == pc_id, Row.status != "failed",
                    or_(Row.delivered_at.isnot(None), Row.received_at.isnot(None), Row.accepted_at.isnot(None),
                        Row.applied_at.isnot(None)))
            .order_by(*latest_deployment_order()).first())  # not superseded first, then the newest


def _rebuild_pc(db, pc_id: str, held) -> Optional[dict]:
    """The record of the deploy the station holds, regenerated from the
    database. None when the layout cannot be built."""
    from app.services import deploy_tracking
    from app.services.config_generator import generate_config
    from app.services.config_loader import load_pc_config

    key = f"pc:{pc_id}"
    try:
        generated = generate_config(load_pc_config(pc_id, db), db)
    except Exception as exc:
        logger.error(f"Stream providers: the layout of PC {pc_id} could not be regenerated ({type(exc).__name__})")
        return None
    payload = dict(generated)
    if settings.DEPLOY_INJECT_CONFIG_ID:
        payload["config_id"] = held.id
    else:
        payload.pop("config_id", None)
    if not layout_index(payload)[1]:
        rec = {"config_id": held.id, "none": True}
    elif deploy_tracking.config_hash(generated) != held.config_hash:
        rec = {"config_id": held.id, "blocked": MSG_BLOCKED}
        logger.warning(f"Stream providers: the layout of PC {pc_id} changed since deploy {held.id}; its "
                       f"provider cameras are not renewed until it is deployed")
    else:
        current = _load(key)
        if current and current.get("config_id") == held.id:
            return current  # the deploy wrote it meanwhile
        note_deployed(pc_id, held.id, held.config_hash, payload, None)
        return _load(key)
    _save(key, rec)
    return rec


def pc_record(db, pc_id: str) -> Optional[dict]:
    """The PC's record for the deploy it holds (rebuilt when missing or stale)."""
    held = held_deployment(db, pc_id)
    if held is None:
        return None
    rec = _load(f"pc:{pc_id}")
    if rec is None or rec.get("config_id") != held.id:
        rec = _rebuild_pc(db, pc_id, held)
    return rec


def _pc_and_health(db, pc_id: str):
    from app.models.pc import PC
    from app.models.pc_tracking import PcHealth

    return (db.query(PC).filter(PC.id == pc_id).first(),
            db.query(PcHealth).filter(PcHealth.pc_id == pc_id).first())


def refresh_pc(db, pc_id: str, reason: str, sender=None, *, cfg: Optional[Dict[str, float]] = None,
               check_refs: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Send the PC its deployed layout with the current addresses when they
    differ from what it acknowledged. Not a deploy: the same root
    ``config_id``, no pc_deployments row, no audit entry (relay event
    ``refresh_config``).

    ``reason``: ``deploy`` (the follow-up of a deploy that left cells
    pending; not rate-limited), ``connect`` and ``failure`` (the PC's
    addresses, or ``check_refs``, are probed first), ``tick``.
    """
    if not settings.STREAM_PROVIDERS_ENABLED:
        return {"status": "disabled"}
    rec = pc_record(db, pc_id)
    if rec is None:  # nothing reached this station yet (or its layout cannot be built)
        return {"status": "no_deploy"}
    if rec.get("none"):
        return {"status": "no_provider_cameras"}
    if rec.get("blocked"):
        return {"status": "blocked"}
    cfg = cfg or config(db)
    pc, health = _pc_and_health(db, pc_id)
    if pc is None:
        redis_client().delete(f"{PREFIX}pc:{pc_id}")
        return {"status": "no_pc"}
    core_ok = core_supports_hls(pc, health)
    refs = _record_refs(rec)
    if core_ok and reason in ("connect", "failure"):
        # A station's failure report is always checked; a connect check trusts a fresh answer.
        check_addresses(db, refs if check_refs is None else check_refs, cfg, immediate=True,
                        skip_recent=reason == "connect")
    resolved = resolve_refs(db, refs, budget_sec=REFRESH_BUDGET_SEC, cfg=cfg) if core_ok else {}
    current = fingerprint(refs, resolved, core_ok)
    # Probing and resolving took seconds. From here to the saved record the PC's send lock is
    # held (a deploy holds it too) and the record is read again: a deploy that came meanwhile
    # is never followed by the layout it replaced, and two refreshes never both deliver.
    lock = f"lock:pc:{pc_id}"
    owner = _lock(lock, PC_LOCK_SEC)
    if not owner:
        return {"status": "busy"}
    try:
        config_id = rec.get("config_id")
        rec = pc_record(db, pc_id)
        if not rec or rec.get("config_id") != config_id or rec.get("none") or rec.get("blocked"):
            return {"status": "superseded"}
        return _send_refresh(pc_id, reason, sender, cfg, rec, refs, resolved, core_ok, current)
    finally:
        _unlock(lock, owner)


def _send_refresh(pc_id: str, reason: str, sender, cfg: Dict[str, float], rec: dict, refs: List[str],
                  resolved: Dict[str, Resolved], core_ok: bool, current: str) -> Dict[str, Any]:
    from app.services import deploy_service

    payload, _cells, unresolved = resolve_layout(rec["layout"], resolved, core_ok)
    same = current == rec.get("sent_fingerprint")
    if same and rec.get("sent_id") and _get_text(f"ack:{pc_id}") == rec["sent_id"]:
        return {"status": "current"}
    retries = int(rec.get("ack_retries") or 0)
    if same and retries >= ACK_RETRIES:
        return {"status": "unacknowledged"}
    t = now()
    if reason != "deploy" and rec.get("sent_at") and t - rec["sent_at"] < cfg["pc_refresh_min_interval_sec"]:
        return {"status": "rate_limited"}
    refresh_id = str(uuid.uuid4())
    try:
        answers = (sender or deploy_service.refresh_sender)(
            [{"refresh_id": refresh_id, "targetId": pc_id, "content": payload}])
    except deploy_service.RelayUnreachable:
        return {"status": "relay_unreachable"}
    answer = answers.get(refresh_id)
    status = answer.get("status") if isinstance(answer, dict) else None
    if status == deploy_service.DELIVERY_DELIVERED:
        rec.update(sent_id=refresh_id, sent_fingerprint=current, sent_at=t, ack_retries=retries + 1 if same else 0)
        _save(f"pc:{pc_id}", rec)
    logger.info(f"Stream addresses for PC {pc_id} ({reason}): {status or 'no answer'}; "
                f"{len(refs) - len(unresolved)} of {len(refs)} camera(s) playable")
    return {"status": status or "no_answer", "refresh_id": refresh_id, "cameras": len(refs),
            "unresolved": len(unresolved)}


def station_failed(db, pc_id: str, source_ids: Iterable[str], sender=None) -> Dict[str, Any]:
    """A station reported failed cells: check the provider cameras among
    them and deliver if an address changed."""
    rec = _load(f"pc:{pc_id}") if settings.STREAM_PROVIDERS_ENABLED else None
    cells = (rec or {}).get("refs") or {}
    refs = sorted({cells[str(sid)] for sid in source_ids if str(sid) in cells})
    if not refs:
        return {"status": "no_provider_cameras"}
    return refresh_pc(db, pc_id, "failure", sender, check_refs=refs)


# --------------------------------------------------------------------------- #
# The periodic tick (Celery beat, every 30 s)
# --------------------------------------------------------------------------- #


def connected_pcs() -> Optional[List[str]]:
    """PC ids the relay holds a session for; None when it cannot be asked."""
    from app.services.deploy_service import relay_url

    try:
        response = httpx.get(relay_url() + "/api/connected-pcs", timeout=2.0)
        response.raise_for_status()
        return [str(item["pc_id"]) for item in response.json().get("connected_pcs", [])]
    except Exception as exc:
        logger.warning(f"Stream providers: the relay's connected list is not available ({type(exc).__name__})")
        return None


def tick(db, sender=None) -> Dict[str, Any]:
    if not settings.STREAM_PROVIDERS_ENABLED:
        return {"status": "disabled"}
    owner = _lock("tick", TICK_LOCK_SEC)
    if not owner:
        return {"status": "busy"}
    try:
        return _tick(db, sender)
    finally:
        _unlock("tick", owner)


def _tick(db, sender) -> Dict[str, Any]:
    from app.models.camera import Camera
    from app.models.pc import PC
    from app.models.pc_tracking import PcHealth

    client = redis_client()
    addr_keys = [raw.decode("utf-8")[len(PREFIX):] for raw in client.keys(PREFIX + "addr:*")]
    if (not addr_keys and not client.keys(PREFIX + "pc:*")
            and db.query(Camera.id).filter(Camera.rtsp_url.like("provider://%")).first() is None):
        return {"status": "idle"}
    connected = connected_pcs()
    if connected is None:
        return {"status": "skipped", "reason": "relay"}
    cfg = config(db)
    t = now()

    # a, b. What connected stations that can play it have deployed.
    pcs = {pc.id: pc for pc in db.query(PC).filter(PC.id.in_(connected)).all()} if connected else {}
    healths = ({h.pc_id: h for h in db.query(PcHealth).filter(PcHealth.pc_id.in_(list(pcs))).all()} if pcs else {})
    records: Dict[str, dict] = {}
    in_use: set = set()
    for pc_id in sorted(pcs):
        rec = pc_record(db, pc_id)
        if not rec or rec.get("none") or rec.get("blocked"):
            continue
        records[pc_id] = rec
        if core_supports_hls(pcs[pc_id], healths.get(pc_id)):
            in_use.update(_record_refs(rec))
    for ref in in_use:
        client.set(PREFIX + _ref_key("use", ref), str(t), ex=ADDR_TTL_SEC)

    # c. Renew all in-use addresses of a provider when its first one is due,
    #    so a station gets one delivery per cycle, not one per camera.
    by_provider: Dict[str, List[Tuple[str, dict]]] = {}
    for ref in in_use:
        rec = _load(_ref_key("addr", ref))
        if rec and rec.get("url"):
            by_provider.setdefault(parse_provider_ref(ref)[0], []).append((ref, rec))
    renewed = 0
    for provider_id, items in by_provider.items():
        if any(t >= (rec.get("renew_at") or 0) for _ref, rec in items):
            resolve_refs(db, [ref for ref, _rec in items], budget_sec=RENEW_BUDGET_SEC, force=True, cfg=cfg)
            renewed += len(items)

    # d. The server-side check (replaces failure reports for Client 1.2.0).
    probed = check_addresses(db, sorted(in_use), cfg) if cfg["watch_interval_sec"] > 0 else 0

    # e. Deliver to stations whose addresses or acknowledgement are behind.
    delivered = 0
    for pc_id in records:
        try:
            if refresh_pc(db, pc_id, "tick", sender, cfg=cfg).get("status") == "delivered":
                delivered += 1
        except Exception as exc:
            logger.error(f"Stream providers: refresh of PC {pc_id} failed ({type(exc).__name__})")

    # f. Give back addresses nobody used for a while. An address on the wall of a station that
    #    is not connected right now is kept while it is valid: the station may still play it
    #    (it lost only its link to the relay), and it gets the same address back when it returns.
    held = set(in_use)
    for raw in client.keys(PREFIX + "pc:*"):
        pc_id = raw.decode("utf-8")[len(PREFIX) + 3:]
        if pc_id not in records:
            held.update(_record_refs(_load(f"pc:{pc_id}") or {}))
    released = 0
    stale = []
    for key in addr_keys:
        _kind, provider_id, quoted = key.split(":", 2)
        camera_id = unquote(quoted)
        ref = make_provider_ref(provider_id, camera_id)
        if ref in in_use:
            continue
        rec = _load(key) or {}
        if ref in held and _usable(rec, t):
            continue
        used = max(float(_get_text(_camera_key("use", provider_id, camera_id)) or 0), rec.get("minted_at") or 0,
                   rec.get("last_error_at") or 0)
        if t - used >= cfg["release_after_sec"]:
            stale.append((provider_id, camera_id, key, rec))
    providers = load_providers(db, [item[0] for item in stale])
    for provider_id, camera_id, key, rec in stale:
        entry = providers.get(provider_id)
        if entry is not None and entry.impl is not None and not entry.error and rec.get("url"):
            _release(entry, camera_id, rec, cfg)
        client.delete(PREFIX + key, PREFIX + _camera_key("use", provider_id, camera_id),
                      PREFIX + _camera_key("probe", provider_id, camera_id))
        released += 1
    return {"status": "ok", "connected": len(connected), "stations": len(records), "in_use": len(in_use),
            "renewed": renewed, "probed": probed, "delivered": delivered, "released": released}


# --------------------------------------------------------------------------- #
# Status for the portal (non-secret fields only)
# --------------------------------------------------------------------------- #


def _camera_status(ref: str, t: float) -> dict:
    key = _ref_key("addr", ref)
    rec = _load(key) if key else None
    if not rec:
        return {"state": "unused", "message": None, "valid_until": None, "renewed_at": None, "last_error_at": None,
                "code": None}
    usable = _usable(rec, t)
    error = rec.get("last_error")
    if not usable:
        state = "error" if error else "unused"
    elif error:
        state = "expiring" if rec.get("expires_at") and rec["expires_at"] - t <= EXPIRING_SEC else "error"
    else:
        state = "ok"
    return {
        "state": state,
        "message": error,
        "code": ERROR_CODES.get(rec.get("last_error_code") or "") if error else None,
        "valid_until": rec.get("expires_at") if usable else None,
        "renewed_at": rec.get("minted_at") if usable else None,
        "last_error_at": rec.get("last_error_at"),
    }


def status_for_cameras(refs: Iterable[str]) -> Dict[str, dict]:
    """``{reference: {state: ok|error|expiring|unused, message, code,
    valid_until, renewed_at, last_error_at}}``; empty when the cache cannot be
    read (a listing must not fail because Redis is down)."""
    try:
        t = now()
        return {ref: _camera_status(ref, t) for ref in dict.fromkeys(refs)}
    except Exception as exc:
        logger.warning(f"Stream provider status not available ({type(exc).__name__})")
        return {}


def provider_status(provider_id: str) -> dict:
    """One line for the Stream providers page."""
    out = {"state": "unused", "message": None, "code": None, "sessions": None, "session_limit": None,
           "checked_at": None, "addresses": 0}
    try:
        t = now()
        info = _load(f"pinfo:{provider_id}") or {}
        out["sessions"] = (info.get("info") or {}).get("sessions")
        out["session_limit"] = (info.get("info") or {}).get("limit")
        out["checked_at"] = info.get("at")
        rank = {"unused": 0, "ok": 1, "error": 2, "expiring": 3}
        for raw in redis_client().keys(f"{PREFIX}addr:{provider_id}:*"):
            _kind, _pid, quoted = raw.decode("utf-8")[len(PREFIX):].split(":", 2)
            status = _camera_status(make_provider_ref(provider_id, unquote(quoted)), t)
            out["addresses"] += 1 if status["valid_until"] or status["state"] == "ok" else 0
            if rank[status["state"]] > rank[out["state"]]:
                out["state"], out["message"], out["code"] = status["state"], status["message"], status["code"]
    except Exception as exc:
        logger.warning(f"Stream provider status not available ({type(exc).__name__})")
    return out


def status_for_pc(db, pc_id: str) -> Optional[dict]:
    """The PC health panel's "Provider cameras" block, or None when the PC's
    latest deploy has no provider camera (or nothing is known)."""
    try:
        rec = _load(f"pc:{pc_id}")
        held = held_deployment(db, pc_id)
        if not rec or rec.get("none") or held is None or rec.get("config_id") != held.id:
            return None
        out = {"cameras": 0, "unresolved": [], "last_delivery_at": None, "acknowledged": False,
               "next_renewal_at": None, "blocked": rec.get("blocked")}
        if rec.get("blocked"):
            return out
        cameras = {ref: (rec.get("cameras") or {}).get(ref) or {} for ref in _record_refs(rec)}  # wall cells
        pc, health = _pc_and_health(db, pc_id)
        core_ok = core_supports_hls(pc, health)
        t = now()
        providers = load_providers(db, [parse_provider_ref(ref)[0] for ref in cameras])
        renewals = []
        for ref in sorted(cameras):
            entry = providers.get(parse_provider_ref(ref)[0])
            status = _camera_status(ref, t)
            addr = _load(_ref_key("addr", ref)) or {}
            if not core_ok:
                code, message = CODE_OLD_CORE, MSG_OLD_CORE
            elif entry is None:
                code, message = CODE_REMOVED, MSG_REMOVED
            elif entry.error:
                code, message = CODE_UNAVAILABLE, entry.error
            elif status["valid_until"] or (status["state"] == "ok"):
                if addr.get("renew_at"):
                    renewals.append(addr["renew_at"])
                continue
            elif status["message"]:
                code, message = status["code"] or CODE_UNAVAILABLE, f"{entry.name}: {status['message']}"
            else:
                code, message = CODE_PENDING, MSG_PENDING
            out["unresolved"].append({"camera_id": cameras[ref].get("camera_id"),
                                      "camera_name": cameras[ref].get("camera_name"), "code": code,
                                      "message": message})
        out["cameras"] = len(cameras)
        out["last_delivery_at"] = rec.get("sent_at")
        out["acknowledged"] = bool(rec.get("sent_id")) and _get_text(f"ack:{pc_id}") == rec.get("sent_id")
        out["next_renewal_at"] = min(renewals) if renewals else None
        return out
    except Exception as exc:
        logger.warning(f"Stream provider status of PC {pc_id} not available ({type(exc).__name__})")
        return None
