"""Retire the SureView integration: drop sync_jobs and the sureview flags.

Revision ID: 009_retire_sureview
Revises: 008_site_device_hierarchy
Create Date: 2026-07-21

Removes the last database-schema traces of the SureView integration, now that
the dedicated service/task/API modules that drove it are being deleted
alongside this migration:

- Drops the ``sync_jobs`` table (27,456 rows in production) and, since
  PostgreSQL does not drop enum types along with a table that used them, the
  ``syncjobstatus`` enum type it depended on.
- Drops ``devices.sureview_site`` (renamed from ``sites.sureview_site`` by
  008; provenance flag for the 74/90 devices SureView discovered).
- Drops ``cameras.sureview_camera`` (provenance flag for the 336/481 cameras
  SureView discovered).

The equipment itself (the Device and Camera rows) is real inventory and is
**not** touched — only the flags recording how it arrived, and the sync
machinery that produced them.

``devices.sureview_site`` carries two indexes, ``idx_devices_sureview`` and
``ix_devices_sureview_site`` (renamed from ``idx_sites_sureview`` /
``ix_sites_sureview_site`` by 008). Both index only that column, so
PostgreSQL drops them automatically when the column is dropped — this
migration does not also issue an explicit ``DROP INDEX`` for them, which
would error ("index does not exist") once the column drop has already
removed them.

Downgrade lossiness
--------------------
The data in the dropped column/table is **not recoverable**. ``downgrade()``
recreates the ``sync_jobs`` table (empty) and the ``syncjobstatus`` enum
exactly as 003 defined them, and recreates both boolean columns and their
indexes — but every recreated row's ``sureview_site`` / ``sureview_camera``
value is reset to ``false`` for every existing Device/Camera, since the
original per-row provenance values are gone. This is the best available
answer, not a bit-reversible undo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '009_retire_sureview'
down_revision: Union[str, None] = '008_site_device_hierarchy'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop sync_jobs (+ its enum) and the two sureview provenance columns."""
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # ------------------------------------------------------------------
    # sync_jobs table + syncjobstatus enum.
    # ------------------------------------------------------------------
    if 'sync_jobs' in insp.get_table_names():
        op.drop_table('sync_jobs')
    else:
        print('009: sync_jobs already absent; skipping drop_table.')

    # DROP TABLE does not drop the enum type it referenced; IF EXISTS makes
    # this safe to re-run whether or not the table drop above already ran.
    op.execute('DROP TYPE IF EXISTS syncjobstatus')

    # ------------------------------------------------------------------
    # devices.sureview_site (+ idx_devices_sureview / ix_devices_sureview_site,
    # both dropped automatically by PostgreSQL along with the column).
    # ------------------------------------------------------------------
    device_cols = {c['name'] for c in insp.get_columns('devices')} if 'devices' in insp.get_table_names() else set()
    if 'sureview_site' in device_cols:
        op.drop_column('devices', 'sureview_site')
    else:
        print('009: devices.sureview_site already absent; skipping drop_column.')

    # ------------------------------------------------------------------
    # cameras.sureview_camera (no index — never had one).
    # ------------------------------------------------------------------
    camera_cols = {c['name'] for c in insp.get_columns('cameras')} if 'cameras' in insp.get_table_names() else set()
    if 'sureview_camera' in camera_cols:
        op.drop_column('cameras', 'sureview_camera')
    else:
        print('009: cameras.sureview_camera already absent; skipping drop_column.')

    # ------------------------------------------------------------------
    # Seeded SureView settings rows.
    #
    # Migration 004 seeds five `sureview.*` rows into system_settings, and it
    # is historical -- 005 chains off it, so it cannot be edited. Without this
    # DELETE the rows survive here AND get recreated on every fresh database,
    # leaving a SureView settings category (still editable by a super-admin
    # via GET /api/v1/settings) on a system with no SureView integration.
    # `category` is an unvalidated str, so nothing errors -- it fails by
    # quietly continuing to work. Correct it forward, here.
    # ------------------------------------------------------------------
    if 'system_settings' in insp.get_table_names():
        res = op.get_bind().execute(
            sa.text("DELETE FROM system_settings WHERE key LIKE 'sureview%'")
        )
        print(f'009: removed {res.rowcount} seeded sureview.* system_settings row(s).')

        # 004:210 seeds tasks.sync_interval_seconds with a SureView-worded
        # description. That key is absent from the current production DB (the
        # Python seeder produced a different set), so this is a no-op there --
        # but it DOES get seeded on any fresh database built from migrations,
        # where the value drives only the snapshot task. Guarded by rowcount,
        # harmless either way.
        op.get_bind().execute(sa.text(
            "UPDATE system_settings "
            "SET description = 'Interval in seconds for background tasks (default: 600 = 10 minutes)' "
            "WHERE key = 'tasks.sync_interval_seconds'"
        ))


def downgrade() -> None:
    """Recreate sync_jobs, syncjobstatus, and both sureview columns + indexes.

    Data is NOT recoverable: sync_jobs comes back empty, and both provenance
    columns are backfilled to ``false`` for every existing row rather than
    their original per-row values, which no longer exist anywhere.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    table_names = insp.get_table_names()

    # ------------------------------------------------------------------
    # syncjobstatus enum (same guarded DO block 003 used).
    # ------------------------------------------------------------------
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE syncjobstatus AS ENUM ('pending', 'in_progress', 'completed', 'failed');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # ------------------------------------------------------------------
    # sync_jobs table — matches 003_add_sync_jobs_table.py exactly.
    # ------------------------------------------------------------------
    if 'sync_jobs' not in table_names:
        op.create_table(
            'sync_jobs',
            sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, comment='Unique identifier for the sync job (UUID)'),
            sa.Column('status', postgresql.ENUM('pending', 'in_progress', 'completed', 'failed', name='syncjobstatus', create_type=False),
                      nullable=False, comment='Current status of the sync job'),
            sa.Column('progress', sa.Integer(), nullable=False,
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
                      comment='User who triggered the sync (NULL for system-triggered)'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                      nullable=False, comment='Timestamp when the record was created'),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                      nullable=False, comment='Timestamp when the record was last updated'),
            sa.PrimaryKeyConstraint('id'),
            sa.ForeignKeyConstraint(['triggered_by'], ['users.user_id'], ondelete='SET NULL')
        )
    else:
        print('009 downgrade: sync_jobs already present; skipping create_table.')

    # ------------------------------------------------------------------
    # devices.sureview_site + idx_devices_sureview + ix_devices_sureview_site.
    # Original column is NOT NULL with no server_default; the table is
    # non-empty, so add nullable first, backfill false, then lock NOT NULL —
    # same two-step pattern 008 used for devices.site_id.
    # ------------------------------------------------------------------
    device_cols = {c['name'] for c in insp.get_columns('devices')} if 'devices' in table_names else set()
    if 'sureview_site' not in device_cols:
        op.add_column(
            'devices',
            sa.Column('sureview_site', sa.Boolean(), nullable=True,
                      comment='Whether this is a SureView-managed device'),
        )
        op.execute('UPDATE devices SET sureview_site = false WHERE sureview_site IS NULL')
        op.alter_column('devices', 'sureview_site', nullable=False)
        op.create_index('idx_devices_sureview', 'devices', ['sureview_site'])
        op.create_index('ix_devices_sureview_site', 'devices', ['sureview_site'])
    else:
        print('009 downgrade: devices.sureview_site already present; skipping.')

    # ------------------------------------------------------------------
    # cameras.sureview_camera — original has server_default=false, so a
    # single-step add_column is sufficient even on a non-empty table.
    # ------------------------------------------------------------------
    camera_cols = {c['name'] for c in insp.get_columns('cameras')} if 'cameras' in table_names else set()
    if 'sureview_camera' not in camera_cols:
        op.add_column(
            'cameras',
            sa.Column('sureview_camera', sa.Boolean(), server_default=sa.text('false'), nullable=False,
                      comment='Flag indicating if this is a SureView integrated camera'),
        )
    else:
        print('009 downgrade: cameras.sureview_camera already present; skipping.')

    # ------------------------------------------------------------------
    # Seeded SureView settings rows, restored for symmetry.
    #
    # Values come back EMPTY rather than repopulated from the environment:
    # 004 seeded them via get_env('SUREVIEW_*'), and those variables no
    # longer exist. That matches the observed production state anyway --
    # username/password/api_url were all empty, so nothing is lost.
    # ------------------------------------------------------------------
    if 'system_settings' in table_names:
        for key, description, encrypted in (
            ('sureview.username', 'SureView API username for authentication', False),
            ('sureview.password', 'SureView API password (will be encrypted)', True),
            ('sureview.api_url', 'SureView API base URL', False),
            ('sureview.login_url', 'SureView login page URL for Selenium', False),
        ):
            op.get_bind().execute(
                sa.text(
                    "INSERT INTO system_settings "
                    "(id, key, value, category, description, is_encrypted, data_type) "
                    "VALUES (gen_random_uuid()::text, :k, '', 'sureview', :d, :e, 'string') "
                    "ON CONFLICT (key) DO NOTHING"
                ),
                {'k': key, 'd': description, 'e': encrypted},
            )
        op.get_bind().execute(sa.text(
            "UPDATE system_settings "
            "SET description = 'Interval in seconds for SureView sync (default: 600 = 10 minutes)' "
            "WHERE key = 'tasks.sync_interval_seconds'"
        ))
