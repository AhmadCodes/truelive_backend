"""
Catch-up re-push of a deploy a station missed (plan 06 D17; OD-20, NEW-21).

Off by default (``DEPLOY_CATCHUP_ENABLED=false``, M2): it changes a wall
unattended, so it is turned on only after M7, once D15's data shows the
deploy states are reliable. With the flag off the relay does nothing here at
all (no database read, no Redis, no broker).

When a station comes back, the relay decides once per session whether its
latest deploy was missed:

* a confirmable session (Client >= 1.4.0, D14) on its first station_status
  that carries ``wall.running_config_id``;
* any other session (Client 1.1.x) at register.

``decide`` re-pushes only when the PC's latest deploy (D13's order) is
``offline``, ``relay_failed`` or ``unconfirmed``, and nothing shows the
station ever got it (no received / accepted / applied / failed timestamp),
the wall does not report running it, and its layout still exists. The PC's
layout assignment must still be that layout (``evaluate_pc``).

A yes is rate-limited per PC by a Redis key (``SET NX EX 600``) and queued
as the Celery task ``app.tasks.pc_maintenance.catch_up_deploy`` with
``retry=False`` and short socket timeouts, from a relay background task and
on a tpool thread, so a broker or Redis outage never blocks the hub; a
failure pauses catch-up for BREAKER_SECONDS. The worker re-checks the
decision, regenerates the layout, refuses if its content changed since the
missed deploy (``config_hash``), and deploys through D14's
``deploy_config_to_pcs`` with trigger ``catch_up``: a new config_id, a new
row, audited like any deploy.

No loops: once per session, at most once per 10 minutes per PC, and only for
a latest row in one of the three states with no receipt.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.models.pc import PC
from app.models.pc_tracking import latest_deployment
from app.services import deploy_tracking

logger = logging.getLogger(__name__)

TASK_NAME = "app.tasks.pc_maintenance.catch_up_deploy"
RATE_LIMIT_SECONDS = 600  # OD-20: at most once per 10 minutes per PC
KEY_PREFIX = "truelive:catchup:"
TASK_EXPIRES_SECONDS = 120  # a catch-up queued during a long broker outage must not run hours later
SOCKET_TIMEOUT_SECONDS = 1.0
BREAKER_SECONDS = 30.0

# The latest-deploy states that mean "the station may have missed it".
PUSH_STATES = frozenset({deploy_tracking.OFFLINE, deploy_tracking.RELAY_FAILED, deploy_tracking.UNCONFIRMED})
# Any of these timestamps is evidence that the station got the deploy.
RECEIPT_FIELDS = ("received_at", "accepted_at", "applied_at", "failed_at")

# Trigger points (log and decision only).
AT_REGISTER = "register"
AT_STATUS = "station_status"


@dataclass
class Decision:
    push: bool
    reason: str
    config_id: Optional[str] = None
    layout_id: Optional[str] = None
    config_hash: Optional[str] = None


def decide(row: Any, *, running_config_id: Optional[str] = None) -> Decision:
    """Pure decision on a PC's latest deploy row (or None)."""
    if row is None:
        return Decision(False, "no deploy")
    if row.status not in PUSH_STATES:
        return Decision(False, f"latest deploy is {row.status}")
    if any(getattr(row, name, None) is not None for name in RECEIPT_FIELDS):
        return Decision(False, f"latest deploy is {row.status} but was received")
    if running_config_id and running_config_id == row.id:
        return Decision(False, "the wall reports running it")
    if not row.layout_id:
        return Decision(False, "its layout no longer exists")
    return Decision(True, f"latest deploy was missed ({row.status})", config_id=row.id, layout_id=row.layout_id,
                    config_hash=row.config_hash)


def evaluate_pc(db, pc_id: str, running_config_id: Optional[str] = None) -> Decision:
    """``decide`` on the PC's latest deploy, plus: the PC still exists and
    is still assigned that layout (an operator's later change wins)."""
    pc = db.query(PC.id, PC.screen_layout_id).filter(PC.id == pc_id).first()
    if pc is None:
        return Decision(False, "no such PC")
    decision = decide(latest_deployment(db, pc_id), running_config_id=running_config_id)
    if decision.push and pc.screen_layout_id != decision.layout_id:
        return Decision(False, "the PC's layout assignment changed since that deploy")
    return decision


# --------------------------------------------------------------------------- #
# Redis rate limit and the Celery queue (run on a tpool thread by the relay)
# --------------------------------------------------------------------------- #


def rate_key(pc_id: str) -> str:
    return KEY_PREFIX + pc_id


class RateLimiter:
    """``SET truelive:catchup:<pc_id> <ts> NX EX 600`` on Redis. Raises on any
    Redis error (the caller trips the breaker)."""

    def __init__(self, url: str, ttl: int = RATE_LIMIT_SECONDS, client_factory: Optional[Callable] = None):
        self.url = url
        self.ttl = ttl
        self._factory = client_factory
        self._client = None

    def _redis(self):
        if self._client is None:
            if self._factory is not None:
                self._client = self._factory()
            else:
                import redis

                self._client = redis.Redis.from_url(self.url, socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
                                                    socket_timeout=SOCKET_TIMEOUT_SECONDS, retry_on_timeout=False)
        return self._client

    def acquire(self, pc_id: str) -> bool:
        """True when no catch-up for this PC happened in the last ``ttl`` s."""
        try:
            return bool(self._redis().set(rate_key(pc_id), str(int(time.time())), nx=True, ex=self.ttl))
        except Exception:
            self._client = None  # reconnect next time
            raise


def _transport_options(broker_url: Optional[str]) -> Dict[str, Any]:
    """Fail-fast socket options for a Redis broker (production). Other kombu
    transports (the tests' SQLAlchemy one) reject unknown options."""
    scheme = (broker_url or "").split("://", 1)[0].lower()
    if scheme in ("redis", "rediss"):
        return {"socket_connect_timeout": SOCKET_TIMEOUT_SECONDS, "socket_timeout": SOCKET_TIMEOUT_SECONDS,
                "max_retries": 0}
    return {}


def enqueue(pc_id: str, config_id: str, config_hash: Optional[str]) -> str:
    """Queue the catch-up task: ``retry=False``, short socket timeouts, an
    expiry, no result. Returns the task id; raises when the broker is
    unreachable."""
    from app.tasks.celery_app import celery_app

    with celery_app.connection_for_write(
        connect_timeout=SOCKET_TIMEOUT_SECONDS,
        transport_options=_transport_options(celery_app.conf.broker_url),
    ) as connection:
        # ignore_result: otherwise send_task first subscribes to the result
        # backend (Redis, with its own 20-retry policy): about 60 s when it is down.
        result = celery_app.send_task(TASK_NAME, args=[pc_id, config_id, config_hash], connection=connection,
                                      retry=False, expires=TASK_EXPIRES_SECONDS, ignore_result=True)
    return result.id


class Breaker:
    """Pauses catch-up for ``seconds`` after a Redis or broker failure, so an
    outage costs one timeout, not one per station."""

    def __init__(self, seconds: float = BREAKER_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self.open_until = 0.0

    def is_open(self) -> bool:
        return self.clock() < self.open_until

    def trip(self) -> None:
        self.open_until = self.clock() + self.seconds

    def reset(self) -> None:
        self.open_until = 0.0


def run_catch_up(db, pc_id: str, config_id: str, config_hash: Optional[str], *, sender=None) -> Dict[str, Any]:
    """The worker side: re-check, then deploy with trigger ``catch_up``.
    Returns a summary without the layout."""
    from app.services import deploy_service
    from app.services.actor import SYSTEM_ACTOR

    decision = evaluate_pc(db, pc_id)
    if not decision.push:
        return {"status": "skipped", "reason": decision.reason}
    if decision.config_id != config_id:
        return {"status": "skipped", "reason": "a newer deploy replaced the missed one"}
    pc = db.query(PC).filter(PC.id == pc_id).first()
    expected = {pc_id: config_hash} if config_hash else None
    summary = deploy_service.deploy_config_to_pcs([pc], db, trigger=deploy_service.TRIGGER_CATCH_UP,
                                                  actor=SYSTEM_ACTOR, sender=sender, expected_hashes=expected)
    result = summary["results"][0]
    return {"status": result["status"], "delivery": result["delivery"], "config_id": result["config_id"],
            "replaces": config_id, "message": result["message"]}
