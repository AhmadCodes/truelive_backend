"""
PC maintenance tasks (plan 06).

Imported by app/tasks/celery_app.py, so every task here is registered in the
worker. D28 adds the environment self-check below, D16 the deploy-history
retention (daily, celery beat ``pc-deploy-history-prune``) and D17 the
catch-up re-push.
"""

import logging
from typing import Any, Dict, Optional

from celery import shared_task

from app.core.startup_check import collect

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.pc_maintenance.env_self_check")
def env_self_check() -> Dict[str, Any]:
    """The worker's view of the shared environment (D28): flags, secret
    fingerprints, SETTINGS_ENCRYPTION_KEY status and gate G-1. Never values.

    The API or an operator can compare its fingerprints with their own to
    prove that the worker runs with the same env, e.g.
    ``celery -A app.tasks.celery_app call app.tasks.pc_maintenance.env_self_check``.
    """
    report = collect("celery_worker")
    logger.info("env_self_check: %s", report.log_line())
    return report.as_dict()


@shared_task(name="app.tasks.pc_maintenance.prune_deploy_history")
def prune_deploy_history() -> Dict[str, Any]:
    """OD-22 (a) retention of pc_deployments (D16): per PC the newest 50
    rows, none older than 90 days. Returns the counts (no ids)."""
    from app.database import SessionLocal
    from app.services import pc_status

    db = SessionLocal()
    try:
        result = pc_status.prune_deploy_history(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    logger.info("prune_deploy_history: %s", result)
    return result


@shared_task(name="app.tasks.pc_maintenance.catch_up_deploy", ignore_result=True)
def catch_up_deploy(pc_id: str, config_id: str, config_hash: Optional[str] = None) -> Dict[str, Any]:
    """D17 catch-up re-push, queued by the relay (app/services/catchup.py).

    Re-checks that ``config_id`` is still the PC's latest deploy and still
    missed, that the PC still has that layout and that the layout's content
    still hashes to ``config_hash``; then deploys it again with trigger
    ``catch_up`` (a new config_id). Does nothing while
    DEPLOY_CATCHUP_ENABLED is off in the worker. Returns no layout.
    """
    from app.core.config import settings
    from app.database import SessionLocal
    from app.services import catchup

    if not settings.DEPLOY_CATCHUP_ENABLED:
        logger.info("catch_up_deploy for PC %s ignored: DEPLOY_CATCHUP_ENABLED is off in this worker", pc_id)
        return {"status": "disabled"}
    db = SessionLocal()
    try:
        result = catchup.run_catch_up(db, pc_id, config_id, config_hash)
    finally:
        db.close()
    logger.info("catch_up_deploy for PC %s (missed %s): %s", pc_id, config_id,
                {k: result.get(k) for k in ("status", "delivery", "config_id", "reason")})
    return result
