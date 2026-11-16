"""
PC maintenance tasks (plan 06).

Imported by app/tasks/celery_app.py, so every task here is registered in the
worker. D28 adds the environment self-check below; D16 (deploy-history
retention) and D17 (catch-up re-push) add their tasks to this module.
"""

import logging
from typing import Any, Dict

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
