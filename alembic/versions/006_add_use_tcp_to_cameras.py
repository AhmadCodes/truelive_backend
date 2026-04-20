"""Add use_tcp to sites (site-wide default) and cameras (per-camera override)

Revision ID: 006_add_use_tcp_to_cameras
Revises: 005_fix_invitation_tokens
Create Date: 2026-04-20

Adds:
- sites.use_tcp BOOLEAN NOT NULL DEFAULT false  (site-wide default)
- cameras.use_tcp BOOLEAN NULL                  (NULL inherits site, true/false overrides)

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '006_add_use_tcp_to_cameras'
down_revision: Union[str, None] = '005_fix_invitation_tokens'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add use_tcp to sites (default False) and nullable use_tcp to cameras (NULL = inherit site)."""
    op.add_column(
        'sites',
        sa.Column(
            'use_tcp',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment='Site-wide default: force RTSP over TCP for all cameras unless camera overrides'
        )
    )
    op.add_column(
        'cameras',
        sa.Column(
            'use_tcp',
            sa.Boolean(),
            nullable=True,
            comment='Per-camera override: NULL inherits site.use_tcp, true/false overrides'
        )
    )


def downgrade() -> None:
    """Remove use_tcp columns."""
    op.drop_column('cameras', 'use_tcp')
    op.drop_column('sites', 'use_tcp')
