"""Add system_settings table for runtime configuration

Revision ID: 004_add_system_settings
Revises: 003_add_sync_jobs
Create Date: 2025-10-22

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column
from sqlalchemy.dialects.postgresql import UUID
import uuid
import os

# revision identifiers, used by Alembic.
revision: str = '004_add_system_settings'
down_revision: Union[str, None] = '003_add_sync_jobs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create system_settings table and populate with defaults from ENV."""

    # Create system_settings table
    op.create_table(
        'system_settings',
        sa.Column('id', sa.String(255), nullable=False, comment='UUID primary key'),
        sa.Column('key', sa.String(255), nullable=False, unique=True, comment='Unique setting key'),
        sa.Column('value', sa.Text(), nullable=True, comment='Setting value (may be encrypted)'),
        sa.Column('category', sa.String(50), nullable=False, comment='Setting category'),
        sa.Column('description', sa.Text(), nullable=True, comment='Setting description'),
        sa.Column('is_encrypted', sa.Boolean(), nullable=False, server_default='false',
                  comment='Whether value is encrypted'),
        sa.Column('data_type', sa.String(20), nullable=False, server_default='string',
                  comment='Data type: string, integer, boolean'),
        sa.Column('updated_by', UUID(as_uuid=True), nullable=True, comment='User who last updated'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['updated_by'], ['users.user_id'], ondelete='SET NULL')
    )

    # Create indexes
    op.create_index('idx_system_settings_key', 'system_settings', ['key'], unique=True)
    op.create_index('idx_system_settings_category', 'system_settings', ['category'])

    # Define table for data insertion
    system_settings = table('system_settings',
        column('id', sa.String),
        column('key', sa.String),
        column('value', sa.Text),
        column('category', sa.String),
        column('description', sa.Text),
        column('is_encrypted', sa.Boolean),
        column('data_type', sa.String)
    )

    # Helper to get env value or default
    def get_env(key: str, default: str = "") -> str:
        return os.getenv(key, default)

    # Seed data: SureView Settings
    sureview_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'sureview.username',
            'value': get_env('SUREVIEW_USERNAME'),
            'category': 'sureview',
            'description': 'SureView API username for authentication',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'sureview.password',
            'value': get_env('SUREVIEW_PASSWORD'),
            'category': 'sureview',
            'description': 'SureView API password (will be encrypted)',
            'is_encrypted': True,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'sureview.api_url',
            'value': get_env('SUREVIEW_API_URL'),
            'category': 'sureview',
            'description': 'SureView API base URL',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'sureview.login_url',
            'value': get_env('SUREVIEW_LOGIN_URL'),
            'category': 'sureview',
            'description': 'SureView login page URL for Selenium',
            'is_encrypted': False,
            'data_type': 'string'
        }
    ]

    # Seed data: SMTP Settings
    smtp_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.host',
            'value': get_env('SMTP_HOST', 'mail.usvg.ai'),
            'category': 'smtp',
            'description': 'SMTP server hostname',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.port',
            'value': get_env('SMTP_PORT', '587'),
            'category': 'smtp',
            'description': 'SMTP server port',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.user',
            'value': get_env('SMTP_USER', 'info@usvg.ai'),
            'category': 'smtp',
            'description': 'SMTP authentication username',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.password',
            'value': get_env('SMTP_PASSWORD'),
            'category': 'smtp',
            'description': 'SMTP authentication password (will be encrypted)',
            'is_encrypted': True,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.from_email',
            'value': get_env('SMTP_FROM_EMAIL', 'info@usvg.ai'),
            'category': 'smtp',
            'description': 'From email address for outgoing emails',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.from_name',
            'value': get_env('SMTP_FROM_NAME', 'Shomer Portal'),
            'category': 'smtp',
            'description': 'From name for outgoing emails',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.use_tls',
            'value': get_env('SMTP_USE_TLS', 'true'),
            'category': 'smtp',
            'description': 'Use TLS for SMTP connection',
            'is_encrypted': False,
            'data_type': 'boolean'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.frontend_url',
            'value': get_env('FRONTEND_URL', 'http://localhost:3000'),
            'category': 'smtp',
            'description': 'Frontend URL for email links',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'smtp.invitation_token_expiry_hours',
            'value': get_env('INVITATION_TOKEN_EXPIRY_HOURS', '72'),
            'category': 'smtp',
            'description': 'Hours until invitation token expires',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Seed data: Task Settings
    task_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'tasks.sync_interval_seconds',
            'value': get_env('BACKGROUND_TASK_INTERVAL', '600'),
            'category': 'tasks',
            'description': 'Interval in seconds for SureView sync (default: 600 = 10 minutes)',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Seed data: Snapshot Settings
    snapshot_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'snapshots.max_age_hours',
            'value': get_env('SNAPSHOT_MAX_AGE_HOURS', '24'),
            'category': 'snapshots',
            'description': 'Maximum age of snapshots before recapture (hours)',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'snapshots.capture_timeout',
            'value': get_env('SNAPSHOT_CAPTURE_TIMEOUT', '10'),
            'category': 'snapshots',
            'description': 'Timeout for snapshot capture (seconds)',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'snapshots.max_workers',
            'value': get_env('SNAPSHOT_MAX_WORKERS', '5'),
            'category': 'snapshots',
            'description': 'Maximum parallel snapshot captures',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'snapshots.batch_time_limit',
            'value': get_env('SNAPSHOT_BATCH_TIME_LIMIT', '300'),
            'category': 'snapshots',
            'description': 'Time limit for batch snapshot processing (seconds)',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Seed data: WebSocket Settings
    websocket_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'websocket.url',
            'value': get_env('WEBSOCKET_URL', 'http://localhost:8080'),
            'category': 'websocket',
            'description': 'WebSocket server URL for frontend connections',
            'is_encrypted': False,
            'data_type': 'string'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'websocket.port',
            'value': get_env('WEBSOCKET_PORT', '8080'),
            'category': 'websocket',
            'description': 'WebSocket server port',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Seed data: Security Settings
    security_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'security.rate_limit_enabled',
            'value': get_env('RATE_LIMIT_ENABLED', 'true'),
            'category': 'security',
            'description': 'Enable API rate limiting',
            'is_encrypted': False,
            'data_type': 'boolean'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'security.rate_limit_per_minute',
            'value': get_env('RATE_LIMIT_PER_MINUTE', '60'),
            'category': 'security',
            'description': 'Maximum API requests per minute per user',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'security.max_upload_size_mb',
            'value': get_env('MAX_UPLOAD_SIZE_MB', '10'),
            'category': 'security',
            'description': 'Maximum file upload size in MB',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Seed data: Token Settings
    token_settings = [
        {
            'id': str(uuid.uuid4()),
            'key': 'tokens.access_token_expire_minutes',
            'value': get_env('ACCESS_TOKEN_EXPIRE_MINUTES', '30'),
            'category': 'tokens',
            'description': 'JWT access token expiration (minutes)',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'tokens.refresh_token_expire_days',
            'value': get_env('REFRESH_TOKEN_EXPIRE_DAYS', '7'),
            'category': 'tokens',
            'description': 'JWT refresh token expiration (days)',
            'is_encrypted': False,
            'data_type': 'integer'
        },
        {
            'id': str(uuid.uuid4()),
            'key': 'tokens.pc_token_expire_hours',
            'value': get_env('JWT_PC_TOKEN_EXPIRE_HOURS', '8760'),
            'category': 'tokens',
            'description': 'PC authentication token expiration (hours, default: 1 year)',
            'is_encrypted': False,
            'data_type': 'integer'
        }
    ]

    # Combine all settings
    all_settings = (
        sureview_settings +
        smtp_settings +
        task_settings +
        snapshot_settings +
        websocket_settings +
        security_settings +
        token_settings
    )

    # Insert all settings
    if all_settings:
        op.bulk_insert(system_settings, all_settings)


def downgrade() -> None:
    """Remove system_settings table."""
    op.drop_index('idx_system_settings_category', table_name='system_settings')
    op.drop_index('idx_system_settings_key', table_name='system_settings')
    op.drop_table('system_settings')
