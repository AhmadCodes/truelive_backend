"""
Actor identity + stamping helpers.

An "actor" is whoever performed a write: a human **user**, a **service account**,
or the **system** (background jobs, seeds, migrations). It is represented as a
`(type, id, label)` triple:

  - type:  'user' | 'service_account' | 'system'
  - id:    user UUID (as text) or service-account id; None for system
  - label: display name captured at write time (cached fallback)

This module is deliberately free of FastAPI imports so it can be used from
services, tasks, and the audit layer without pulling in the web stack.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

# (type, id, label)
ActorTriple = tuple[str, Optional[str], str]

SYSTEM_ACTOR: ActorTriple = ("system", None, "system")


def principal_to_actor(principal: Any) -> ActorTriple:
    """
    Convert an authenticated principal (a User or a ServiceAccount, as returned
    by the hybrid auth deps) into an ActorTriple. Falls back to SYSTEM_ACTOR when
    the principal is None or unrecognised.
    """
    if principal is None:
        return SYSTEM_ACTOR

    # Import lazily to avoid import cycles at module load.
    from app.models.user import User

    if isinstance(principal, User):
        return ("user", str(principal.user_id), principal.username)

    # ServiceAccount: duck-typed (has .id and .name). Avoids a hard import so this
    # module stays importable where the alerting models aren't migrated.
    sa_id = getattr(principal, "id", None)
    sa_name = getattr(principal, "name", None)
    if sa_id is not None and sa_name is not None:
        return ("service_account", str(sa_id), sa_name)

    return SYSTEM_ACTOR


# --------------------------------------------------------------------------- #
# Stamping
# --------------------------------------------------------------------------- #

def stamp_updated(entity: Any, actor: ActorTriple) -> None:
    """Set the updated_by_* triple on an ActorStampMixin entity."""
    entity.updated_by_type, entity.updated_by_id, entity.updated_by_label = actor


def stamp_created(entity: Any, actor: ActorTriple) -> None:
    """Set both created_by_* and updated_by_* triples on a freshly created entity."""
    entity.created_by_type, entity.created_by_id, entity.created_by_label = actor
    stamp_updated(entity, actor)


def snapshot(entity: Any) -> dict[str, Any]:
    """
    Generic column snapshot of an ORM entity, read straight from the mapped
    columns (works even for models that override to_dict()). Values are left raw;
    JSON encoding (datetimes/UUIDs) is handled by the audit layer via default=str.
    """
    return {c.name: getattr(entity, c.name) for c in entity.__table__.columns}


# --------------------------------------------------------------------------- #
# Secret masking
# --------------------------------------------------------------------------- #

MASK = "***"

# Secret field names per audit resource_type. Values of these fields are never
# stored in audit payloads — only the fact that they changed.
SECRET_FIELDS: dict[str, set[str]] = {
    "camera": {"rtsp_url", "main_stream_url"},
    "device": {"nvr_password", "nvr_username"},
    "pc": {"auth_token"},
    # site, team, layout, screen, view, screen_mapping: no secret fields
}


def mask_payload(resource_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `data` with this resource_type's secret fields masked."""
    secrets = SECRET_FIELDS.get(resource_type, set())
    return {
        k: (MASK if (k in secrets and v is not None) else v)
        for k, v in data.items()
    }


# Bookkeeping columns that must never appear in an update diff: the actor-stamp
# columns (redundant with the audit row's own actor_* columns) and the automatic
# timestamps. Excluding them also means a no-op business edit by a different actor
# produces an empty diff (so no spurious *.updated audit row is written).
_DIFF_EXCLUDED = frozenset({
    "created_by_type", "created_by_id", "created_by_label",
    "updated_by_type", "updated_by_id", "updated_by_label",
    "created_at", "updated_at",
})


def diff_fields(
    resource_type: str, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """
    Field-level {old, new} diff of only the changed *business* fields, with secret
    values masked. A secret field that changed appears as {"old": "***", "new": "***"}.
    Actor-stamp and timestamp columns are excluded (see _DIFF_EXCLUDED).
    """
    changed: dict[str, dict[str, Any]] = {}
    secrets = SECRET_FIELDS.get(resource_type, set())
    keys = (set(before) | set(after)) - _DIFF_EXCLUDED
    for k in keys:
        old = before.get(k)
        new = after.get(k)
        if old != new:
            if k in secrets:
                changed[k] = {"old": MASK if old is not None else None,
                              "new": MASK if new is not None else None}
            else:
                changed[k] = {"old": old, "new": new}
    return changed


# --------------------------------------------------------------------------- #
# Live label resolution (read paths)
# --------------------------------------------------------------------------- #

def resolve_label(
    db: Session, actor_type: Optional[str], actor_id: Optional[str], cached_label: Optional[str]
) -> Optional[str]:
    """
    Return the actor's CURRENT display name if the actor still exists, else the
    cached label. System actors and malformed ids fall back to the cached label.
    """
    from app.models.user import User

    if actor_type == "user" and actor_id:
        try:
            uid = uuid.UUID(str(actor_id))
        except (ValueError, TypeError):
            return cached_label
        row = db.query(User.username).filter(User.user_id == uid).first()
        return row[0] if row else cached_label

    if actor_type == "service_account" and actor_id:
        try:
            from app.models.service_account import ServiceAccount
        except Exception:
            return cached_label
        row = (
            db.query(ServiceAccount.name)
            .filter(ServiceAccount.id == str(actor_id))
            .first()
        )
        return row[0] if row else cached_label

    return cached_label


def resolve_labels(
    db: Session, pairs: list[tuple[Optional[str], Optional[str]]]
) -> dict[tuple[Optional[str], Optional[str]], str]:
    """
    Batch resolver: given a list of (actor_type, actor_id) pairs, return a map to
    current names. Two IN queries (users, service accounts) to avoid N+1 on list
    endpoints. Missing/deleted actors are simply absent from the map, so callers
    fall back to the cached label.
    """
    from app.models.user import User

    user_ids: set[str] = set()
    sa_ids: set[str] = set()
    for atype, aid in pairs:
        if not aid:
            continue
        if atype == "user":
            user_ids.add(str(aid))
        elif atype == "service_account":
            sa_ids.add(str(aid))

    out: dict[tuple[Optional[str], Optional[str]], str] = {}

    if user_ids:
        coerced: dict[uuid.UUID, str] = {}
        for s in user_ids:
            try:
                coerced[uuid.UUID(s)] = s
            except (ValueError, TypeError):
                continue
        if coerced:
            rows = (
                db.query(User.user_id, User.username)
                .filter(User.user_id.in_(list(coerced.keys())))
                .all()
            )
            for uid, uname in rows:
                out[("user", str(uid))] = uname

    if sa_ids:
        try:
            from app.models.service_account import ServiceAccount
            rows = (
                db.query(ServiceAccount.id, ServiceAccount.name)
                .filter(ServiceAccount.id.in_(list(sa_ids)))
                .all()
            )
            for sid, sname in rows:
                out[("service_account", str(sid))] = sname
        except Exception:
            pass

    return out


def _build_stamp(entity: Any, which: str, resolved_label: Optional[str]):
    """Build an ActorStamp for a response from an entity's stamp columns.
    `which` is 'created' or 'updated'. `resolved_label` overrides the cached label
    when the actor is still alive."""
    atype = getattr(entity, f"{which}_by_type", None)
    if not atype:
        return None
    from app.schemas.actor import ActorStamp

    aid = getattr(entity, f"{which}_by_id", None)
    cached = getattr(entity, f"{which}_by_label", None)
    return ActorStamp(type=atype, id=aid, label=resolved_label or cached or "system")


def attach_actor_stamps(db: Session, response: Any, orm: Any) -> Any:
    """
    Populate `response.created_by` / `response.updated_by` (live-resolved labels)
    from a single ORM entity. Use in GET-one and create/update endpoints.
    """
    ct, ci = getattr(orm, "created_by_type", None), getattr(orm, "created_by_id", None)
    ut, ui = getattr(orm, "updated_by_type", None), getattr(orm, "updated_by_id", None)
    labels = resolve_labels(db, [(ct, ci), (ut, ui)])
    response.created_by = _build_stamp(orm, "created", labels.get((ct, ci)))
    response.updated_by = _build_stamp(orm, "updated", labels.get((ut, ui)))
    return response


def resolve_stamps_for_orms(db: Session, orms: list) -> list:
    """
    Batch live-resolve helper for endpoints that return plain dicts (e.g. cameras).
    Returns a list aligned to `orms`, each item a (created_by_dict, updated_by_dict)
    tuple where each dict is {"type","id","label"} with the label live-resolved to
    the actor's current name (cached fallback if the actor is gone). None entries
    for rows without a stamp type.
    """
    pairs: list[tuple[Optional[str], Optional[str]]] = []
    for o in orms:
        pairs.append((getattr(o, "created_by_type", None), getattr(o, "created_by_id", None)))
        pairs.append((getattr(o, "updated_by_type", None), getattr(o, "updated_by_id", None)))
    labels = resolve_labels(db, pairs)

    def _dict(o, which):
        atype = getattr(o, f"{which}_by_type", None)
        if not atype:
            return None
        aid = getattr(o, f"{which}_by_id", None)
        cached = getattr(o, f"{which}_by_label", None)
        return {"type": atype, "id": aid, "label": labels.get((atype, aid)) or cached or "system"}

    return [( _dict(o, "created"), _dict(o, "updated") ) for o in orms]


def attach_actor_stamps_list(db: Session, responses: list, orms: list) -> list:
    """
    Batch variant: populate created_by/updated_by across a list of (response, orm)
    pairs using two IN queries total (no N+1). `responses` and `orms` must align.
    """
    pairs: list[tuple[Optional[str], Optional[str]]] = []
    for o in orms:
        pairs.append((getattr(o, "created_by_type", None), getattr(o, "created_by_id", None)))
        pairs.append((getattr(o, "updated_by_type", None), getattr(o, "updated_by_id", None)))
    labels = resolve_labels(db, pairs)
    for resp, o in zip(responses, orms):
        ct, ci = getattr(o, "created_by_type", None), getattr(o, "created_by_id", None)
        ut, ui = getattr(o, "updated_by_type", None), getattr(o, "updated_by_id", None)
        resp.created_by = _build_stamp(o, "created", labels.get((ct, ci)))
        resp.updated_by = _build_stamp(o, "updated", labels.get((ut, ui)))
    return responses
