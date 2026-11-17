"""
PC status, deploy history and retention (plan 06 D16; OD-22, NEW-51, E20).

* ``status_rows``: every PC (or one) with its ``pc_health`` row and its
  latest ``pc_deployments`` row in **one query**. "Latest" is D13's order:
  ``row_number()`` over (status = 'superseded', created_at desc, id desc), so
  a same-second tie still yields exactly one row per PC.
* ``health_summary`` / ``health_detail`` / ``deployment_summary`` map rows to
  the response models (app/schemas/pc_status.py): epoch columns become
  timezone-aware UTC datetimes, ``health_stale`` is set when the last
  station_status is older than HEALTH_STALE_AFTER_SEC (180 s), and free-text
  details have URLs replaced by ``<url>``. Nothing carries a layout body or a
  token.
* ``deployment_history``: a PC's deploys, newest first (created_at desc, id
  desc), paginated.
* ``prune_deploy_history``: OD-22 (a) retention. Per PC the newest
  KEEP_PER_PC (50) rows are kept, and no row older than MAX_AGE_DAYS (90).
  Run daily by Celery (app.tasks.pc_maintenance.prune_deploy_history).
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, delete, func, or_, select

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment, PcHealth, latest_deployments_subquery

logger = logging.getLogger(__name__)

HEALTH_STALE_AFTER_SEC = 180
KEEP_PER_PC = 50
MAX_AGE_DAYS = 90
PRUNE_BATCH = 5000
HISTORY_DEFAULT_LIMIT = 20
HISTORY_MAX_LIMIT = 100

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")

DEPLOY_EPOCH_FIELDS = ("status_changed_at", "delivered_at", "received_at", "accepted_at", "applied_at", "failed_at",
                       "unconfirmed_at", "superseded_at")
_DEPLOY_PLAIN_FIELDS = ("pc_id", "status", "trigger", "layout_id", "layout_name", "config_hash", "screens", "mode",
                        "used_fallback", "persisted", "source", "client_version", "core_version")

_SUMMARY_PLAIN = (
    "health", "client_version", "client_build", "core_version", "core_installed_version", "core_running_version",
    "core_version_source", "core_build_class", "delivery_policy", "core_state", "core_pid", "core_uptime_sec",
    "core_restarts_24h", "core_last_exit", "telemetry_mode", "tap_state", "running_config_id", "deploying",
    "screens", "streams_total", "streams_playing", "streams_connecting", "streams_failed_count",
)
_DETAIL_PLAIN = (
    "last_message_id", "core_exe_sha256", "core_exe_size", "core_config_mode", "mode_set_by", "core_revision",
    "config_core_version", "migration_status", "legacy_available", "data_dir_acl_warning", "fleet_errors_count",
)
_DETAIL_LISTS = ("capabilities", "streams_failed", "core_caps", "drift_key_names")


# --------------------------------------------------------------------------- #
# Small converters
# --------------------------------------------------------------------------- #


def utc_from_epoch(value: Any) -> Optional[datetime]:
    """Unix seconds (pcs / pc_deployments / pc_health epoch columns) as an
    aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """An aware datetime in UTC; a naive one (SQLite) is taken as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_iso(value: Any) -> Optional[str]:
    """An ISO 8601 string with an explicit UTC offset. The relay's
    ``connected_at`` was naive UTC (NEW-51), which a browser reads as local
    time; a naive value is taken as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed).isoformat()


def scrub(text: Optional[str]) -> Optional[str]:
    """Free text with every URL replaced by ``<url>`` (defence in depth: the
    relay already strips them, D15)."""
    if text is None:
        return None
    return _URL_RE.sub("<url>", str(text))


def _list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _bool_dict(value: Any) -> Dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {str(k): bool(v) for k, v in value.items() if isinstance(v, bool)}


# --------------------------------------------------------------------------- #
# Row -> response dicts
# --------------------------------------------------------------------------- #


def is_stale(row: Optional[PcHealth], now: Optional[float] = None) -> bool:
    """No station_status received for more than HEALTH_STALE_AFTER_SEC."""
    if row is None or row.received_at is None:
        return True
    current = time.time() if now is None else now
    return current - int(row.received_at) > HEALTH_STALE_AFTER_SEC


def health_summary(row: Optional[PcHealth], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out: Dict[str, Any] = {name: getattr(row, name) for name in _SUMMARY_PLAIN}
    out["reasons"] = [str(r) for r in _list(row.reasons)]
    out["health_stale"] = is_stale(row, now)
    out["received_at"] = utc_from_epoch(row.received_at)
    out["reported_at"] = utc_from_epoch(row.reported_at)
    out["registered_at"] = utc_from_epoch(row.registered_at)
    out["relay_flags"] = _bool_dict(row.relay_flags)
    return out


def health_detail(row: Optional[PcHealth], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    out = health_summary(row, now)
    if out is None:
        return None
    out["pc_id"] = row.pc_id
    for name in _DETAIL_PLAIN:
        out[name] = getattr(row, name)
    for name in _DETAIL_LISTS:
        out[name] = [str(v) for v in _list(getattr(row, name))]
    out["telemetry_channels"] = row.telemetry_channels if isinstance(row.telemetry_channels, dict) else {}
    out["flags"] = _bool_dict(row.flags)
    readiness = []
    for item in _list(row.readiness):
        if isinstance(item, dict) and item.get("code"):
            readiness.append({"code": str(item["code"]), "detail": scrub(item.get("detail"))})
    out["readiness"] = readiness
    out["last_config_error"] = scrub(row.last_config_error)
    out["updated_at"] = as_utc(row.updated_at)
    return out


def deployment_summary(row: Optional[PcDeployment]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out: Dict[str, Any] = {name: getattr(row, name) for name in _DEPLOY_PLAIN_FIELDS}
    out["config_id"] = row.id
    out["confirmable"] = bool(row.confirmable)
    out["status_detail"] = scrub(row.status_detail)
    out["created_at"] = as_utc(row.created_at)
    for name in DEPLOY_EPOCH_FIELDS:
        out[name] = utc_from_epoch(getattr(row, name))
    if row.created_by_type:
        out["deployed_by"] = {"type": row.created_by_type, "id": row.created_by_id, "label": row.created_by_label}
    else:
        out["deployed_by"] = None
    return out


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def status_rows(db, pc_id: Optional[str] = None) -> List[Tuple[PC, Optional[PcHealth], Optional[PcDeployment]]]:
    """``(pc, health, latest deployment)`` per PC, one query, ordered by PC id."""
    latest = latest_deployments_subquery()
    query = (
        db.query(PC, PcHealth, PcDeployment)
        .outerjoin(PcHealth, PcHealth.pc_id == PC.id)
        .outerjoin(latest, latest.c.pc_id == PC.id)
        .outerjoin(PcDeployment, PcDeployment.id == latest.c.id)
    )
    if pc_id is not None:
        query = query.filter(PC.id == pc_id)
    return query.order_by(PC.id).all()


def deployment_history(db, pc_id: str, limit: int = HISTORY_DEFAULT_LIMIT,
                       offset: int = 0) -> Tuple[int, List[PcDeployment]]:
    total = db.query(func.count(PcDeployment.id)).filter(PcDeployment.pc_id == pc_id).scalar() or 0
    rows = (
        db.query(PcDeployment)
        .filter(PcDeployment.pc_id == pc_id)
        .order_by(PcDeployment.created_at.desc(), PcDeployment.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return int(total), rows


# --------------------------------------------------------------------------- #
# Retention (OD-22 (a))
# --------------------------------------------------------------------------- #


def prune_deploy_history(db, *, keep: int = KEEP_PER_PC, max_age_days: int = MAX_AGE_DAYS,
                         now: Optional[datetime] = None, batch: int = PRUNE_BATCH) -> Dict[str, int]:
    """Delete every deploy row beyond the newest ``keep`` of its PC (by
    created_at desc, id desc) and every row created more than
    ``max_age_days`` ago. The caller commits. Returns the counts."""
    current = as_utc(now) if now is not None else datetime.now(timezone.utc)
    cutoff = current - timedelta(days=max_age_days)
    if db.get_bind().dialect.name == "sqlite":
        cutoff = cutoff.replace(tzinfo=None)  # SQLite stores naive UTC
    rank = func.row_number().over(partition_by=PcDeployment.pc_id,
                                  order_by=(PcDeployment.created_at.desc(), PcDeployment.id.desc()))
    ranked = select(PcDeployment.id.label("id"), PcDeployment.created_at.label("created_at"),
                    rank.label("rn")).subquery()
    doomed = db.execute(
        select(ranked.c.id, ranked.c.created_at < cutoff).where(or_(ranked.c.rn > keep, ranked.c.created_at < cutoff))
    ).all()
    by_age = sum(1 for _id, old in doomed if old)
    ids = [row_id for row_id, _old in doomed]
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        db.execute(delete(PcDeployment).where(PcDeployment.id.in_(chunk)))
    result = {"deleted": len(ids), "by_age": by_age, "by_count": len(ids) - by_age}
    if ids:
        logger.info(f"Deploy history retention: deleted {len(ids)} row(s) "
                    f"({result['by_count']} beyond {keep} per PC, {by_age} older than {max_age_days} days)")
    return result
