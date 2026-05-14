"""
Celery application configuration.

This module sets up Celery for background task processing.
"""
from celery import Celery
from celery.schedules import crontab
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# Create Celery app
celery_app = Celery(
    "truelive_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour max per task
    task_soft_time_limit=3000,  # 50 minute soft limit
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
)

# Dedicated queues for the alerting pipeline. alert_parse handles MIME parsing
# and persistence; alert_deliver runs with prefetch=1 so slow consumer responses
# don't block parallel deliveries. Configure both worker pools in deployment:
#   celery -A app.tasks.celery_app worker -Q alert_parse,celery -c 4
#   celery -A app.tasks.celery_app worker -Q alert_deliver -c 8 --prefetch-multiplier=1
celery_app.conf.task_routes = {
    "app.tasks.process_inbound_alert": {"queue": "alert_parse"},
    "app.tasks.deliver_webhook": {"queue": "alert_deliver"},
    "app.tasks.deliver_webhook_retry": {"queue": "alert_deliver"},
    "app.tasks.rollover_alerting_partitions": {"queue": "celery"},
    "app.tasks.drop_old_alerting_partitions": {"queue": "celery"},
    "app.tasks.reconcile_smtp_ingest": {"queue": "alert_parse"},
}

# Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    'update-snapshots': {
        'task': 'app.tasks.snapshot_tasks.update_snapshots',
        'schedule': settings.BACKGROUND_TASK_INTERVAL,  # 10 minutes
        'options': {
            'expires': 300  # Task expires after 5 minutes if not picked up
        }
    },
    'sync-sureview-devices': {
        'task': 'app.tasks.sureview_tasks.sync_devices',
        'schedule': settings.BACKGROUND_TASK_INTERVAL,  # 10 minutes
        'options': {
            'expires': 300  # Task expires after 5 minutes if not picked up
        }
    },
    'alerting-rollover-partitions': {
        'task': 'app.tasks.rollover_alerting_partitions',
        'schedule': crontab(hour=1, minute=30),  # Daily at 01:30 UTC
    },
    'alerting-drop-old-partitions': {
        'task': 'app.tasks.drop_old_alerting_partitions',
        'schedule': crontab(hour=2, minute=0),   # Daily at 02:00 UTC
    },
    'alerting-reconcile-ingest': {
        'task': 'app.tasks.reconcile_smtp_ingest',
        'schedule': 60,  # every minute
    },
}


@celery_app.task(bind=True)
def debug_task(self):
    """
    Debug task for testing Celery setup.
    """
    print(f'Request: {self.request!r}')
    return f'Task completed successfully'


# Import tasks to register them with Celery
# This must be at the end to avoid circular imports
try:
    from app.tasks import snapshot_tasks, sureview_tasks
except ImportError as e:
    logger.warning(f"Could not import task modules: {e}")

try:
    from app.tasks import (
        process_inbound_alert,  # noqa: F401
        deliver_webhook,        # noqa: F401
        alerting_maintenance,   # noqa: F401
    )
except ImportError as e:
    logger.warning(f"Could not import alerting task modules: {e}")
