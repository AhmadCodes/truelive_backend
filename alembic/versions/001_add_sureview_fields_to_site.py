"""Add SureView fields to Site model

Revision ID: 001_add_sureview_fields
Revises:
Create Date: 2025-10-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001_add_sureview_fields'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add SureView-related fields to sites table."""
    # Add new columns to sites table
    op.add_column('sites', sa.Column('customer_id', sa.String(length=50), nullable=True))
    op.add_column('sites', sa.Column('address', sa.String(length=500), nullable=True))
    op.add_column('sites', sa.Column('telephone', sa.String(length=255), nullable=True))
    op.add_column('sites', sa.Column('telephone2', sa.String(length=255), nullable=True))
    op.add_column('sites', sa.Column('telephone_police', sa.String(length=100), nullable=True))
    op.add_column('sites', sa.Column('telephone_fire', sa.String(length=100), nullable=True))
    op.add_column('sites', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('sites', sa.Column('lat_long', sa.String(length=100), nullable=True))

    # Create index on customer_id for faster lookups
    op.create_index('idx_sites_customer_id', 'sites', ['customer_id'])


def downgrade() -> None:
    """Remove SureView-related fields from sites table."""
    # Drop index
    op.drop_index('idx_sites_customer_id', table_name='sites')

    # Drop columns
    op.drop_column('sites', 'lat_long')
    op.drop_column('sites', 'notes')
    op.drop_column('sites', 'telephone_fire')
    op.drop_column('sites', 'telephone_police')
    op.drop_column('sites', 'telephone2')
    op.drop_column('sites', 'telephone')
    op.drop_column('sites', 'address')
    op.drop_column('sites', 'customer_id')
