"""Add sync_jobs table for async sync tracking

Revision ID: 003_add_sync_jobs
Revises: 002_add_full_name
Create Date: 2025-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '003_add_sync_jobs'
down_revision: Union[str, None] = '002_add_full_name'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create sync_jobs table for tracking async SureView sync operations."""

    # Create enum type for job status using DO block to avoid "already exists" errors
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE syncjobstatus AS ENUM ('pending', 'in_progress', 'completed', 'failed');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Create sync_jobs table
    op.create_table(
        'sync_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()'), comment='Unique identifier for the sync job (UUID)'),
        sa.Column('status', postgresql.ENUM('pending', 'in_progress', 'completed', 'failed', name='syncjobstatus', create_type=False),
                  nullable=False, comment='Current status of the sync job'),
        sa.Column('progress', sa.Integer(), nullable=False, server_default='0',
                  comment='Progress percentage (0-100)'),
        sa.Column('progress_message', sa.String(500), nullable=True,
                  comment='Current step or progress description'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True,
                  comment='Timestamp when sync job actually started processing'),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True,
                  comment='Timestamp when sync job completed or failed'),
        sa.Column('result', postgresql.JSON(astext_type=sa.Text()), nullable=True,
                  comment='Sync results (sites_updated, cameras_updated, etc.)'),
        sa.Column('error_message', sa.Text(), nullable=True,
                  comment='Error message if sync failed'),
        sa.Column('triggered_by', postgresql.UUID(as_uuid=True), nullable=True,
                  comment='User who triggered the sync'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='When sync job was created'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='When sync job was last updated'),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['triggered_by'], ['users.user_id'], ondelete='SET NULL')
    )

    # Create indexes for better query performance
    op.create_index('idx_sync_jobs_status', 'sync_jobs', ['status'])
    op.create_index('idx_sync_jobs_triggered_by', 'sync_jobs', ['triggered_by'])
    op.create_index('idx_sync_jobs_created_at', 'sync_jobs', ['created_at'], postgresql_using='btree')


def downgrade() -> None:
    """Remove sync_jobs table."""
    op.drop_index('idx_sync_jobs_created_at', table_name='sync_jobs')
    op.drop_index('idx_sync_jobs_triggered_by', table_name='sync_jobs')
    op.drop_index('idx_sync_jobs_status', table_name='sync_jobs')
    op.drop_table('sync_jobs')
    op.execute('DROP TYPE syncjobstatus')
