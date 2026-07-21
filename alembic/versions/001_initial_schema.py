"""Initial schema (pre-002) — explicit DDL.

Revision ID: 001_initial_schema
Revises:
Create Date: 2025-10-09

Historically this revision called ``Base.metadata.create_all()``, which made a
fresh database materialise whatever the ORM models looked like at run time —
including everything revisions 002..007 are supposed to add. The chain then
collided (``DuplicateColumn: column "full_name" of relation "users"``).

This revision now emits explicit DDL for the 14 tables that existed before
revision 002. It is derived from the production schema dump
(``experiments/site_device_refactor/baseline/prod_pre008.schema.sql``, taken at
revision 007) with everything 002..007 add subtracted:

  002 -> users.full_name
  003 -> sync_jobs (+ syncjobstatus enum)
  004 -> system_settings
  005 -> invitation_tokens.{email, role, invited_by_id} + FK + indexes,
         token_id -> id rename, user_id becomes nullable
  006 -> sites.use_tcp, cameras.use_tcp
  007 -> the 8 alerting tables and their monthly partitions

``app/models/screenshot.py`` is deliberately not represented: it is not imported
in ``app/models/__init__.py`` and no ``screenshots`` table exists in production.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the pre-002 baseline schema."""
    op.create_table('pcs',
    sa.Column('id', sa.VARCHAR(length=50), nullable=False, comment='Unique identifier for the PC'),
    sa.Column('name', sa.VARCHAR(length=255), nullable=False, comment='Display name of the PC'),
    sa.Column('ip_address', sa.VARCHAR(length=45), nullable=True, comment='IPv4 or IPv6 address of the PC'),
    sa.Column('gpu_type', sa.VARCHAR(length=100), nullable=True, comment='GPU type/model installed on the PC'),
    sa.Column('role', sa.VARCHAR(length=20), nullable=False, comment='PC role (controller or manager)'),
    sa.Column('manager_id', sa.VARCHAR(length=50), nullable=True, comment='ID of the managing PC (for controller PCs)'),
    sa.Column('auth_token', sa.TEXT(), nullable=True, comment='Authentication token for PC'),
    sa.Column('token_expiry', sa.BIGINT(), nullable=True, comment='Unix timestamp when authentication token expires'),
    sa.Column('last_connected', sa.BIGINT(), nullable=True, comment='Unix timestamp of last connection'),
    sa.Column('last_applied', sa.BIGINT(), nullable=True, comment='Unix timestamp of last configuration applied'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint("role::text = ANY (ARRAY['controller'::character varying::text, 'manager'::character varying::text])", name='check_pc_role'),
    sa.ForeignKeyConstraint(['manager_id'], ['pcs.id'], name='pcs_manager_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='pcs_pkey')
    )
    op.create_index('idx_pcs_last_connected', 'pcs', [sa.text('last_connected DESC')], unique=False)
    op.create_index('idx_pcs_manager_id', 'pcs', ['manager_id'], unique=False)
    op.create_index('idx_pcs_name', 'pcs', ['name'], unique=False)
    op.create_index('idx_pcs_role', 'pcs', ['role'], unique=False)
    op.create_table('site_categories',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='Unique category identifier'),
    sa.Column('name', sa.VARCHAR(length=100), nullable=False, comment='Unique category name'),
    sa.Column('color', sa.BIGINT(), nullable=False, comment='Color in 0xFFRRGGBBAA format'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.PrimaryKeyConstraint('id', name='site_categories_pkey')
    )
    op.create_index('idx_categories_name', 'site_categories', ['name'], unique=False)
    op.create_index('ix_site_categories_name', 'site_categories', ['name'], unique=True)
    op.create_table('sites',
    sa.Column('id', sa.VARCHAR(length=255), nullable=False, comment='Unique site identifier'),
    sa.Column('name', sa.VARCHAR(length=255), nullable=False, comment='Site name'),
    sa.Column('nvr_username', sa.VARCHAR(length=255), nullable=False, comment='Username for NVR access'),
    sa.Column('nvr_password', sa.TEXT(), nullable=False, comment='Encrypted password for NVR access'),
    sa.Column('sureview_site', sa.BOOLEAN(), nullable=False, comment='Whether this is a SureView-managed site'),
    sa.Column('new', sa.BOOLEAN(), nullable=False, comment='Whether this is a newly added site'),
    sa.Column('customer_id', sa.VARCHAR(length=50), nullable=True, comment='Customer ID from SureView (referenceId)'),
    sa.Column('address', sa.VARCHAR(length=500), nullable=True, comment='Physical address of the site'),
    sa.Column('telephone', sa.VARCHAR(length=255), nullable=True, comment='Primary contact telephone'),
    sa.Column('telephone2', sa.VARCHAR(length=255), nullable=True, comment='Secondary contact telephone'),
    sa.Column('telephone_police', sa.VARCHAR(length=100), nullable=True, comment='Police contact telephone'),
    sa.Column('telephone_fire', sa.VARCHAR(length=100), nullable=True, comment='Fire department contact telephone'),
    sa.Column('notes', sa.TEXT(), nullable=True, comment='Site notes and instructions'),
    sa.Column('lat_long', sa.VARCHAR(length=100), nullable=True, comment='Latitude and longitude coordinates'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.PrimaryKeyConstraint('id', name='sites_pkey')
    )
    op.create_index('idx_sites_created_at', 'sites', [sa.text('created_at DESC')], unique=False)
    op.create_index('idx_sites_customer_id', 'sites', ['customer_id'], unique=False)
    op.create_index('idx_sites_name', 'sites', ['name'], unique=False)
    op.create_index('idx_sites_sureview', 'sites', ['sureview_site'], unique=False)
    op.create_index('ix_sites_customer_id', 'sites', ['customer_id'], unique=False)
    op.create_index('ix_sites_name', 'sites', ['name'], unique=False)
    op.create_index('ix_sites_sureview_site', 'sites', ['sureview_site'], unique=False)
    op.create_table('users',
    sa.Column('user_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='Unique user identifier'),
    sa.Column('username', sa.VARCHAR(length=255), nullable=False, comment='Unique username for login'),
    sa.Column('email', sa.VARCHAR(length=255), nullable=False, comment='User email address'),
    sa.Column('password_hash', sa.VARCHAR(length=255), nullable=False, comment='Hashed password (bcrypt or Argon2)'),
    sa.Column('role', sa.VARCHAR(length=50), nullable=False, comment='User role: user, admin, or super_admin'),
    sa.Column('is_active', sa.BOOLEAN(), nullable=False, comment='Whether the user account is active'),
    sa.Column('created_by', sa.UUID(), nullable=True, comment='User who created this account'),
    sa.Column('last_login', postgresql.TIMESTAMP(timezone=True), nullable=True, comment='Timestamp of last successful login'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint("email::text ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}$'::text", name='email_format'),
    sa.CheckConstraint("role::text = ANY (ARRAY['user'::character varying::text, 'admin'::character varying::text, 'super_admin'::character varying::text])", name='valid_role'),
    sa.ForeignKeyConstraint(['created_by'], ['users.user_id'], name='users_created_by_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('user_id', name='users_pkey')
    )
    op.create_index('idx_users_email', 'users', ['email'], unique=False)
    op.create_index('idx_users_is_active', 'users', ['is_active'], unique=False)
    op.create_index('idx_users_last_login', 'users', [sa.text('last_login DESC')], unique=False)
    op.create_index('idx_users_role', 'users', ['role'], unique=False)
    op.create_index('idx_users_username', 'users', ['username'], unique=False)
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_index('ix_users_is_active', 'users', ['is_active'], unique=False)
    op.create_index('ix_users_last_login', 'users', ['last_login'], unique=False)
    op.create_index('ix_users_role', 'users', ['role'], unique=False)
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_table('audit_logs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='Unique audit log identifier'),
    sa.Column('user_id', sa.UUID(), nullable=True, comment='User who performed the action'),
    sa.Column('action', sa.VARCHAR(length=100), nullable=False, comment="Action identifier (e.g., 'site.created', 'user.updated')"),
    sa.Column('resource_type', sa.VARCHAR(length=50), nullable=False, comment='Type of resource affected'),
    sa.Column('resource_id', sa.VARCHAR(length=255), nullable=True, comment='ID of the affected resource'),
    sa.Column('changes', sa.VARCHAR(), nullable=True, comment='JSON object containing the changes made'),
    sa.Column('ip_address', sa.VARCHAR(length=45), nullable=True, comment='IP address of the user'),
    sa.Column('user_agent', sa.VARCHAR(), nullable=True, comment='User agent string from the request'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name='audit_logs_user_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name='audit_logs_pkey')
    )
    op.create_index('idx_audit_logs_action', 'audit_logs', ['action'], unique=False)
    op.create_index('idx_audit_logs_created_at', 'audit_logs', [sa.text('created_at DESC')], unique=False)
    op.create_index('idx_audit_logs_resource_type', 'audit_logs', ['resource_type'], unique=False)
    op.create_index('idx_audit_logs_user_id', 'audit_logs', ['user_id'], unique=False)
    op.create_index('ix_audit_logs_action', 'audit_logs', ['action'], unique=False)
    op.create_index('ix_audit_logs_resource_type', 'audit_logs', ['resource_type'], unique=False)
    op.create_index('ix_audit_logs_user_id', 'audit_logs', ['user_id'], unique=False)
    op.create_table('cameras',
    sa.Column('id', sa.VARCHAR(length=255), nullable=False, comment='Unique identifier for the camera'),
    sa.Column('site_id', sa.VARCHAR(length=255), nullable=False, comment='Site this camera belongs to (references sites.id)'),
    sa.Column('name', sa.VARCHAR(length=255), nullable=False, comment='Display name of the camera'),
    sa.Column('rtsp_url', sa.TEXT(), nullable=False, comment='RTSP URL for camera streaming (can be long)'),
    sa.Column('main_stream_url', sa.TEXT(), nullable=True, comment='Main stream URL for camera (optional)'),
    sa.Column('sureview_camera', sa.BOOLEAN(), server_default=sa.text('false'), nullable=False, comment='Flag indicating if this is a SureView integrated camera'),
    sa.Column('new', sa.BOOLEAN(), server_default=sa.text('true'), nullable=False, comment='Flag indicating if this is a newly added camera'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name='cameras_site_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='cameras_pkey')
    )
    op.create_index('idx_cameras_created_at', 'cameras', [sa.text('created_at DESC')], unique=False)
    op.create_index('idx_cameras_name', 'cameras', ['name'], unique=False)
    op.create_index('idx_cameras_site_id', 'cameras', ['site_id'], unique=False)
    op.create_index('ix_cameras_name', 'cameras', ['name'], unique=False)
    op.create_index('ix_cameras_site_id', 'cameras', ['site_id'], unique=False)
    op.create_table('invitation_tokens',
    sa.Column('token_id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False, comment='Unique invitation identifier'),
    sa.Column('token', sa.VARCHAR(length=255), nullable=False, comment='Unique invitation token string'),
    sa.Column('user_id', sa.UUID(), nullable=False, comment='User who registered with this token (set after registration)'),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), nullable=False, comment='Token expiration timestamp'),
    sa.Column('is_used', sa.BOOLEAN(), nullable=False, comment='Whether the token has been used'),
    sa.Column('used_at', postgresql.TIMESTAMP(timezone=True), nullable=True, comment='Timestamp when token was used'),
    sa.Column('used_from_ip', sa.VARCHAR(length=45), nullable=True, comment='IP address from which token was used'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint('expires_at > created_at', name='valid_invitation_expiration'),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], name='invitation_tokens_user_id_fkey', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('token_id', name='invitation_tokens_pkey')
    )
    op.create_index('idx_invitation_tokens_expires_at', 'invitation_tokens', ['expires_at'], unique=False)
    op.create_index('idx_invitation_tokens_is_used', 'invitation_tokens', ['is_used'], unique=False)
    op.create_index('idx_invitation_tokens_token', 'invitation_tokens', ['token'], unique=False)
    op.create_index('idx_invitation_tokens_user_id', 'invitation_tokens', ['user_id'], unique=False)
    op.create_index('ix_invitation_tokens_expires_at', 'invitation_tokens', ['expires_at'], unique=False)
    op.create_index('ix_invitation_tokens_is_used', 'invitation_tokens', ['is_used'], unique=False)
    op.create_index('ix_invitation_tokens_token', 'invitation_tokens', ['token'], unique=True)
    op.create_index('ix_invitation_tokens_user_id', 'invitation_tokens', ['user_id'], unique=False)
    op.create_table('screens',
    sa.Column('id', sa.VARCHAR(length=100), nullable=False, comment='Unique identifier for the screen'),
    sa.Column('pc_id', sa.VARCHAR(length=50), nullable=False, comment='ID of the PC this screen is connected to'),
    sa.Column('name', sa.VARCHAR(length=100), nullable=False, comment='Display name of the screen'),
    sa.Column('rows', sa.INTEGER(), nullable=False, comment='Number of rows in the screen grid (1-4)'),
    sa.Column('columns', sa.INTEGER(), nullable=False, comment='Number of columns in the screen grid (1-4)'),
    sa.Column('switching_interval', sa.INTEGER(), nullable=False, comment='Interval in seconds for view switching (minimum 1)'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint('columns >= 1 AND columns <= 4', name='check_screen_columns'),
    sa.CheckConstraint('rows >= 1 AND rows <= 4', name='check_screen_rows'),
    sa.CheckConstraint('switching_interval >= 1', name='check_switching_interval'),
    sa.ForeignKeyConstraint(['pc_id'], ['pcs.id'], name='screens_pc_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='screens_pkey')
    )
    op.create_index('idx_screens_pc_id', 'screens', ['pc_id'], unique=False)
    op.create_table('site_cameras_layout_config',
    sa.Column('site_id', sa.VARCHAR(length=255), nullable=False, comment='Unique identifier for the site (references sites.id)'),
    sa.Column('site_name', sa.VARCHAR(length=255), nullable=False, comment='Name of the site'),
    sa.Column('n_rows', sa.INTEGER(), nullable=False, comment='Number of rows in the layout grid (1-4)'),
    sa.Column('n_cols', sa.INTEGER(), nullable=False, comment='Number of columns in the layout grid (1-4)'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint('n_cols >= 1 AND n_cols <= 4', name='check_n_cols_valid'),
    sa.CheckConstraint('n_rows >= 1 AND n_rows <= 4', name='check_n_rows_valid'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name='site_cameras_layout_config_site_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('site_id', name='site_cameras_layout_config_pkey')
    )
    op.create_table('site_category_mappings',
    sa.Column('site_id', sa.VARCHAR(length=255), nullable=False, comment='Site identifier'),
    sa.Column('category_id', sa.UUID(), nullable=False, comment='Category identifier'),
    sa.Column('assigned_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False, comment='Timestamp when the mapping was created'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.ForeignKeyConstraint(['category_id'], ['site_categories.id'], name='site_category_mappings_category_id_fkey', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name='site_category_mappings_site_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('site_id', 'category_id', name='site_category_mappings_pkey')
    )
    op.create_index('idx_mappings_category', 'site_category_mappings', ['category_id'], unique=False)
    op.create_index('idx_mappings_site', 'site_category_mappings', ['site_id'], unique=False)
    op.create_table('site_cameras_layout',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='Auto-incrementing primary key'),
    sa.Column('site_id', sa.VARCHAR(length=255), nullable=False, comment='Unique identifier for the site (references sites.id)'),
    sa.Column('site_name', sa.VARCHAR(length=255), nullable=False, comment='Name of the site'),
    sa.Column('slot_row', sa.INTEGER(), nullable=False, comment='Row position in the grid (1-indexed)'),
    sa.Column('slot_col', sa.INTEGER(), nullable=False, comment='Column position in the grid (1-indexed)'),
    sa.Column('camera_id', sa.VARCHAR(length=255), nullable=False, comment='Unique identifier for the camera (references cameras.id)'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint('slot_col >= 1', name='check_slot_col_positive'),
    sa.CheckConstraint('slot_row >= 1', name='check_slot_row_positive'),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name='fk_site_cameras_layout_camera', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name='fk_site_cameras_layout_site', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='site_cameras_layout_pkey'),
    sa.UniqueConstraint('site_id', 'slot_row', 'slot_col', name='uq_site_cameras_layout_slot')
    )
    op.create_index('idx_site_cameras_layout_camera', 'site_cameras_layout', ['camera_id'], unique=False)
    op.create_index('idx_site_cameras_layout_site', 'site_cameras_layout', ['site_id'], unique=False)
    op.create_table('snapshots',
    sa.Column('camera_id', sa.VARCHAR(length=255), nullable=False, comment='Camera ID this snapshot belongs to (references cameras.id)'),
    sa.Column('image', postgresql.BYTEA(), nullable=False, comment='Binary image data (BYTEA in PostgreSQL)'),
    sa.Column('width', sa.INTEGER(), nullable=False, comment='Image width in pixels'),
    sa.Column('height', sa.INTEGER(), nullable=False, comment='Image height in pixels'),
    sa.Column('capture_time', sa.BIGINT(), nullable=False, comment='Unix timestamp when snapshot was captured'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name='snapshots_camera_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('camera_id', name='snapshots_pkey')
    )
    op.create_index('idx_snapshots_capture_time', 'snapshots', [sa.text('capture_time DESC')], unique=False)
    op.create_index('ix_snapshots_capture_time', 'snapshots', ['capture_time'], unique=False)
    op.create_table('views',
    sa.Column('id', sa.VARCHAR(length=255), nullable=False, comment='Unique identifier for the view'),
    sa.Column('screen_id', sa.VARCHAR(length=100), nullable=False, comment='ID of the screen this view belongs to'),
    sa.Column('name', sa.VARCHAR(length=50), nullable=False, comment='Display name of the view'),
    sa.Column('layout_rows', sa.INTEGER(), nullable=False, comment='Number of rows in the view layout grid (1-10)'),
    sa.Column('layout_columns', sa.INTEGER(), nullable=False, comment='Number of columns in the view layout grid (1-10)'),
    sa.Column('view_number', sa.INTEGER(), nullable=False, comment='Sequential number of this view on the screen'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.CheckConstraint('layout_columns >= 1 AND layout_columns <= 10', name='check_view_layout_columns'),
    sa.CheckConstraint('layout_rows >= 1 AND layout_rows <= 10', name='check_view_layout_rows'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='views_screen_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='views_pkey'),
    sa.UniqueConstraint('screen_id', 'view_number', name='uq_screen_view_number')
    )
    op.create_index('idx_views_screen_id', 'views', ['screen_id'], unique=False)
    op.create_index('idx_views_view_number', 'views', ['view_number'], unique=False)
    op.create_table('screen_mappings',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='Auto-incrementing primary key'),
    sa.Column('pc_id', sa.VARCHAR(length=50), nullable=False, comment='ID of the PC'),
    sa.Column('screen_id', sa.VARCHAR(length=100), nullable=False, comment='ID of the screen'),
    sa.Column('view_id', sa.VARCHAR(length=255), nullable=False, comment='ID of the view'),
    sa.Column('slot_row', sa.INTEGER(), nullable=False, comment='Row position in the grid (1-indexed)'),
    sa.Column('slot_col', sa.INTEGER(), nullable=False, comment='Column position in the grid (1-indexed)'),
    sa.Column('site_id', sa.VARCHAR(length=255), nullable=True, comment='ID of the site'),
    sa.Column('camera_id', sa.VARCHAR(length=255), nullable=True, comment='ID of the camera'),
    sa.Column('playing_state', sa.BOOLEAN(), nullable=False, comment='Whether this camera is currently playing'),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was created'),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False, comment='Timestamp when the record was last updated'),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], name='screen_mappings_camera_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['pc_id'], ['pcs.id'], name='screen_mappings_pc_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='screen_mappings_screen_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['site_id'], ['sites.id'], name='screen_mappings_site_id_fkey', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['view_id'], ['views.id'], name='screen_mappings_view_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='screen_mappings_pkey'),
    sa.UniqueConstraint('view_id', 'slot_row', 'slot_col', name='uq_screen_mapping_slot')
    )
    op.create_index('idx_screen_mappings_camera', 'screen_mappings', ['camera_id'], unique=False)
    op.create_index('idx_screen_mappings_pc', 'screen_mappings', ['pc_id'], unique=False)
    op.create_index('idx_screen_mappings_screen', 'screen_mappings', ['screen_id'], unique=False)
    op.create_index('idx_screen_mappings_site', 'screen_mappings', ['site_id'], unique=False)
    op.create_index('idx_screen_mappings_view', 'screen_mappings', ['view_id'], unique=False)
    # ### end Alembic commands ###



def downgrade() -> None:
    """Drop the pre-002 baseline schema (reverse dependency order)."""
    op.drop_table('screen_mappings')
    op.drop_table('views')
    op.drop_table('snapshots')
    op.drop_table('site_cameras_layout')
    op.drop_table('site_category_mappings')
    op.drop_table('site_cameras_layout_config')
    op.drop_table('screens')
    op.drop_table('invitation_tokens')
    op.drop_table('cameras')
    op.drop_table('audit_logs')
    op.drop_table('users')
    op.drop_table('sites')
    op.drop_table('site_categories')
    op.drop_table('pcs')
