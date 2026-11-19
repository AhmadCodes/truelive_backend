"""
A stream provider: a vendor account that mints HLS addresses for its cameras
(app/services/stream_providers; Lumana is the first type).
"""
import uuid

from sqlalchemy import JSON, Column, DateTime, String, Text, UniqueConstraint

from app.models.base import ActorStampMixin, BaseModel


class StreamProvider(BaseModel, ActorStampMixin):
    __tablename__ = "stream_providers"
    __table_args__ = (UniqueConstraint("name", name="uq_stream_providers_name"),)

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), comment="uuid4")
    name = Column(String(255), nullable=False, comment="Display name")
    type = Column(String(32), nullable=False, comment="Provider type id from the registry, e.g. lumana")
    settings = Column(JSON, nullable=False, default=dict, comment="Non-secret fields of the type")
    secrets = Column(Text, nullable=True,
                     comment="JSON object of the secret fields, encrypted with the settings key; never returned")
    secrets_changed_at = Column(DateTime(timezone=True), nullable=True, comment="When a secret was last written")
