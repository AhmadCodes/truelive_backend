"""Stream providers (Lumana first): table stream_providers and pcs.hls_capable.

Shipped with backend 1.2.0.

* ``stream_providers``: a vendor account that mints HLS addresses. Secret
  fields are stored encrypted (settings Fernet) in ``secrets``.
* ``pcs.hls_capable``: admin-set "runs TrueLive Live 1.3.0 or later"; NULL
  means not confirmed. Client 1.2.0 reports no Core version.

Purely additive: no existing row or column is touched (a provider camera is
a ``provider://`` value in cameras.rtsp_url). The downgrade drops both and
loses only the providers and the flags. Idempotent and inspector-guarded,
like 015.

VERIFY ON A SCRATCH DATABASE OR A RESTORED COPY OF PRODUCTION, never the
live database.

Revision ID: 016_stream_providers
Revises: 015_pc_deploy_tracking
Create Date: 2026-11-20
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "016_stream_providers"
down_revision: Union[str, None] = "015_pc_deploy_tracking"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    if "stream_providers" not in _inspector().get_table_names():
        op.create_table(
            "stream_providers",
            sa.Column("id", sa.String(36), nullable=False, comment="uuid4"),
            sa.Column("name", sa.String(255), nullable=False, comment="Display name"),
            sa.Column("type", sa.String(32), nullable=False,
                      comment="Provider type id from the registry, e.g. lumana"),
            sa.Column("settings", sa.JSON(), nullable=False, comment="Non-secret fields of the type"),
            sa.Column("secrets", sa.Text(), nullable=True,
                      comment="JSON object of the secret fields, encrypted with the settings key; never returned"),
            sa.Column("secrets_changed_at", sa.DateTime(timezone=True), nullable=True,
                      comment="When a secret was last written"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
                      comment="Timestamp when the record was created"),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
                      comment="Timestamp when the record was last updated"),
            sa.Column("created_by_type", sa.String(20), nullable=False, server_default="system",
                      comment="Actor kind that created the row: user|service_account|system"),
            sa.Column("created_by_id", sa.String(36), nullable=True,
                      comment="User UUID (as text) or service-account id of the creator; NULL for system"),
            sa.Column("created_by_label", sa.String(255), nullable=False, server_default="system",
                      comment="Cached display name of the creator at creation time"),
            sa.Column("updated_by_type", sa.String(20), nullable=False, server_default="system",
                      comment="Actor kind that last modified the row: user|service_account|system"),
            sa.Column("updated_by_id", sa.String(36), nullable=True,
                      comment="User UUID (as text) or service-account id of the last modifier; NULL for system"),
            sa.Column("updated_by_label", sa.String(255), nullable=False, server_default="system",
                      comment="Cached display name of the last modifier at modification time"),
            sa.PrimaryKeyConstraint("id", name="stream_providers_pkey"),
            sa.UniqueConstraint("name", name="uq_stream_providers_name"),
        )
    if "hls_capable" not in {c["name"] for c in _inspector().get_columns("pcs")}:
        op.add_column("pcs", sa.Column(
            "hls_capable", sa.Boolean(), nullable=True,
            comment="Admin-set: runs TrueLive Live (Core) 1.3.0 or later; NULL = not confirmed"))


def downgrade() -> None:
    if "hls_capable" in {c["name"] for c in _inspector().get_columns("pcs")}:
        op.drop_column("pcs", "hls_capable")
    if "stream_providers" in _inspector().get_table_names():
        op.drop_table("stream_providers")
