"""
Stream provider tasks (app/services/stream_providers/engine.py).

* ``tick``: celery beat, every 30 s: renew addresses that are due, check the
  ones in use, deliver to stations that are behind, release unused ones.
* ``refresh_pc``: queued by the relay when a station connects, and by a
  deploy that left cells pending.
* ``station_failed``: queued by the relay when a station reports failed
  cells (at most once per minute per PC).

No result is kept and nothing is retried: the next tick covers a failure.
"""

import logging
from typing import Any, Callable, Dict, List

from celery import shared_task

logger = logging.getLogger(__name__)


def _run(work: Callable) -> Dict[str, Any]:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return work(db)
    finally:
        db.rollback()  # the engine only reads the database
        db.close()


@shared_task(name="app.tasks.stream_provider_tasks.tick", ignore_result=True)
def tick() -> Dict[str, Any]:
    from app.services.stream_providers import engine

    result = _run(engine.tick)
    if result.get("status") == "ok" and (result["renewed"] or result["delivered"] or result["released"]):
        logger.info("stream providers tick: %s", result)
    return result


@shared_task(name="app.tasks.stream_provider_tasks.refresh_pc", ignore_result=True)
def refresh_pc(pc_id: str, reason: str = "connect") -> Dict[str, Any]:
    from app.services.stream_providers import engine

    return _run(lambda db: engine.refresh_pc(db, pc_id, reason))


@shared_task(name="app.tasks.stream_provider_tasks.station_failed", ignore_result=True)
def station_failed(pc_id: str, source_ids: List[str]) -> Dict[str, Any]:
    from app.services.stream_providers import engine

    return _run(lambda db: engine.station_failed(db, pc_id, source_ids))
