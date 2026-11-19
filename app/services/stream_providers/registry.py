"""The registry of stream provider types. A new vendor is one module plus
``register`` (see __init__.py)."""
from dataclasses import asdict
from typing import Dict, List, Optional

from app.services.stream_providers.base import StreamProvider

PROVIDER_TYPES: Dict[str, StreamProvider] = {}


def register(provider: StreamProvider) -> None:
    PROVIDER_TYPES[provider.type_id] = provider


def get(type_id: str) -> Optional[StreamProvider]:
    return PROVIDER_TYPES.get(type_id)


def describe() -> List[dict]:
    """What the portal builds its form from: types and their fields, never a stored value."""
    return [{"type": p.type_id, "label": p.label, "fields": [asdict(f) for f in p.fields]}
            for p in PROVIDER_TYPES.values()]
