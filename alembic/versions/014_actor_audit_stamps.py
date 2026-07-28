"""Actor stamping + audit actor columns.

Adds ``created_by_*`` / ``updated_by_*`` (type, id, label) triples to the nine
in-scope entities (sites, devices, cameras, pcs, teams, screen_layouts, screens,
views, screen_mappings) so every row records who created and last modified it —
where the actor may be a user, a service account, or the system.

Also extends ``audit_logs`` with ``actor_type`` / ``actor_id`` / ``actor_label``
(keeping the legacy ``user_id`` column) so audit entries can attribute an action
to a service account as well as a user.

Existing rows backfill to the ``system`` actor via the column ``server_default``;
existing audit rows with a ``user_id`` backfill to ``actor_type='user'``.

Idempotent + inspector-guarded so it is safe to re-run; downgrade fully reverses.

VERIFY ON A RESTORED SCRATCH COPY OF PRODUCTION — never the live database. The
verification harness (experiments/site_device_refactor/verify_014.py) imports
experiments/site_device_refactor/_guard.py, which hard-exits if DATABASE_URL is
unset or ends with ``/truelive_portal``.

Revision ID: 014_actor_audit_stamps
Revises: 013_teams
Create Date: 2026-07-28
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "014_actor_audit_stamps"
down_revision: Union[str, None] = "013_teams"
branch_labels = None
depends_on = None


# The nine entities that get creator/modifier stamps.
STAMPED_TABLES = [
    "sites",
    "devices",
    "cameras",
    "pcs",
    "teams",
    "screen_layouts",
    "screens",
    "views",
    "screen_mappings",
]

# (column, type factory, nullable, server_default)
STAMP_COLUMNS = [
    ("created_by_type", lambda: sa.String(20), False, "system"),
    ("created_by_id", lambda: sa.String(36), True, None),
    ("created_by_label", lambda: sa.String(255), False, "system"),
    ("updated_by_type", lambda: sa.String(20), False, "system"),
    ("updated_by_id", lambda: sa.String(36), True, None),
    ("updated_by_label", lambda: sa.String(255), False, "system"),
]

AUDIT_ACTOR_COLUMNS = [
    ("actor_type", lambda: sa.String(20)),
    ("actor_id", lambda: sa.String(36)),
    ("actor_label", lambda: sa.String(255)),
]


def _tables(insp):
    return set(insp.get_table_names())


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table):
    return {i["name"] for i in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = _tables(insp)

    # 1. Stamp columns on the nine entities. server_default backfills existing rows.
    for table in STAMPED_TABLES:
        if table not in tables:
            continue
        existing = _columns(insp, table)
        for name, type_factory, nullable, default in STAMP_COLUMNS:
            if name in existing:
                continue
            kwargs = {}
            if default is not None:
                kwargs["server_default"] = default
            op.add_column(
                table,
                sa.Column(name, type_factory(), nullable=nullable, **kwargs),
            )

    # 2. Actor columns on audit_logs (nullable — historical rows predate them).
    if "audit_logs" in tables:
        audit_cols = _columns(insp, "audit_logs")
        for name, type_factory in AUDIT_ACTOR_COLUMNS:
            if name not in audit_cols:
                op.add_column(
                    "audit_logs", sa.Column(name, type_factory(), nullable=True)
                )
        # Backfill: user-attributed rows become actor_type='user'; the rest 'system'.
        op.execute(
            sa.text(
                "UPDATE audit_logs SET actor_type = 'user', actor_id = user_id::text "
                "WHERE user_id IS NOT NULL AND actor_type IS NULL"
            )
        )
        op.execute(
            sa.text(
                "UPDATE audit_logs SET actor_type = 'system' WHERE actor_type IS NULL"
            )
        )
        if "ix_audit_logs_actor" not in _indexes(insp, "audit_logs"):
            op.create_index(
                "ix_audit_logs_actor", "audit_logs", ["actor_type", "actor_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = _tables(insp)

    if "audit_logs" in tables:
        if "ix_audit_logs_actor" in _indexes(insp, "audit_logs"):
            op.drop_index("ix_audit_logs_actor", table_name="audit_logs")
        audit_cols = _columns(insp, "audit_logs")
        for name, _ in AUDIT_ACTOR_COLUMNS:
            if name in audit_cols:
                op.drop_column("audit_logs", name)

    for table in STAMPED_TABLES:
        if table not in tables:
            continue
        existing = _columns(insp, table)
        for name, _type_factory, _nullable, _default in STAMP_COLUMNS:
            if name in existing:
                op.drop_column(table, name)
