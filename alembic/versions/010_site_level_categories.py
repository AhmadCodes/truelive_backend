"""Move site categories and camera layouts up from the Device to the Site.

Revision ID: 010_site_level_categories
Revises: 009_retire_sureview
Create Date: 2026-07-21

Migration 008 pushed ``site_category_mappings``, ``site_cameras_layout_config``
and ``site_cameras_layout`` down onto the new ``devices`` table along with
everything else that used to hang off the old sites-as-NVR table. That was one
level too low: a category describes a *place*, and its OSD colour must apply to
every camera at that place regardless of which recorder the camera hangs off;
likewise a site's camera grid must be able to draw from any camera on any
device belonging to that site. Cameras themselves stay on the Device — a
camera physically belongs to a recorder.

This migration re-points all three tables at ``sites.id`` via each row's
device's parent site::

    UPDATE <t> SET site_id = (SELECT d.site_id FROM devices d WHERE d.id = <t>.device_id)

Two of the three tables need real primary-key surgery, not a rename:

- ``site_category_mappings`` — ``device_id`` is a **composite PK member**;
  ``(device_id, category_id)`` becomes ``(site_id, category_id)``.
- ``site_cameras_layout_config`` — ``device_id`` **is** the whole PK; it
  becomes ``(site_id)``.
- ``site_cameras_layout`` — PK stays on the surrogate ``id``, but the unique
  constraint ``(device_id, slot_row, slot_col)`` becomes
  ``(site_id, slot_row, slot_col)``.

The denormalized ``device_name`` on both layout tables becomes ``site_name``
and is repopulated from ``sites.name`` — a row keyed by ``site_id`` carrying a
device's name is incoherent.

Per table the order is: add nullable ``site_id`` → backfill → **assert** →
drop old key → add new key → drop ``device_id``. Every site currently holds
exactly one device (91/91), so no key collision is possible today, but the
assertions are unconditional: two devices under one site both mapped to the
same category, both holding a layout config, or both holding a slot at the
same grid position would silently lose rows without them. Each raises
``RAISE EXCEPTION`` rather than letting the key drop swallow the duplicates.

Verification snapshot
---------------------
Before any mutation, ``_pre010_snapshot`` captures ``(pk..., device_id)`` for
all three tables so the re-pointing can be verified afterwards by joining the
migrated rows back to their original device and that device's parent site.
The table is deliberately **left behind** for post-migration verification;
drop it manually once satisfied.

Downgrade lossiness
-------------------
``downgrade()`` fully reverses the schema shape, but the data is **not**
bit-reversible once a site holds more than one device. A site-level category
mapping, layout config or layout slot carries no record of which device it
came from, so downgrade attributes every row to the site's lowest-id device.
With today's strict 1:1 (91 sites / 91 devices) that is exact; with two or
more devices per site it is a deterministic but arbitrary choice, and the
original device attribution is gone. ``downgrade()`` raises rather than
guessing if a site holds no device at all.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '010_site_level_categories'
down_revision: Union[str, None] = '009_retire_sureview'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set:
    """Column names of ``table``, or an empty set when the table is absent."""
    if table not in insp.get_table_names():
        return set()
    return {c['name'] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    catmap = _columns(insp, 'site_category_mappings')
    cfg = _columns(insp, 'site_cameras_layout_config')
    slots = _columns(insp, 'site_cameras_layout')

    if not catmap or not cfg or not slots:
        raise RuntimeError(
            '010_site_level_categories: one or more of '
            'site_category_mappings / site_cameras_layout_config / '
            'site_cameras_layout is missing. Inspect the database before '
            'retrying.'
        )

    if 'device_id' not in catmap and 'device_id' not in cfg and 'device_id' not in slots:
        print('010: all three tables already keyed by site_id; nothing to do.')
        return

    # ------------------------------------------------------------------
    # Snapshot — (pk..., device_id) per table, kept for AC-8 verification.
    # ------------------------------------------------------------------
    op.execute('DROP TABLE IF EXISTS _pre010_snapshot')
    op.execute("""
        CREATE TABLE _pre010_snapshot AS
            SELECT 'site_category_mappings'::text AS src_table,
                   m.device_id                    AS device_id,
                   m.category_id::text            AS pk_category_id,
                   NULL::integer                  AS pk_id
              FROM site_category_mappings m
            UNION ALL
            SELECT 'site_cameras_layout_config'::text,
                   c.device_id,
                   NULL::text,
                   NULL::integer
              FROM site_cameras_layout_config c
            UNION ALL
            SELECT 'site_cameras_layout'::text,
                   l.device_id,
                   NULL::text,
                   l.id
              FROM site_cameras_layout l
    """)

    # ==================================================================
    # 1. site_category_mappings — composite PK (device_id, category_id).
    # ==================================================================
    op.add_column(
        'site_category_mappings',
        sa.Column('site_id', sa.String(255), nullable=True,
                  comment='Site identifier'),
    )
    op.execute("""
        UPDATE site_category_mappings
           SET site_id = (SELECT d.site_id FROM devices d
                           WHERE d.id = site_category_mappings.device_id)
    """)
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM site_category_mappings m
             WHERE m.site_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.id = m.site_id);
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % site_category_mappings row(s) could not be '
                    're-pointed at a parent site', n;
            END IF;

            SELECT count(*) INTO n
              FROM (SELECT site_id, category_id
                      FROM site_category_mappings
                     GROUP BY site_id, category_id
                    HAVING count(*) > 1) dup;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % (site_id, category_id) collision(s) in '
                    'site_category_mappings -- two devices under one site are '
                    'mapped to the same category; merge them by hand before '
                    'migrating', n;
            END IF;
        END $$;
    """)
    op.alter_column('site_category_mappings', 'site_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('site_category_mappings_pkey',
                       'site_category_mappings', type_='primary')
    op.create_primary_key('site_category_mappings_pkey',
                          'site_category_mappings', ['site_id', 'category_id'])
    # Dropping device_id also drops site_category_mappings_device_id_fkey and
    # idx_mappings_device, which indexes only that column — so neither is
    # dropped explicitly here (that would error afterwards).
    op.drop_column('site_category_mappings', 'device_id')
    op.create_index('idx_mappings_site', 'site_category_mappings', ['site_id'])
    op.create_foreign_key(
        'site_category_mappings_site_id_fkey', 'site_category_mappings',
        'sites', ['site_id'], ['id'], ondelete='CASCADE',
    )

    # ==================================================================
    # 2. site_cameras_layout_config — device_id IS the PK.
    # ==================================================================
    op.add_column(
        'site_cameras_layout_config',
        sa.Column('site_id', sa.String(255), nullable=True,
                  comment='Unique identifier for the site (references sites.id)'),
    )
    op.execute("""
        UPDATE site_cameras_layout_config
           SET site_id = (SELECT d.site_id FROM devices d
                           WHERE d.id = site_cameras_layout_config.device_id)
    """)
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM site_cameras_layout_config c
             WHERE c.site_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.id = c.site_id);
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % site_cameras_layout_config row(s) could not be '
                    're-pointed at a parent site', n;
            END IF;

            SELECT count(*) INTO n
              FROM (SELECT site_id
                      FROM site_cameras_layout_config
                     GROUP BY site_id
                    HAVING count(*) > 1) dup;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % site_id collision(s) in '
                    'site_cameras_layout_config -- two devices under one site '
                    'each hold a layout config; merge them by hand before '
                    'migrating', n;
            END IF;
        END $$;
    """)
    op.alter_column('site_cameras_layout_config', 'site_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('site_cameras_layout_config_pkey',
                       'site_cameras_layout_config', type_='primary')
    op.create_primary_key('site_cameras_layout_config_pkey',
                          'site_cameras_layout_config', ['site_id'])
    # Drops site_cameras_layout_config_device_id_fkey along with the column.
    op.drop_column('site_cameras_layout_config', 'device_id')
    op.create_foreign_key(
        'site_cameras_layout_config_site_id_fkey',
        'site_cameras_layout_config', 'sites',
        ['site_id'], ['id'], ondelete='CASCADE',
    )
    op.alter_column('site_cameras_layout_config', 'device_name',
                    new_column_name='site_name')
    op.execute("""
        UPDATE site_cameras_layout_config
           SET site_name = (SELECT s.name FROM sites s
                             WHERE s.id = site_cameras_layout_config.site_id)
    """)
    op.execute("COMMENT ON COLUMN site_cameras_layout_config.site_name "
               "IS 'Name of the site'")

    # ==================================================================
    # 3. site_cameras_layout — PK stays on `id`; the unique key moves.
    # ==================================================================
    op.add_column(
        'site_cameras_layout',
        sa.Column('site_id', sa.String(255), nullable=True,
                  comment='Unique identifier for the site (references sites.id)'),
    )
    op.execute("""
        UPDATE site_cameras_layout
           SET site_id = (SELECT d.site_id FROM devices d
                           WHERE d.id = site_cameras_layout.device_id)
    """)
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM site_cameras_layout l
             WHERE l.site_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM sites s WHERE s.id = l.site_id);
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % site_cameras_layout row(s) could not be '
                    're-pointed at a parent site', n;
            END IF;

            SELECT count(*) INTO n
              FROM (SELECT site_id, slot_row, slot_col
                      FROM site_cameras_layout
                     GROUP BY site_id, slot_row, slot_col
                    HAVING count(*) > 1) dup;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010: % (site_id, slot_row, slot_col) collision(s) in '
                    'site_cameras_layout -- two devices under one site occupy '
                    'the same grid position; merge them by hand before '
                    'migrating', n;
            END IF;
        END $$;
    """)
    op.alter_column('site_cameras_layout', 'site_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('uq_site_cameras_layout_device_slot',
                       'site_cameras_layout', type_='unique')
    # Drops fk_site_cameras_layout_device and idx_site_cameras_layout_device
    # (which indexes only device_id) along with the column.
    op.drop_column('site_cameras_layout', 'device_id')
    op.create_unique_constraint(
        'uq_site_cameras_layout_site_slot', 'site_cameras_layout',
        ['site_id', 'slot_row', 'slot_col'],
    )
    op.create_index('idx_site_cameras_layout_site',
                    'site_cameras_layout', ['site_id'])
    op.create_foreign_key(
        'fk_site_cameras_layout_site', 'site_cameras_layout', 'sites',
        ['site_id'], ['id'], ondelete='CASCADE',
    )
    op.alter_column('site_cameras_layout', 'device_name',
                    new_column_name='site_name')
    op.execute("""
        UPDATE site_cameras_layout
           SET site_name = (SELECT s.name FROM sites s
                             WHERE s.id = site_cameras_layout.site_id)
    """)
    op.execute("COMMENT ON COLUMN site_cameras_layout.site_name "
               "IS 'Name of the site'")


def downgrade() -> None:
    """Push categories and camera layouts back down onto the Device.

    LOSSY once a site holds more than one device: the original device
    attribution is not recorded anywhere on a site-keyed row, so every row is
    re-attached to its site's lowest-id device. Exact under the strict 1:1
    that exists today; arbitrary (but deterministic) otherwise.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)

    catmap = _columns(insp, 'site_category_mappings')
    cfg = _columns(insp, 'site_cameras_layout_config')
    slots = _columns(insp, 'site_cameras_layout')

    if not catmap or not cfg or not slots:
        raise RuntimeError(
            '010 downgrade: one or more of site_category_mappings / '
            'site_cameras_layout_config / site_cameras_layout is missing.'
        )

    if 'site_id' not in catmap and 'site_id' not in cfg and 'site_id' not in slots:
        print('010 downgrade: all three tables already keyed by device_id; '
              'nothing to do.')
        return

    # Every site carrying one of these rows must own at least one device, or
    # there is nowhere to put the row back.
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM (
                SELECT site_id FROM site_category_mappings
                UNION SELECT site_id FROM site_cameras_layout_config
                UNION SELECT site_id FROM site_cameras_layout
            ) used
            WHERE NOT EXISTS (
                SELECT 1 FROM devices d WHERE d.site_id = used.site_id
            );
            IF n > 0 THEN
                RAISE EXCEPTION
                    '010 downgrade: % site(s) hold categories/layouts but own '
                    'no device to re-attach them to', n;
            END IF;
        END $$;
    """)

    # ==================================================================
    # 3. site_cameras_layout (reverse order of upgrade).
    # ==================================================================
    op.execute("COMMENT ON COLUMN site_cameras_layout.site_name "
               "IS 'Name of the device'")
    op.alter_column('site_cameras_layout', 'site_name',
                    new_column_name='device_name')
    op.add_column(
        'site_cameras_layout',
        sa.Column('device_id', sa.String(255), nullable=True,
                  comment='Unique identifier for the device (references devices.id)'),
    )
    op.execute("""
        UPDATE site_cameras_layout
           SET device_id = (SELECT d.id FROM devices d
                             WHERE d.site_id = site_cameras_layout.site_id
                             ORDER BY d.id LIMIT 1)
    """)
    op.execute("""
        UPDATE site_cameras_layout
           SET device_name = (SELECT d.name FROM devices d
                               WHERE d.id = site_cameras_layout.device_id)
    """)
    op.alter_column('site_cameras_layout', 'device_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('uq_site_cameras_layout_site_slot',
                       'site_cameras_layout', type_='unique')
    # Drops fk_site_cameras_layout_site and idx_site_cameras_layout_site.
    op.drop_column('site_cameras_layout', 'site_id')
    op.create_unique_constraint(
        'uq_site_cameras_layout_device_slot', 'site_cameras_layout',
        ['device_id', 'slot_row', 'slot_col'],
    )
    op.create_index('idx_site_cameras_layout_device',
                    'site_cameras_layout', ['device_id'])
    op.create_foreign_key(
        'fk_site_cameras_layout_device', 'site_cameras_layout', 'devices',
        ['device_id'], ['id'], ondelete='CASCADE',
    )

    # ==================================================================
    # 2. site_cameras_layout_config.
    # ==================================================================
    op.execute("COMMENT ON COLUMN site_cameras_layout_config.site_name "
               "IS 'Name of the device'")
    op.alter_column('site_cameras_layout_config', 'site_name',
                    new_column_name='device_name')
    op.add_column(
        'site_cameras_layout_config',
        sa.Column('device_id', sa.String(255), nullable=True,
                  comment='Unique identifier for the device (references devices.id)'),
    )
    op.execute("""
        UPDATE site_cameras_layout_config
           SET device_id = (SELECT d.id FROM devices d
                             WHERE d.site_id = site_cameras_layout_config.site_id
                             ORDER BY d.id LIMIT 1)
    """)
    op.execute("""
        UPDATE site_cameras_layout_config
           SET device_name = (SELECT d.name FROM devices d
                               WHERE d.id = site_cameras_layout_config.device_id)
    """)
    op.alter_column('site_cameras_layout_config', 'device_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('site_cameras_layout_config_pkey',
                       'site_cameras_layout_config', type_='primary')
    op.create_primary_key('site_cameras_layout_config_pkey',
                          'site_cameras_layout_config', ['device_id'])
    # Drops site_cameras_layout_config_site_id_fkey along with the column.
    op.drop_column('site_cameras_layout_config', 'site_id')
    op.create_foreign_key(
        'site_cameras_layout_config_device_id_fkey',
        'site_cameras_layout_config', 'devices',
        ['device_id'], ['id'], ondelete='CASCADE',
    )

    # ==================================================================
    # 1. site_category_mappings.
    # ==================================================================
    op.add_column(
        'site_category_mappings',
        sa.Column('device_id', sa.String(255), nullable=True,
                  comment='Device identifier'),
    )
    op.execute("""
        UPDATE site_category_mappings
           SET device_id = (SELECT d.id FROM devices d
                             WHERE d.site_id = site_category_mappings.site_id
                             ORDER BY d.id LIMIT 1)
    """)
    op.alter_column('site_category_mappings', 'device_id',
                    existing_type=sa.String(255), nullable=False)
    op.drop_constraint('site_category_mappings_pkey',
                       'site_category_mappings', type_='primary')
    op.create_primary_key('site_category_mappings_pkey',
                          'site_category_mappings', ['device_id', 'category_id'])
    # Drops site_category_mappings_site_id_fkey and idx_mappings_site.
    op.drop_column('site_category_mappings', 'site_id')
    op.create_index('idx_mappings_device', 'site_category_mappings', ['device_id'])
    op.create_foreign_key(
        'site_category_mappings_device_id_fkey', 'site_category_mappings',
        'devices', ['device_id'], ['id'], ondelete='CASCADE',
    )
