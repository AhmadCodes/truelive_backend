"""
Shared response schema for creator/modifier attribution.

Every in-scope entity's Response carries `created_by` / `updated_by` so the
frontend can show "who created / last modified this, and when" (the "when" comes
from the existing `created_at` / `updated_at` fields). The `label` is live-resolved
to the actor's current name at read time when the actor still exists, otherwise it
falls back to the cached label stored on the row.
"""

from typing import Optional

from pydantic import BaseModel, Field


class ActorStamp(BaseModel):
    """Who performed a create/update, for display."""

    type: str = Field(..., description="Actor kind: 'user' | 'service_account' | 'system'")
    id: Optional[str] = Field(None, description="User id or service-account id; null for system")
    label: str = Field(..., description="Display name of the actor")


class ActorStampsMixin(BaseModel):
    """Mixin for entity Response schemas: adds created_by / updated_by."""

    created_by: Optional[ActorStamp] = None
    updated_by: Optional[ActorStamp] = None
