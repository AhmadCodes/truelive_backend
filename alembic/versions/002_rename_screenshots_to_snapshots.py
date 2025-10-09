"""rename screenshots to snapshots

Revision ID: 002
Revises: 001
Create Date: 2025-10-09

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002_rename_to_snapshots'
down_revision = '001_initial_schema'
branch_labels = None
depends_on = None


def upgrade():
    """
    Rename screenshots table to snapshots and update related indexes.
    """
    # Rename the table
    op.rename_table('screenshots', 'snapshots')

    # Rename the index (if it exists)
    # PostgreSQL automatically renames indexes when table is renamed, but we'll be explicit
    op.execute('ALTER INDEX IF EXISTS idx_screenshots_capture_time RENAME TO idx_snapshots_capture_time')


def downgrade():
    """
    Revert snapshots table back to screenshots.
    """
    # Rename the table back
    op.rename_table('snapshots', 'screenshots')

    # Rename the index back
    op.execute('ALTER INDEX IF EXISTS idx_snapshots_capture_time RENAME TO idx_screenshots_capture_time')
