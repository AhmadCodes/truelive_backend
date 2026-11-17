"""
Deploy state machine of ``pc_deployments`` (plan 06 D14, D15; OD-18).

Both writers of a deploy row use this module: the API (D14: pending, then
delivered / offline / relay_failed from the relay's answer) and the relay
(D14: received when the Client ACKs the ``config`` message; D15: accepted,
applied, failed, unconfirmed and superseded from station reports and the
timeout sweep). They run in different processes and race, so every state
change is **one conditional UPDATE**: the row moves to ``to`` only when its
current status is in ``ALLOWED_FROM[to]``. Whoever commits second can never
move a row backwards (a relay that runs ahead of the API's own ``delivered``
write keeps the row at ``accepted``).

Ranks (the order the station walks)::

    pending / relay_failed (0) < delivered (1) < received (2) < accepted (3)
        < applied (4) ; failed is terminal ; offline and superseded are terminal

Rules:

* ``relay_failed`` means "the relay call failed or never answered". It is not
  evidence that the PC did not get the layout, so any later evidence
  (delivered, received, accepted, applied, failed) upgrades it.
* ``offline`` means the relay had no session for the PC and forwarded
  nothing: terminal.
* ``unconfirmed`` (D15 sweep) is upgraded by late Core evidence (accepted,
  applied, failed), never by a late Socket.IO ACK (that only stamps
  ``received_at``, which D17 reads as "it was received").
* ``applied`` can still become ``failed`` (a late Failed, OD-18 (b)).
* ``failed`` and ``superseded`` are terminal.
* A newer deploy to the same PC supersedes the older rows that are still in
  flight (pending, delivered, received of a confirmable row, accepted). A
  non-confirmable ``received`` row (Client 1.1.x) has reached its end state
  and is kept as history, like applied, failed, offline, relay_failed and
  unconfirmed rows.

When the status cannot move, evidence still counts: ``delivered_at``,
``received_at`` and ``accepted_at`` are stamped once (COALESCE) on any
non-superseded row, so history keeps "it was received" even when the ACK
lands after ``accepted``.

``PendingTransitions`` keeps an event whose row does not exist yet (or whose
write hit a database error) and retries it until it applies or expires, so an
event that arrives early is not lost.

Timestamps are Unix seconds (BigInteger), like ``pcs.last_applied``.
"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy import and_, func, not_, select, update

from app.models.pc import PC
from app.models.pc_tracking import DEPLOYMENT_STATUSES, PcDeployment

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The transition table
# --------------------------------------------------------------------------- #

PENDING = "pending"
DELIVERED = "delivered"
RECEIVED = "received"
ACCEPTED = "accepted"
APPLIED = "applied"
FAILED = "failed"
UNCONFIRMED = "unconfirmed"
SUPERSEDED = "superseded"
OFFLINE = "offline"
RELAY_FAILED = "relay_failed"

# Allowed source states of each target state. Anything else is a no-op.
ALLOWED_FROM: Dict[str, FrozenSet[str]] = {
    DELIVERED: frozenset({PENDING, RELAY_FAILED}),
    OFFLINE: frozenset({PENDING}),
    RELAY_FAILED: frozenset({PENDING}),
    RECEIVED: frozenset({PENDING, DELIVERED, RELAY_FAILED}),
    ACCEPTED: frozenset({PENDING, DELIVERED, RECEIVED, RELAY_FAILED, UNCONFIRMED}),
    APPLIED: frozenset({PENDING, DELIVERED, RECEIVED, ACCEPTED, RELAY_FAILED, UNCONFIRMED}),
    FAILED: frozenset({PENDING, DELIVERED, RECEIVED, ACCEPTED, APPLIED, RELAY_FAILED, UNCONFIRMED}),
    UNCONFIRMED: frozenset({DELIVERED, RECEIVED, ACCEPTED}),
    SUPERSEDED: frozenset({PENDING, DELIVERED, RECEIVED, ACCEPTED}),
}

# Terminal states: nothing moves out of them (failed, offline, superseded).
TERMINAL_STATES: FrozenSet[str] = frozenset(
    s for s in DEPLOYMENT_STATUSES if not any(s in sources for sources in ALLOWED_FROM.values())
)

# States whose timestamp is evidence worth keeping even when the status
# cannot move (see the module docstring).
EVIDENCE_STATES: FrozenSet[str] = frozenset({DELIVERED, RECEIVED, ACCEPTED})

# The epoch column each state stamps.
STATE_TIMESTAMP: Dict[str, str] = {
    DELIVERED: "delivered_at",
    RECEIVED: "received_at",
    ACCEPTED: "accepted_at",
    APPLIED: "applied_at",
    FAILED: "failed_at",
    UNCONFIRMED: "unconfirmed_at",
    SUPERSEDED: "superseded_at",
}

# Rows a newer deploy to the same PC supersedes (see the module docstring).
SUPERSEDABLE_STATES: FrozenSet[str] = ALLOWED_FROM[SUPERSEDED]

# Columns a transition may set besides status and timestamps.
SETTABLE_FIELDS: FrozenSet[str] = frozenset(
    {"confirmable", "mode", "used_fallback", "persisted", "source", "client_version", "core_version"}
)

MAX_DETAIL = 500


def can_transition(current: str, to: str) -> bool:
    """True when a row in ``current`` may move to ``to``."""
    return current in ALLOWED_FROM.get(to, frozenset())


def now_ts() -> int:
    return int(time.time())


# --------------------------------------------------------------------------- #
# Payload identity
# --------------------------------------------------------------------------- #


def config_hash(config: Mapping[str, Any]) -> str:
    """sha256 hex of the layout content: the generated payload in canonical
    JSON (sorted keys, no whitespace), **without** the root ``config_id``, so
    two deploys of the same layout share a hash and differ only in config_id."""
    body = {k: v for k, v in config.items() if k != "config_id"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def screen_count(config: Mapping[str, Any]) -> int:
    screens = config.get("screens")
    return len(screens) if isinstance(screens, list) else 0


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


@dataclass
class TransitionResult:
    """Outcome of :func:`transition`.

    ``outcome`` is ``changed`` (the row moved), ``stale`` (the row exists but
    its state does not allow the move), ``mismatch`` (the row belongs to
    another PC) or ``missing`` (no such row yet).
    """

    outcome: str
    status: Optional[str] = None  # the row's status after the call (None when missing)
    previous: Optional[str] = None  # the status the row had before a change

    @property
    def changed(self) -> bool:
        return self.outcome == "changed"


def _detail(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return str(text)[:MAX_DETAIL]


def transition(
    db,
    config_id: str,
    to: str,
    *,
    ts: Optional[int] = None,
    pc_id: Optional[str] = None,
    detail: Optional[str] = None,
    fields: Optional[Mapping[str, Any]] = None,
    stamps: Optional[Mapping[str, Optional[int]]] = None,
) -> TransitionResult:
    """Move deploy ``config_id`` to ``to`` with one conditional UPDATE.

    * ``pc_id``: when given, the row must belong to this PC (a report from
      another PC's session never moves a row, D15).
    * ``detail``: the new ``status_detail`` (truncated to 500 characters).
    * ``fields``: other columns to set with the move (``SETTABLE_FIELDS``).
    * ``stamps``: extra epoch columns set once with the move, e.g.
      ``{"delivered_at": t}`` from the relay that forwarded the layout.

    Does not commit. Returns a :class:`TransitionResult`.
    """
    if to not in ALLOWED_FROM:
        raise ValueError(f"unknown target state {to!r}")
    ts = now_ts() if ts is None else int(ts)
    values: Dict[str, Any] = {"status": to, "status_changed_at": ts}
    column = STATE_TIMESTAMP.get(to)
    if column:
        values[column] = func.coalesce(getattr(PcDeployment, column), ts)
    if detail is not None:
        values["status_detail"] = _detail(detail)
    for key, value in (fields or {}).items():
        if key not in SETTABLE_FIELDS:
            raise ValueError(f"field {key!r} cannot be set by a transition")
        values[key] = value
    for key, value in (stamps or {}).items():
        if key not in STATE_TIMESTAMP.values() or value is None:
            continue
        values[key] = func.coalesce(getattr(PcDeployment, key), int(value))

    conditions = [PcDeployment.id == config_id, PcDeployment.status.in_(sorted(ALLOWED_FROM[to]))]
    if pc_id is not None:
        conditions.append(PcDeployment.pc_id == pc_id)
    row = db.execute(select(PcDeployment.status, PcDeployment.pc_id).where(PcDeployment.id == config_id)).first()
    if row is None:
        return TransitionResult("missing")
    if pc_id is not None and row.pc_id != pc_id:
        return TransitionResult("mismatch", status=row.status)
    result = db.execute(
        update(PcDeployment).where(and_(*conditions)).values(**values).execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return TransitionResult("changed", status=to, previous=row.status)

    # The row did not move: re-read (another writer may have just moved it)
    # and keep evidence timestamps.
    current = db.execute(select(PcDeployment.status).where(PcDeployment.id == config_id)).scalar()
    if to in EVIDENCE_STATES and current is not None and current != SUPERSEDED:
        evidence = {STATE_TIMESTAMP[to]: func.coalesce(getattr(PcDeployment, STATE_TIMESTAMP[to]), ts)}
        for key, value in (stamps or {}).items():
            if key in STATE_TIMESTAMP.values() and value is not None:
                evidence[key] = func.coalesce(getattr(PcDeployment, key), int(value))
        db.execute(
            update(PcDeployment)
            .where(PcDeployment.id == config_id, PcDeployment.status != SUPERSEDED)
            .values(**evidence)
            .execution_options(synchronize_session=False)
        )
    return TransitionResult("stale", status=current)


def set_confirmable(db, config_id: str, confirmable: bool) -> None:
    """Record whether the station can confirm this deploy (decided by the
    relay when it forwards it). A fact, so it is set whatever the status."""
    db.execute(
        update(PcDeployment)
        .where(PcDeployment.id == config_id, PcDeployment.confirmable != bool(confirmable))
        .values(confirmable=bool(confirmable))
        .execution_options(synchronize_session=False)
    )


def supersede_older(db, pc_id: str, newer_id: str, *, ts: Optional[int] = None) -> int:
    """Mark the PC's older in-flight rows superseded by ``newer_id``. Returns
    how many rows moved. A non-confirmable ``received`` row is kept."""
    ts = now_ts() if ts is None else int(ts)
    result = db.execute(
        update(PcDeployment)
        .where(
            PcDeployment.pc_id == pc_id,
            PcDeployment.id != newer_id,
            PcDeployment.status.in_(sorted(SUPERSEDABLE_STATES)),
            not_(and_(PcDeployment.status == RECEIVED, PcDeployment.confirmable == False)),  # noqa: E712
        )
        .values(
            status=SUPERSEDED,
            status_changed_at=ts,
            superseded_at=func.coalesce(PcDeployment.superseded_at, ts),
            status_detail=_detail(f"superseded by {newer_id}"),
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def stamp_last_applied(db, pc_id: str, ts: Optional[int] = None) -> None:
    """``pcs.last_applied`` (D14): stamped when a Client 1.1.x row is
    received, or when a confirmable row is applied (D15). Never at emit time.

    A status stamp, not an edit of the PC: ``updated_at`` is set to itself so
    its onupdate default does not fire (NEW-52; the portal shows updated_at
    next to updated_by as the PC's provenance)."""
    ts = now_ts() if ts is None else int(ts)
    table = PC.__table__
    db.execute(update(table).where(table.c.id == pc_id).values(last_applied=ts, updated_at=table.c.updated_at))


def statuses(db, config_ids: Iterable[str]) -> Dict[str, str]:
    """``{config_id: status}`` for the rows that exist."""
    ids = list(config_ids)
    if not ids:
        return {}
    rows = db.execute(select(PcDeployment.id, PcDeployment.status).where(PcDeployment.id.in_(ids))).all()
    return {row.id: row.status for row in rows}


def record_receipt(db, config_id: str, pc_id: str, *, confirmable: bool, ts: Optional[int] = None,
                   forwarded_at: Optional[int] = None) -> TransitionResult:
    """The Client ACKed the ``config`` message (D14). Moves the row to
    ``received`` and, for a non-confirmable row (Client 1.1.x, whose rows end
    here), stamps ``pcs.last_applied``. Does not commit."""
    ts = now_ts() if ts is None else int(ts)
    result = transition(
        db, config_id, RECEIVED, ts=ts, pc_id=pc_id,
        fields={"confirmable": bool(confirmable)},
        stamps={"delivered_at": forwarded_at},
    )
    if result.changed and not confirmable:
        stamp_last_applied(db, pc_id, ts)
    return result


# --------------------------------------------------------------------------- #
# Timeout sweep (D15)
# --------------------------------------------------------------------------- #

PENDING_TIMEOUT_SEC = 60


def sweep_timeouts(db, *, now: Optional[int] = None, ack_timeout: int = 300) -> Dict[str, int]:
    """The relay's timeout sweep (D15), as conditional bulk UPDATEs:

    * pending for more than 60 s: relay_failed (the API never recorded a relay
      answer);
    * confirmable rows delivered or received for more than ``ack_timeout``
      (DEPLOY_ACK_TIMEOUT_SEC) since delivery: unconfirmed;
    * confirmable rows accepted for more than twice that since acceptance:
      unconfirmed;
    * non-confirmable rows (Client 1.1.x) are left alone: they end at received.

    Late evidence still upgrades the result afterwards (ALLOWED_FROM). Does not
    commit. Returns the number of rows each rule moved.
    """
    now = now_ts() if now is None else int(now)
    ack_timeout = int(ack_timeout)
    counts: Dict[str, int] = {}

    result = db.execute(
        update(PcDeployment)
        .where(PcDeployment.status == PENDING, PcDeployment.status_changed_at < now - PENDING_TIMEOUT_SEC)
        .values(status=RELAY_FAILED, status_changed_at=now,
                status_detail=_detail(f"no relay answer within {PENDING_TIMEOUT_SEC} s"))
        .execution_options(synchronize_session=False)
    )
    counts["pending_to_relay_failed"] = result.rowcount or 0

    since_delivery = func.coalesce(PcDeployment.delivered_at, PcDeployment.status_changed_at)
    result = db.execute(
        update(PcDeployment)
        .where(
            PcDeployment.confirmable == True,  # noqa: E712
            PcDeployment.status.in_((DELIVERED, RECEIVED)),
            since_delivery < now - ack_timeout,
        )
        .values(status=UNCONFIRMED, status_changed_at=now, unconfirmed_at=func.coalesce(PcDeployment.unconfirmed_at, now),
                source="timeout", status_detail=_detail(f"no confirmation within {ack_timeout} s"))
        .execution_options(synchronize_session=False)
    )
    counts["delivered_to_unconfirmed"] = result.rowcount or 0

    since_accepted = func.coalesce(PcDeployment.accepted_at, PcDeployment.status_changed_at)
    result = db.execute(
        update(PcDeployment)
        .where(
            PcDeployment.confirmable == True,  # noqa: E712
            PcDeployment.status == ACCEPTED,
            since_accepted < now - 2 * ack_timeout,
        )
        .values(status=UNCONFIRMED, status_changed_at=now, unconfirmed_at=func.coalesce(PcDeployment.unconfirmed_at, now),
                source="timeout", status_detail=_detail(f"accepted but not applied within {2 * ack_timeout} s"))
        .execution_options(synchronize_session=False)
    )
    counts["accepted_to_unconfirmed"] = result.rowcount or 0
    return counts


# --------------------------------------------------------------------------- #
# Events that arrive before their row
# --------------------------------------------------------------------------- #


@dataclass
class PendingEvent:
    config_id: str
    apply: Callable[[Any], TransitionResult]  # apply(db) -> TransitionResult; must not commit
    expires_at: float
    attempts: int = 0
    label: str = ""
    created_at: float = field(default_factory=time.monotonic)


class PendingTransitions:
    """Events kept until their row exists (D14: "an event that arrives early is
    kept until its row exists"), or until a database error clears.

    Thread-safe and bounded: at most ``max_events`` are kept (the oldest is
    dropped first) and each expires after ``ttl_seconds``. ``retry(open_db)``
    re-applies every event in one session: ``changed``, ``stale`` and
    ``mismatch`` events are done, ``missing`` ones stay until they expire.
    """

    def __init__(self, ttl_seconds: float = 600.0, max_events: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_events = max_events
        self._clock = clock
        self._events: List[PendingEvent] = []
        self._lock = threading.Lock()
        self.dropped = 0
        self.expired = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def add(self, config_id: str, apply: Callable[[Any], TransitionResult], label: str = "") -> None:
        with self._lock:
            if len(self._events) >= self.max_events:
                self._events.pop(0)
                self.dropped += 1
            self._events.append(PendingEvent(config_id, apply, self._clock() + self.ttl_seconds, label=label))

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def retry(self, open_db: Callable[[], Any]) -> Tuple[int, int]:
        """Retry every kept event. Returns ``(applied, still_kept)``."""
        with self._lock:
            events, self._events = self._events, []
        if not events:
            return 0, 0
        now = self._clock()
        keep: List[PendingEvent] = []
        done = 0
        db = None
        try:
            db = open_db()
            for event in events:
                if event.expires_at <= now:
                    self.expired += 1
                    logger.warning(f"Dropping deploy event {event.label or '?'} for {event.config_id}: "
                                   f"its row never appeared")
                    continue
                event.attempts += 1
                result = event.apply(db)  # an error aborts the whole retry (below)
                if result.outcome == "missing":
                    keep.append(event)
                else:
                    done += 1
            db.commit()
        except Exception as exc:
            # Nothing was committed: keep every unexpired event for the next
            # retry (re-applying one that did move is a harmless no-op).
            logger.error(f"Deploy event retry aborted: {type(exc).__name__}")
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            keep = [event for event in events if event.expires_at > now]
            done = 0
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        with self._lock:
            self._events = keep + self._events
            while len(self._events) > self.max_events:
                self._events.pop(0)
                self.dropped += 1
            kept = len(self._events)
        return done, kept
