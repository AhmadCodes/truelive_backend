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
    "shomer_tasks",
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

# Beat schedule for periodic tasks
celery_app.conf.beat_schedule = {
    'update-screenshots': {
        'task': 'app.tasks.screenshot_tasks.update_screenshots',
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
    from app.tasks import screenshot_tasks, sureview_tasks
except ImportError as e:
    logger.warning(f"Could not import task modules: {e}")
