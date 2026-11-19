"""
Stream providers API (admins only, except ``/options``).

A provider is a vendor account that mints HLS addresses for its cameras
(app/services/stream_providers). Its secret fields are write-only: they are
stored encrypted and no response, log line or audit entry carries them or a
minted address. The form the portal shows is built from ``GET /types``.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, DBSession
from app.models.camera import Camera
from app.models.stream_provider import StreamProvider
from app.services import audit_service
from app.services.actor import principal_to_actor, stamp_created, stamp_updated
from app.services.stream_providers import engine, registry
from app.services.stream_providers.base import ProviderError
from app.services.system_settings_service import system_settings_service
from app.utils.url_processor import PROVIDER_SCHEME, is_hls_url

logger = logging.getLogger(__name__)
router = APIRouter()

RESOURCE_TYPE = "stream_provider"
MSG_NO_KEY = "Set SETTINGS_ENCRYPTION_KEY before saving a provider key"


class ProviderWrite(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    type: Optional[str] = Field(None, max_length=32, description="Required on create; cannot change afterwards")
    settings: Dict[str, Any] = Field(default_factory=dict,
                                     description="The type's fields. A secret left out or empty keeps the stored one")


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _stored_secrets(row: Optional[StreamProvider]) -> Dict[str, Any]:
    if row is None or not row.secrets:
        return {}
    try:
        return engine.decrypt_secrets(row)
    except Exception:
        return {}  # written with another key: the admin enters it again


def _split_fields(impl, values: Dict[str, Any], row: Optional[StreamProvider]) -> Tuple[dict, dict, List[str]]:
    """Validate ``values`` against the type's fields. Returns
    ``(settings, secrets, names of the secrets written now)``."""
    known = {f.name for f in impl.fields}
    unknown = sorted(set(values) - known)
    if unknown:
        raise _invalid(f"Unknown field(s) for {impl.label}: {', '.join(unknown)}")
    plain: Dict[str, Any] = {}
    secrets = _stored_secrets(row)
    written: List[str] = []
    old = dict(row.settings or {}) if row is not None else {}
    for f in impl.fields:
        value = values.get(f.name)
        if isinstance(value, str):
            value = value.strip()
        if f.kind == "secret":
            if value not in (None, ""):
                secrets[f.name] = str(value)
                written.append(f.name)
            elif f.required and not secrets.get(f.name):
                raise _invalid(f"{f.label} is required")
            continue
        if value in (None, ""):
            value = old.get(f.name) if f.name not in values else None
        if value in (None, ""):
            value = f.default
        if value in (None, ""):
            if f.required:
                raise _invalid(f"{f.label} is required")
            continue
        if f.kind == "url":
            value = str(value).rstrip("/")
            if not is_hls_url(value):
                raise _invalid(f"{f.label} must start with http:// or https://")
        elif f.kind == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise _invalid(f"{f.label} must be a whole number") from None
        else:
            value = str(value)
        plain[f.name] = value
    return plain, {k: v for k, v in secrets.items() if k in known}, written


def _cameras_of(db, provider_id: str):
    # "%" and "_" cannot occur in a uuid4 provider id.
    return db.query(Camera).filter(Camera.rtsp_url.like(f"{PROVIDER_SCHEME}://{provider_id}/%"))


def _out(db, row: StreamProvider) -> Dict[str, Any]:
    impl = registry.get(row.type)
    stored = _stored_secrets(row)
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "type_label": impl.label if impl else row.type,
        "settings": dict(row.settings or {}),
        "secrets_set": {f.name: bool(stored.get(f.name)) for f in (impl.fields if impl else ())
                        if f.kind == "secret"},
        "secrets_changed_at": row.secrets_changed_at,
        "cameras_count": _cameras_of(db, row.id).count(),
        "status": engine.provider_status(row.id),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _get(db, provider_id: str) -> StreamProvider:
    row = db.query(StreamProvider).filter(StreamProvider.id == provider_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Stream provider not found")
    return row


def _name_taken(db, name: str, except_id: Optional[str] = None) -> bool:
    query = db.query(StreamProvider.id).filter(StreamProvider.name == name)
    if except_id:
        query = query.filter(StreamProvider.id != except_id)
    return query.first() is not None


def _audit(db, action: str, row: StreamProvider, actor, fields: List[str]) -> None:
    """Field names only: never a value (a key is among them)."""
    audit_service.record_event(db, action=f"{RESOURCE_TYPE}.{action}", resource_type=RESOURCE_TYPE,
                               resource_id=row.id, actor=actor,
                               changes={"name": row.name, "type": row.type, "fields": sorted(fields)})


def _forget_addresses(db, row: StreamProvider, release: bool) -> None:
    try:
        entry = engine.load_providers(db, [row.id]).get(row.id) if release else None
        engine.forget_provider(entry, row.id, engine.config(db))
    except Exception as exc:
        logger.warning(f"Cached addresses of provider {row.id} not dropped ({type(exc).__name__}); "
                       f"they expire by themselves")


@router.get("/types")
def list_types(current_user: AdminUser) -> List[Dict[str, Any]]:
    """The provider types and their fields (no stored value)."""
    return registry.describe()


@router.get("/options")
def list_options(current_user: CurrentUser, db: DBSession) -> List[Dict[str, Any]]:
    """``[{id, name, type}]`` for the camera form (any signed-in user)."""
    rows = db.query(StreamProvider).order_by(StreamProvider.name).all()
    return [{"id": row.id, "name": row.name, "type": row.type} for row in rows]


@router.get("")
def list_providers(current_user: AdminUser, db: DBSession) -> List[Dict[str, Any]]:
    return [_out(db, row) for row in db.query(StreamProvider).order_by(StreamProvider.name).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_provider(body: ProviderWrite, current_user: AdminUser, db: DBSession) -> Dict[str, Any]:
    impl = registry.get(body.type or "")
    if impl is None:
        raise _invalid("Unknown provider type")
    name = body.name.strip()
    if not name:
        raise _invalid("Name is required")
    if _name_taken(db, name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A provider named '{name}' already exists")
    plain, secrets, written = _split_fields(impl, body.settings, None)
    if secrets and not system_settings_service.has_shared_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MSG_NO_KEY)
    actor = principal_to_actor(current_user)
    row = StreamProvider(name=name, type=impl.type_id, settings=plain,
                         secrets=system_settings_service._encrypt_value(json.dumps(secrets)) if secrets else None,
                         secrets_changed_at=datetime.now(timezone.utc) if secrets else None)
    db.add(row)
    db.flush()
    stamp_created(row, actor)
    _audit(db, "created", row, actor, sorted(plain) + written)
    db.commit()
    db.refresh(row)
    logger.info(f"Stream provider {row.id} ({impl.type_id}) created")
    return _out(db, row)


@router.put("/{provider_id}")
def update_provider(provider_id: str, body: ProviderWrite, current_user: AdminUser, db: DBSession) -> Dict[str, Any]:
    row = _get(db, provider_id)
    impl = registry.get(row.type)
    if impl is None:
        raise _invalid(f"Provider type '{row.type}' is not installed on this server")
    if body.type not in (None, row.type):
        raise _invalid("The type of a provider cannot be changed; add a new provider instead")
    name = body.name.strip()
    if not name:
        raise _invalid("Name is required")
    if _name_taken(db, name, provider_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"A provider named '{name}' already exists")
    plain, secrets, written = _split_fields(impl, body.settings, row)
    if written and not system_settings_service.has_shared_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=MSG_NO_KEY)
    actor = principal_to_actor(current_user)
    changed = sorted(k for k in set(plain) | set(row.settings or {}) if plain.get(k) != (row.settings or {}).get(k))
    if name != row.name:
        changed.append("name")
    # The addresses minted with the old settings go first (released with them).
    if changed != ["name"] and (changed or written):
        _forget_addresses(db, row, release=True)
    row.name = name
    row.settings = plain
    if written:
        row.secrets = system_settings_service._encrypt_value(json.dumps(secrets))
        row.secrets_changed_at = datetime.now(timezone.utc)
    stamp_updated(row, actor)
    _audit(db, "updated", row, actor, changed + written)
    db.commit()
    db.refresh(row)
    logger.info(f"Stream provider {row.id} updated")
    return _out(db, row)


@router.post("/{provider_id}/test")
def test_provider(provider_id: str, current_user: AdminUser, db: DBSession) -> Dict[str, Any]:
    """Ask the provider whether the stored settings work (opens no stream)."""
    row = _get(db, provider_id)
    try:
        return {"ok": True, "message": engine.check_provider(db, row.id)}
    except ProviderError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: str, current_user: AdminUser, db: DBSession) -> None:
    row = _get(db, provider_id)
    cameras = _cameras_of(db, provider_id)
    count = cameras.count()
    if count:
        names = [name for (name,) in cameras.with_entities(Camera.name).order_by(Camera.name).limit(10).all()]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{count} camera(s) still use this provider: {', '.join(names)}{'...' if count > 10 else ''}",
        )
    actor = principal_to_actor(current_user)
    _forget_addresses(db, row, release=True)
    _audit(db, "deleted", row, actor, [])
    db.delete(row)
    db.commit()
    logger.info(f"Stream provider {provider_id} removed")
    return None
