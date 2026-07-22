"""Decouple screen layouts from PCs — insert a ScreenLayout owner between PC and Screen.

Revision ID: 011_screen_layouts
Revises: 010_site_level_categories
Create Date: 2026-07-22

Until now a ``Screen`` hung directly off a ``PC`` (``screens.pc_id``) and each
``screen_mapping`` both carried its own ``pc_id`` and stored a single global
``playing_state``. That conflated three concerns: *which display grid* a set of
screens forms, *which PC(s)* render that grid, and *whether a given camera is
playing for a given PC*. Sharing one grid across several PCs was impossible, and
play state could not differ per PC.

This migration introduces ``screen_layouts`` as the new owner of screens:

- ``screens`` reparents from ``pcs.id`` to ``screen_layouts.id``
  (``screen_layout_id``, CASCADE, NOT NULL).
- ``pcs`` gains a single nullable ``screen_layout_id`` pointer (SET NULL) so
  many PCs may share one layout.
- ``screen_mappings`` loses ``pc_id`` and ``playing_state``; the grid is now
  PC-agnostic.
- A new ``pc_screen_mapping_state`` table holds portal-only per-``(PC, mapping)``
  play state, replacing the single ``screen_mappings.playing_state`` column.

One layout is minted per existing PC (``lay_<pc_id>``, named after the PC), every
PC is assigned to its own layout, and each screen reparents to the layout minted
from its former ``pc_id`` — so the pre-migration 1 PC ⇒ 1 grid topology is
preserved exactly. Per-PC play state is copied one row per existing mapping.

Verification snapshot
---------------------
Before any mutation, ``_pre011_snapshot`` captures ``(mapping_id, pc_id,
playing_state)`` for every ``screen_mapping`` so the copy into
``pc_screen_mapping_state`` can be verified afterwards (AC-8). The table is
deliberately **left behind**; drop it manually once satisfied. All five
correctness assertions run **before** any column is dropped (they read
``screen_mappings.pc_id`` / ``.playing_state``, which still exist at that point)
and each raises ``RAISE EXCEPTION`` rather than letting a later drop swallow a
discrepancy.

Downgrade lossiness
-------------------
``downgrade()`` fully reverses the schema shape, but the data is **bit-reversible
only while every layout has at most one assigned PC** (the topology that exists
immediately after this upgrade). A screen and a mapping carry no record of which
PC they belonged to once ``pc_id`` is gone — they are re-attached to the layout's
assigned PC, and for a layout shared by several PCs downgrade deterministically
picks the lowest ``pcs.id`` and discards the other PCs' per-PC play state. If a
layout that owns screens has **zero** assigned PCs there is nowhere to re-attach
them, so ``downgrade()`` raises rather than guessing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "011_screen_layouts"
down_revision: Union[str, None] = "010_site_level_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set:
    """Column names of ``table``, or an empty set when the table is absent."""
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    sm_cols = _columns(insp, "screen_mappings")
    sc_cols = _columns(insp, "screens")
    pcs_cols = _columns(insp, "pcs")

    if not sm_cols or not sc_cols or not pcs_cols:
        raise RuntimeError(
            "011_screen_layouts: one or more of screen_mappings / screens / "
            "pcs is missing. Inspect the database before retrying."
        )

    if "pc_id" not in sm_cols and "pc_id" not in sc_cols:
        print(
            "011: screens/screen_mappings already decoupled from pcs; " "nothing to do."
        )
        return

    # ------------------------------------------------------------------
    # 1. Snapshot — (mapping_id, pc_id, playing_state) per mapping, kept for
    #    AC-8 verification. Taken while pc_id / playing_state still exist.
    # ------------------------------------------------------------------
    op.execute("DROP TABLE IF EXISTS _pre011_snapshot")
    op.execute(
        """
        CREATE TABLE _pre011_snapshot AS
            SELECT id AS mapping_id, pc_id, playing_state
              FROM screen_mappings
    """
    )

    # ------------------------------------------------------------------
    # 2. screen_layouts — the new owner of screens.
    # ------------------------------------------------------------------
    op.create_table(
        "screen_layouts",
        sa.Column(
            "id",
            sa.String(100),
            nullable=False,
            comment="Unique identifier for the screen layout",
        ),
        sa.Column(
            "name",
            sa.String(255),
            nullable=False,
            comment="Display name of the screen layout",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when the record was created",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when the record was last updated",
        ),
        sa.PrimaryKeyConstraint("id", name="screen_layouts_pkey"),
    )
    op.create_index("idx_screen_layouts_name", "screen_layouts", ["name"])

    # ------------------------------------------------------------------
    # 3. Add the nullable layout pointers (populated below, before NOT NULL).
    # ------------------------------------------------------------------
    op.add_column(
        "screens",
        sa.Column(
            "screen_layout_id",
            sa.String(100),
            nullable=True,
            comment="ID of the screen layout this screen belongs to",
        ),
    )
    op.add_column(
        "pcs",
        sa.Column(
            "screen_layout_id",
            sa.String(100),
            nullable=True,
            comment="ID of the screen layout assigned to this PC",
        ),
    )

    # ------------------------------------------------------------------
    # 4. Seed one layout per PC, assign each PC to its own layout, and
    #    reparent each screen onto the layout minted from its former pc_id.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO screen_layouts (id, name, created_at, updated_at)
        SELECT 'lay_' || p.id, p.name, now(), now()
          FROM pcs p
    """
    )
    op.execute("UPDATE pcs SET screen_layout_id = 'lay_' || pcs.id")
    op.execute("UPDATE screens SET screen_layout_id = 'lay_' || screens.pc_id")

    # ------------------------------------------------------------------
    # 5. pc_screen_mapping_state — portal-only per-(PC, mapping) play state.
    # ------------------------------------------------------------------
    op.create_table(
        "pc_screen_mapping_state",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="Auto-incrementing primary key",
        ),
        sa.Column("pc_id", sa.String(50), nullable=False, comment="ID of the PC"),
        sa.Column(
            "mapping_id",
            sa.Integer(),
            nullable=False,
            comment="ID of the screen mapping",
        ),
        sa.Column(
            "playing_state",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether this camera is currently playing for this PC",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when the record was created",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Timestamp when the record was last updated",
        ),
        sa.PrimaryKeyConstraint("id", name="pc_screen_mapping_state_pkey"),
        sa.UniqueConstraint("pc_id", "mapping_id", name="uq_pc_screen_mapping_state"),
        sa.ForeignKeyConstraint(
            ["pc_id"], ["pcs.id"], name="fk_pcsms_pc", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id"],
            ["screen_mappings.id"],
            name="fk_pcsms_mapping",
            ondelete="CASCADE",
        ),
    )
    op.create_index("idx_pcsms_pc", "pc_screen_mapping_state", ["pc_id"])
    op.create_index("idx_pcsms_mapping", "pc_screen_mapping_state", ["mapping_id"])

    # ------------------------------------------------------------------
    # 6. Copy the per-PC play state — one row per existing mapping.
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO pc_screen_mapping_state (pc_id, mapping_id, playing_state)
        SELECT sm.pc_id, sm.id, sm.playing_state
          FROM screen_mappings sm
    """
    )

    # ------------------------------------------------------------------
    # 7. Assert BEFORE any drop (AC-9). These read screen_mappings.pc_id /
    #    .playing_state, which still exist at this point.
    # ------------------------------------------------------------------
    # 7a. Every screen must have a non-NULL layout.
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM screens WHERE screen_layout_id IS NULL;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011: % screen(s) have a NULL screen_layout_id after '
                    'reparenting', n;
            END IF;
        END $$;
    """
    )
    # 7b. Every screen/pc layout pointer must resolve to an existing layout.
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM screens s
             WHERE NOT EXISTS (SELECT 1 FROM screen_layouts l
                                WHERE l.id = s.screen_layout_id);
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011: % screen(s) reference a screen_layout_id that does '
                    'not exist in screen_layouts', n;
            END IF;

            SELECT count(*) INTO n
              FROM pcs p
             WHERE p.screen_layout_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM screen_layouts l
                                WHERE l.id = p.screen_layout_id);
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011: % pc(s) reference a screen_layout_id that does not '
                    'exist in screen_layouts', n;
            END IF;
        END $$;
    """
    )
    # 7c. Row count parity between the copy and the source.
    op.execute(
        """
        DO $$
        DECLARE a bigint; b bigint;
        BEGIN
            SELECT count(*) INTO a FROM pc_screen_mapping_state;
            SELECT count(*) INTO b FROM screen_mappings;
            IF a <> b THEN
                RAISE EXCEPTION
                    '011: pc_screen_mapping_state has % row(s) but '
                    'screen_mappings has % -- state copy is incomplete', a, b;
            END IF;
        END $$;
    """
    )
    # 7d. Every copied row must agree with its source on (pc_id, playing_state).
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM pc_screen_mapping_state st
              JOIN screen_mappings sm ON sm.id = st.mapping_id
             WHERE st.pc_id IS DISTINCT FROM sm.pc_id
                OR st.playing_state IS DISTINCT FROM sm.playing_state;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011: % pc_screen_mapping_state row(s) disagree with '
                    'screen_mappings on (pc_id, playing_state)', n;
            END IF;
        END $$;
    """
    )
    # 7e. No duplicate (pc_id, mapping_id) in the new table.
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n
              FROM (SELECT pc_id, mapping_id
                      FROM pc_screen_mapping_state
                     GROUP BY pc_id, mapping_id
                    HAVING count(*) > 1) dup;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011: % duplicate (pc_id, mapping_id) row(s) in '
                    'pc_screen_mapping_state', n;
            END IF;
        END $$;
    """
    )

    # ------------------------------------------------------------------
    # 8. Now that every screen is reparented, enforce NOT NULL.
    # ------------------------------------------------------------------
    op.alter_column(
        "screens", "screen_layout_id", existing_type=sa.String(100), nullable=False
    )

    # ------------------------------------------------------------------
    # 9. Add the layout FKs/indexes with the exact names the models declare.
    # ------------------------------------------------------------------
    op.create_index("idx_screens_layout_id", "screens", ["screen_layout_id"])
    op.create_foreign_key(
        "fk_screens_screen_layout",
        "screens",
        "screen_layouts",
        ["screen_layout_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_pcs_screen_layout_id", "pcs", ["screen_layout_id"])
    op.create_foreign_key(
        "fk_pcs_screen_layout",
        "pcs",
        "screen_layouts",
        ["screen_layout_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ------------------------------------------------------------------
    # 10. Drop the old PC linkage. In Postgres DROP COLUMN auto-drops the
    #     column's own FK and single-column index, so screens_pc_id_fkey /
    #     idx_screens_pc_id and screen_mappings_pc_id_fkey / idx_screen_mappings_pc
    #     are NOT dropped explicitly here (that would error afterwards).
    # ------------------------------------------------------------------
    op.drop_column("screen_mappings", "pc_id")
    op.drop_column("screen_mappings", "playing_state")
    op.drop_column("screens", "pc_id")


def downgrade() -> None:
    """Re-attach screens and per-mapping play state back onto PCs.

    EXACT only while every layout has at most one assigned PC (the topology
    produced by ``upgrade()``). For a layout shared by several PCs each screen /
    mapping is re-attached to the lowest ``pcs.id`` assigned to that layout and
    the other PCs' per-PC play state is discarded. Raises if a layout that owns
    screens has no assigned PC — there is nowhere to put its screens back.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)

    sm_cols = _columns(insp, "screen_mappings")
    sc_cols = _columns(insp, "screens")

    if not sm_cols or not sc_cols:
        raise RuntimeError("011 downgrade: screen_mappings or screens is missing.")

    if "screen_layout_id" not in sc_cols:
        print(
            "011 downgrade: screens.screen_layout_id absent; already "
            "reversed. Nothing to do."
        )
        return

    if "pc_screen_mapping_state" not in insp.get_table_names():
        raise RuntimeError(
            "011 downgrade: pc_screen_mapping_state is missing; per-mapping "
            "play state cannot be restored."
        )

    # A layout that owns screens must have at least one assigned PC, or there
    # is nowhere to re-attach those screens (and their mappings).
    op.execute(
        """
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM (
                SELECT DISTINCT s.screen_layout_id
                  FROM screens s
                 WHERE NOT EXISTS (
                    SELECT 1 FROM pcs p
                     WHERE p.screen_layout_id = s.screen_layout_id
                 )
            ) orphan;
            IF n > 0 THEN
                RAISE EXCEPTION
                    '011 downgrade: % layout(s) own screens but have no '
                    'assigned PC to re-attach them to', n;
            END IF;
        END $$;
    """
    )

    # ------------------------------------------------------------------
    # 1. Re-add the old columns (nullable / with a default so the ADD succeeds
    #    on a populated table; backfilled below).
    # ------------------------------------------------------------------
    op.add_column(
        "screens",
        sa.Column(
            "pc_id",
            sa.String(50),
            nullable=True,
            comment="ID of the PC this screen is connected to",
        ),
    )
    op.add_column(
        "screen_mappings",
        sa.Column("pc_id", sa.String(50), nullable=True, comment="ID of the PC"),
    )
    op.add_column(
        "screen_mappings",
        sa.Column(
            "playing_state",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether this camera is currently playing",
        ),
    )

    # ------------------------------------------------------------------
    # 2. Backfill from the surviving layout -> PC link (lowest pcs.id wins for
    #    a shared layout), and restore playing_state from the per-PC state
    #    table (lowest pc_id wins; default false when no row exists).
    # ------------------------------------------------------------------
    op.execute(
        """
        UPDATE screens
           SET pc_id = (SELECT min(p.id) FROM pcs p
                         WHERE p.screen_layout_id = screens.screen_layout_id)
    """
    )
    op.execute(
        """
        UPDATE screen_mappings
           SET pc_id = (
               SELECT min(p.id)
                 FROM screens s
                 JOIN pcs p ON p.screen_layout_id = s.screen_layout_id
                WHERE s.id = screen_mappings.screen_id
           )
    """
    )
    op.execute(
        """
        UPDATE screen_mappings
           SET playing_state = COALESCE((
               SELECT st.playing_state
                 FROM pc_screen_mapping_state st
                WHERE st.mapping_id = screen_mappings.id
                  AND st.pc_id = (SELECT min(st2.pc_id)
                                    FROM pc_screen_mapping_state st2
                                   WHERE st2.mapping_id = screen_mappings.id)
           ), false)
    """
    )

    # ------------------------------------------------------------------
    # 3. Restore the original NOT NULL on both pc_id columns (both were NOT
    #    NULL before 011; every row is backfilled above, guaranteed non-NULL
    #    by the orphan assertion since each layout has an assigned PC).
    # ------------------------------------------------------------------
    op.alter_column("screens", "pc_id", existing_type=sa.String(50), nullable=False)
    op.alter_column(
        "screen_mappings", "pc_id", existing_type=sa.String(50), nullable=False
    )

    # ------------------------------------------------------------------
    # 4. Re-add the old indexes and PC foreign keys.
    # ------------------------------------------------------------------
    op.create_index("idx_screens_pc_id", "screens", ["pc_id"])
    op.create_foreign_key(
        "screens_pc_id_fkey",
        "screens",
        "pcs",
        ["pc_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("idx_screen_mappings_pc", "screen_mappings", ["pc_id"])
    op.create_foreign_key(
        "screen_mappings_pc_id_fkey",
        "screen_mappings",
        "pcs",
        ["pc_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # ------------------------------------------------------------------
    # 5. Drop the layout pointers. DROP COLUMN auto-drops each column's own
    #    FK and single-column index (fk_screens_screen_layout /
    #    idx_screens_layout_id and fk_pcs_screen_layout /
    #    idx_pcs_screen_layout_id).
    # ------------------------------------------------------------------
    op.drop_column("screens", "screen_layout_id")
    op.drop_column("pcs", "screen_layout_id")

    # ------------------------------------------------------------------
    # 6. Drop the per-PC state table (already read for the restore above).
    # ------------------------------------------------------------------
    op.drop_table("pc_screen_mapping_state")

    # ------------------------------------------------------------------
    # 7. Drop the layout table (no FK references it any more).
    # ------------------------------------------------------------------
    op.drop_table("screen_layouts")
