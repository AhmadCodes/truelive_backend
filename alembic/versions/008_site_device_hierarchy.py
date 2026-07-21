"""Site -> Device hierarchy.

Revision ID: 008_site_device_hierarchy
Revises: 007_alerting_feature_tables
Create Date: 2026-07-21

Renames the existing ``sites`` table (which actually modelled one NVR/DVR) to
``devices``, creates a brand-new ``sites`` parent table holding the location /
contact columns, and backfills a strictly 1:1 parent Site for every Device.
The five child tables keep pointing at the Device with their ``site_id`` column
renamed to ``device_id``.

Order is load-bearing. PostgreSQL keeps tables, indexes and constraints in one
``pg_class`` namespace, so every ``sites*``-named relation on the renamed table
must be renamed or dropped before ``CREATE TABLE sites`` can succeed.

A snapshot table ``_pre008_sites`` is created before any DDL. It is deliberately
**not** dropped — the acceptance checks (AC-4 / AC-7) compare the migrated data
against it. Drop it manually once verification is done.

Downgrade lossiness
-------------------
``downgrade()`` copies the location columns back down from the Site onto each of
its Devices. That is exact only while the mapping is still 1:1. If an operator
has reparented devices so that two Devices share one Site, the copy-back writes
the same location onto both — which is the pre-008 semantics and the only
sensible answer, but it is not bit-reversible. A Site left with zero Devices is
simply dropped.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '008_site_device_hierarchy'
down_revision: Union[str, None] = '007_alerting_feature_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The 8 location / contact columns that move up from the device to its site.
_LOCATION_COLUMNS = (
    ('customer_id', sa.String(50)),
    ('address', sa.String(500)),
    ('telephone', sa.String(255)),
    ('telephone2', sa.String(255)),
    ('telephone_police', sa.String(100)),
    ('telephone_fire', sa.String(100)),
    ('notes', sa.Text()),
    ('lat_long', sa.String(100)),
)

_LOCATION_NAMES = tuple(name for name, _ in _LOCATION_COLUMNS)

# Deterministic parent-site id derived from the device id (see A-5 in the spec).
_SITE_ID_EXPR = "'SITE_' || upper(substr(md5(d.id), 1, 12))"

# ALTER TABLE ... RENAME COLUMN / RENAME TABLE both carry the old column
# comments over. Restate them so the live schema matches the ORM models and
# `--autogenerate` reports no comment drift.
_COMMENTS_TARGET = (
    "COMMENT ON COLUMN devices.id IS 'Unique device identifier'",
    "COMMENT ON COLUMN devices.name IS 'Device name'",
    "COMMENT ON COLUMN devices.sureview_site IS 'Whether this is a SureView-managed device'",
    "COMMENT ON COLUMN devices.new IS 'Whether this is a newly added device'",
    "COMMENT ON COLUMN devices.use_tcp IS 'Device-wide default for RTSP TCP transport"
    " (overridable per camera)'",
    "COMMENT ON COLUMN cameras.device_id IS 'Device this camera belongs to"
    " (references devices.id)'",
    "COMMENT ON COLUMN cameras.use_tcp IS 'Per-camera RTSP TCP override: NULL inherits"
    " device.use_tcp, true/false overrides'",
    "COMMENT ON COLUMN site_category_mappings.device_id IS 'Device identifier'",
    "COMMENT ON COLUMN screen_mappings.device_id IS 'ID of the device'",
    "COMMENT ON COLUMN site_cameras_layout_config.device_id IS 'Unique identifier for the"
    " device (references devices.id)'",
    "COMMENT ON COLUMN site_cameras_layout_config.device_name IS 'Name of the device'",
    "COMMENT ON COLUMN site_cameras_layout.device_id IS 'Unique identifier for the device"
    " (references devices.id)'",
    "COMMENT ON COLUMN site_cameras_layout.device_name IS 'Name of the device'",
)

_COMMENTS_LEGACY = (
    "COMMENT ON COLUMN sites.id IS 'Unique site identifier'",
    "COMMENT ON COLUMN sites.name IS 'Site name'",
    "COMMENT ON COLUMN sites.sureview_site IS 'Whether this is a SureView-managed site'",
    "COMMENT ON COLUMN sites.new IS 'Whether this is a newly added site'",
    "COMMENT ON COLUMN sites.use_tcp IS 'Site-wide default: force RTSP over TCP for all"
    " cameras unless camera overrides'",
    "COMMENT ON COLUMN cameras.site_id IS 'Site this camera belongs to (references sites.id)'",
    "COMMENT ON COLUMN cameras.use_tcp IS 'Per-camera override: NULL inherits site.use_tcp,"
    " true/false overrides'",
    "COMMENT ON COLUMN site_category_mappings.site_id IS 'Site identifier'",
    "COMMENT ON COLUMN screen_mappings.site_id IS 'ID of the site'",
    "COMMENT ON COLUMN site_cameras_layout_config.site_id IS 'Unique identifier for the site"
    " (references sites.id)'",
    "COMMENT ON COLUMN site_cameras_layout_config.site_name IS 'Name of the site'",
    "COMMENT ON COLUMN site_cameras_layout.site_id IS 'Unique identifier for the site"
    " (references sites.id)'",
    "COMMENT ON COLUMN site_cameras_layout.site_name IS 'Name of the site'",
)


def _shape(bind) -> str:
    """Classify the live schema as 'legacy', 'target' or 'unknown'."""
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    site_cols = {c['name'] for c in insp.get_columns('sites')} if 'sites' in tables else set()
    dev_cols = {c['name'] for c in insp.get_columns('devices')} if 'devices' in tables else set()

    if 'devices' in tables:
        if (
            'site_id' in dev_cols
            and 'nvr_username' in dev_cols
            and 'nvr_username' not in site_cols
            and 'address' in site_cols
        ):
            return 'target'
        return 'unknown'

    if 'sites' in tables and 'nvr_username' in site_cols and 'address' in site_cols:
        return 'legacy'

    return 'unknown'


def upgrade() -> None:
    bind = op.get_bind()
    shape = _shape(bind)

    if shape == 'target':
        print('008: schema already in target (Site->Device) shape; nothing to do.')
        return
    if shape != 'legacy':
        raise RuntimeError(
            "008_site_device_hierarchy: refusing to run — the live schema is "
            "neither the legacy (sites-as-NVR) nor the target (sites+devices) "
            "shape. Inspect the database manually before retrying."
        )

    # ------------------------------------------------------------------
    # Snapshot — kept for post-migration verification (AC-4 / AC-7).
    # ------------------------------------------------------------------
    op.execute('DROP TABLE IF EXISTS _pre008_sites')
    op.execute('CREATE TABLE _pre008_sites AS SELECT * FROM sites')

    # ------------------------------------------------------------------
    # Phase A — rename the table and free every `sites*` relation name.
    # ------------------------------------------------------------------
    op.rename_table('sites', 'devices')
    # Renaming a constraint also renames its backing index.
    op.execute('ALTER TABLE devices RENAME CONSTRAINT sites_pkey TO devices_pkey')
    op.execute('ALTER INDEX idx_sites_name RENAME TO idx_devices_name')
    op.execute('ALTER INDEX idx_sites_sureview RENAME TO idx_devices_sureview')
    op.execute('ALTER INDEX idx_sites_created_at RENAME TO idx_devices_created_at')
    op.execute('ALTER INDEX ix_sites_name RENAME TO ix_devices_name')
    op.execute('ALTER INDEX ix_sites_sureview_site RENAME TO ix_devices_sureview_site')
    # customer_id leaves `devices` in Phase E, so its indexes are dropped.
    op.execute('DROP INDEX idx_sites_customer_id')
    op.execute('DROP INDEX ix_sites_customer_id')

    # ------------------------------------------------------------------
    # Phase B — create the new parent `sites` table.
    # ------------------------------------------------------------------
    op.create_table(
        'sites',
        sa.Column('id', sa.String(255), nullable=False, comment='Unique site identifier'),
        sa.Column('name', sa.String(255), nullable=False, comment='Site name'),
        sa.Column('customer_id', sa.String(50), nullable=True,
                  comment='Customer ID from SureView (referenceId)'),
        sa.Column('address', sa.String(500), nullable=True,
                  comment='Physical address of the site'),
        sa.Column('telephone', sa.String(255), nullable=True,
                  comment='Primary contact telephone'),
        sa.Column('telephone2', sa.String(255), nullable=True,
                  comment='Secondary contact telephone'),
        sa.Column('telephone_police', sa.String(100), nullable=True,
                  comment='Police contact telephone'),
        sa.Column('telephone_fire', sa.String(100), nullable=True,
                  comment='Fire department contact telephone'),
        sa.Column('notes', sa.Text(), nullable=True,
                  comment='Site notes and instructions'),
        sa.Column('lat_long', sa.String(100), nullable=True,
                  comment='Latitude and longitude coordinates'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False,
                  comment='Timestamp when the record was created'),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False,
                  comment='Timestamp when the record was last updated'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sites_name', 'sites', ['name'])
    op.create_index('ix_sites_customer_id', 'sites', ['customer_id'])
    op.create_index('idx_sites_name', 'sites', ['name'])
    op.create_index('idx_sites_customer_id', 'sites', ['customer_id'])
    op.execute('CREATE INDEX idx_sites_created_at ON sites (created_at DESC)')

    # ------------------------------------------------------------------
    # Phase C — add devices.site_id (nullable) and backfill 1:1.
    # ------------------------------------------------------------------
    op.add_column(
        'devices',
        sa.Column('site_id', sa.String(255), nullable=True,
                  comment='Site this device belongs to (references sites.id)'),
    )

    op.execute(f"""
        INSERT INTO sites (id, name, customer_id, address, telephone, telephone2,
                           telephone_police, telephone_fire, notes, lat_long,
                           created_at, updated_at)
        SELECT {_SITE_ID_EXPR},
               d.name, d.customer_id, d.address, d.telephone, d.telephone2,
               d.telephone_police, d.telephone_fire, d.notes, d.lat_long,
               d.created_at, d.updated_at
        FROM devices d
    """)

    op.execute(f"UPDATE devices d SET site_id = {_SITE_ID_EXPR}")

    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM devices d
             WHERE d.site_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.id = d.site_id);
            IF n > 0 THEN
                RAISE EXCEPTION '008 backfill left % unparented device(s)', n;
            END IF;

            IF (SELECT count(*) FROM sites) <> (SELECT count(*) FROM devices) THEN
                RAISE EXCEPTION
                    '008 backfill is not 1:1 (sites=%, devices=%)',
                    (SELECT count(*) FROM sites), (SELECT count(*) FROM devices);
            END IF;
        END $$;
    """)

    # ------------------------------------------------------------------
    # Phase D — lock the parent relationship down.
    # ------------------------------------------------------------------
    op.alter_column('devices', 'site_id', existing_type=sa.String(255), nullable=False)
    op.create_foreign_key(
        'devices_site_id_fkey', 'devices', 'sites',
        ['site_id'], ['id'], ondelete='CASCADE',
    )
    op.create_index('ix_devices_site_id', 'devices', ['site_id'])
    op.create_index('idx_devices_site_id', 'devices', ['site_id'])

    # ------------------------------------------------------------------
    # Phase E — drop the location columns from devices.
    # ------------------------------------------------------------------
    for name in _LOCATION_NAMES:
        op.drop_column('devices', name)

    # ------------------------------------------------------------------
    # Phase F — rename the child columns / constraints / indexes.
    # RENAME COLUMN is catalog-only and rewrites PK / unique definitions in
    # place; no constraint is dropped or recreated.
    # ------------------------------------------------------------------
    op.alter_column('cameras', 'site_id', new_column_name='device_id')
    op.execute('ALTER INDEX idx_cameras_site_id RENAME TO idx_cameras_device_id')
    op.execute('ALTER INDEX ix_cameras_site_id RENAME TO ix_cameras_device_id')
    op.execute('ALTER TABLE cameras RENAME CONSTRAINT cameras_site_id_fkey '
               'TO cameras_device_id_fkey')

    op.alter_column('site_category_mappings', 'site_id', new_column_name='device_id')
    op.execute('ALTER INDEX idx_mappings_site RENAME TO idx_mappings_device')
    op.execute('ALTER TABLE site_category_mappings RENAME CONSTRAINT '
               'site_category_mappings_site_id_fkey TO '
               'site_category_mappings_device_id_fkey')

    op.alter_column('screen_mappings', 'site_id', new_column_name='device_id')
    op.execute('ALTER INDEX idx_screen_mappings_site RENAME TO idx_screen_mappings_device')
    op.execute('ALTER TABLE screen_mappings RENAME CONSTRAINT '
               'screen_mappings_site_id_fkey TO screen_mappings_device_id_fkey')

    op.alter_column('site_cameras_layout_config', 'site_id', new_column_name='device_id')
    op.alter_column('site_cameras_layout_config', 'site_name', new_column_name='device_name')
    op.execute('ALTER TABLE site_cameras_layout_config RENAME CONSTRAINT '
               'site_cameras_layout_config_site_id_fkey TO '
               'site_cameras_layout_config_device_id_fkey')

    op.alter_column('site_cameras_layout', 'site_id', new_column_name='device_id')
    op.alter_column('site_cameras_layout', 'site_name', new_column_name='device_name')
    op.execute('ALTER TABLE site_cameras_layout RENAME CONSTRAINT '
               'fk_site_cameras_layout_site TO fk_site_cameras_layout_device')
    # Renames the backing index of the same name along with the constraint.
    op.execute('ALTER TABLE site_cameras_layout RENAME CONSTRAINT '
               'uq_site_cameras_layout_slot TO uq_site_cameras_layout_device_slot')
    op.execute('ALTER INDEX idx_site_cameras_layout_site '
               'RENAME TO idx_site_cameras_layout_device')

    for stmt in _COMMENTS_TARGET:
        op.execute(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    shape = _shape(bind)

    if shape == 'legacy':
        print('008: schema already in legacy shape; nothing to undo.')
        return
    if shape != 'target':
        raise RuntimeError(
            "008_site_device_hierarchy: refusing to downgrade — the live schema "
            "is neither the target nor the legacy shape."
        )

    # ------------------------------------------------------------------
    # Reverse Phase F.
    # ------------------------------------------------------------------
    op.execute('ALTER INDEX idx_site_cameras_layout_device '
               'RENAME TO idx_site_cameras_layout_site')
    op.execute('ALTER TABLE site_cameras_layout RENAME CONSTRAINT '
               'uq_site_cameras_layout_device_slot TO uq_site_cameras_layout_slot')
    op.execute('ALTER TABLE site_cameras_layout RENAME CONSTRAINT '
               'fk_site_cameras_layout_device TO fk_site_cameras_layout_site')
    op.alter_column('site_cameras_layout', 'device_name', new_column_name='site_name')
    op.alter_column('site_cameras_layout', 'device_id', new_column_name='site_id')

    op.execute('ALTER TABLE site_cameras_layout_config RENAME CONSTRAINT '
               'site_cameras_layout_config_device_id_fkey TO '
               'site_cameras_layout_config_site_id_fkey')
    op.alter_column('site_cameras_layout_config', 'device_name', new_column_name='site_name')
    op.alter_column('site_cameras_layout_config', 'device_id', new_column_name='site_id')

    op.execute('ALTER TABLE screen_mappings RENAME CONSTRAINT '
               'screen_mappings_device_id_fkey TO screen_mappings_site_id_fkey')
    op.execute('ALTER INDEX idx_screen_mappings_device RENAME TO idx_screen_mappings_site')
    op.alter_column('screen_mappings', 'device_id', new_column_name='site_id')

    op.execute('ALTER TABLE site_category_mappings RENAME CONSTRAINT '
               'site_category_mappings_device_id_fkey TO '
               'site_category_mappings_site_id_fkey')
    op.execute('ALTER INDEX idx_mappings_device RENAME TO idx_mappings_site')
    op.alter_column('site_category_mappings', 'device_id', new_column_name='site_id')

    op.execute('ALTER TABLE cameras RENAME CONSTRAINT cameras_device_id_fkey '
               'TO cameras_site_id_fkey')
    op.execute('ALTER INDEX ix_cameras_device_id RENAME TO ix_cameras_site_id')
    op.execute('ALTER INDEX idx_cameras_device_id RENAME TO idx_cameras_site_id')
    op.alter_column('cameras', 'device_id', new_column_name='site_id')

    # ------------------------------------------------------------------
    # Reverse Phase E — re-add the location columns and copy the data back
    # down from the parent Site. Lossy if devices were reparented (see the
    # module docstring).
    # ------------------------------------------------------------------
    for name, type_ in _LOCATION_COLUMNS:
        op.add_column('devices', sa.Column(name, type_, nullable=True))

    op.execute("""
        UPDATE devices d
           SET customer_id      = s.customer_id,
               address          = s.address,
               telephone        = s.telephone,
               telephone2       = s.telephone2,
               telephone_police = s.telephone_police,
               telephone_fire   = s.telephone_fire,
               notes            = s.notes,
               lat_long         = s.lat_long
          FROM sites s
         WHERE s.id = d.site_id
    """)

    # ------------------------------------------------------------------
    # Reverse Phase D / C.
    # ------------------------------------------------------------------
    op.drop_index('idx_devices_site_id', table_name='devices')
    op.drop_index('ix_devices_site_id', table_name='devices')
    op.drop_constraint('devices_site_id_fkey', 'devices', type_='foreignkey')
    op.drop_column('devices', 'site_id')

    # ------------------------------------------------------------------
    # Reverse Phase B — drop the parent table, freeing the `sites*` namespace
    # again (its PK and indexes go with it).
    # ------------------------------------------------------------------
    op.drop_table('sites')

    # ------------------------------------------------------------------
    # Reverse Phase A.
    # ------------------------------------------------------------------
    op.rename_table('devices', 'sites')
    op.execute('ALTER TABLE sites RENAME CONSTRAINT devices_pkey TO sites_pkey')
    op.execute('ALTER INDEX idx_devices_name RENAME TO idx_sites_name')
    op.execute('ALTER INDEX idx_devices_sureview RENAME TO idx_sites_sureview')
    op.execute('ALTER INDEX idx_devices_created_at RENAME TO idx_sites_created_at')
    op.execute('ALTER INDEX ix_devices_name RENAME TO ix_sites_name')
    op.execute('ALTER INDEX ix_devices_sureview_site RENAME TO ix_sites_sureview_site')
    op.create_index('idx_sites_customer_id', 'sites', ['customer_id'])
    op.create_index('ix_sites_customer_id', 'sites', ['customer_id'])

    for stmt in _COMMENTS_LEGACY:
        op.execute(stmt)
