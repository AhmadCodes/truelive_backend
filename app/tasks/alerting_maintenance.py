"""
Periodic maintenance for the alerting partitions.

Three Celery beat tasks:

1. rollover_alerting_partitions — create next month's partitions ~1 week before
   the month begins, so writes never fall off the end of the range.

2. drop_old_alerting_partitions — drop partitions past retention. Retention is
   90 days for raw_messages + alerts and 30 days for alert_media +
   webhook_deliveries.

3. reconcile_smtp_ingest — re-enqueue raw_messages stuck in `status='received'`
   for more than 60 seconds. Catches the rare case where the LMTP ACK happened
   but the Celery enqueue failed.

All three are idempotent.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import text

from app.core.config import settings
from app.database import SessionLocal, engine

logger = logging.getLogger(__name__)


_PARTITIONED = (
    ("raw_messages", "received_at", "RETENTION_RAW_MAIL_DAYS"),
    ("alerts", "received_at", "RETENTION_ALERTS_DAYS"),
    ("alert_media", "created_at", "RETENTION_ALERT_MEDIA_DAYS"),
    ("webhook_deliveries", "attempted_at", "RETENTION_WEBHOOK_DELIVERIES_DAYS"),
)


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _partition_name(table: str, d: date) -> str:
    return f"{table}_p{d.year:04d}_{d.month:02d}"


def _ensure_partition(table: str, d: date) -> bool:
    """Create the monthly partition covering month `d`. Idempotent."""
    start = _month_start(d)
    end = _next_month(start)
    name = _partition_name(table, start)

    sql = text(
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
    with engine.begin() as conn:
        conn.execute(sql)
    return True


@shared_task(name="app.tasks.rollover_alerting_partitions")
def rollover_alerting_partitions(months_ahead: int = 2):
    """Ensure current + `months_ahead` future monthly partitions exist."""
    today = date.today()
    created = 0
    for table, _col, _retention_attr in _PARTITIONED:
        cur = _month_start(today)
        for _ in range(months_ahead + 1):
            try:
                _ensure_partition(table, cur)
                created += 1
            except Exception:
                logger.exception("partition create failed", extra={"table": table, "month": cur})
            cur = _next_month(cur)
    logger.info("partition rollover complete", extra={"created_or_existing": created})
    return created


@shared_task(name="app.tasks.drop_old_alerting_partitions")
def drop_old_alerting_partitions():
    """Drop partitions whose end date is before (today - retention_days)."""
    today = date.today()
    dropped = 0
    with engine.begin() as conn:
        for table, _col, retention_attr in _PARTITIONED:
            retention_days = getattr(settings, retention_attr)
            cutoff = today - timedelta(days=retention_days)
            # Walk pg_inherits to find this parent's partitions.
            rows = conn.execute(text("""
                SELECT child.relname
                FROM pg_inherits
                JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
                JOIN pg_class child  ON pg_inherits.inhrelid = child.oid
                WHERE parent.relname = :parent
            """), {"parent": table}).fetchall()
            for (child_name,) in rows:
                # Pull the partition's upper bound from the catalog. Names match
                # `{table}_pYYYY_MM`, so we can parse the month directly.
                tail = child_name.removeprefix(f"{table}_p")
                try:
                    year_s, month_s = tail.split("_", 1)
                    pmonth = date(int(year_s), int(month_s), 1)
                except (ValueError, IndexError):
                    continue
                pmonth_end = _next_month(pmonth)
                if pmonth_end <= cutoff:
                    conn.execute(text(f"DROP TABLE IF EXISTS {child_name}"))
                    dropped += 1
                    logger.info(
                        "dropped expired partition",
                        extra={"partition": child_name, "table": table, "end": pmonth_end.isoformat()},
                    )
    return dropped


@shared_task(name="app.tasks.reconcile_smtp_ingest")
def reconcile_smtp_ingest(older_than_seconds: int = 60):
    """Catch raw_messages that got persisted but never enqueued."""
    from app.tasks.process_inbound_alert import process_inbound_alert
    from app.models.alerting import RawMessage
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    n = 0
    with SessionLocal() as db:
        stuck = (
            db.query(RawMessage)
            .filter(RawMessage.status == "received", RawMessage.received_at < cutoff)
            .all()
        )
        for row in stuck:
            process_inbound_alert.apply_async(args=[row.id], queue="alert_parse")
            n += 1
    if n:
        logger.info("reconciled stuck raw_messages", extra={"count": n})
    return n
