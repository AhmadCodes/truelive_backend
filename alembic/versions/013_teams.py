"""Introduce the Team abstraction.

Adds the ``teams`` table and the ``site_team`` many-to-many association, plus a
``team_id`` foreign key on ``pcs`` and ``screen_layouts`` (each entity belongs to
exactly one team). Seeds a single "Alpha Team" and backfills ALL pre-existing
sites (as memberships), PCs, and layouts into it, then tightens the two new
columns to NOT NULL. Team membership is mandatory going forward.

Idempotent + inspector-guarded so it is safe to re-run; downgrade fully reverses.

Revision ID: 013_teams
Revises: 012_pc_last_seen
Create Date: 2026-07-24
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "013_teams"
down_revision: Union[str, None] = "012_pc_last_seen"
branch_labels = None
depends_on = None


# Deterministic id for the seeded default team, used by the seed INSERT. The
# backfill re-resolves the id by name (below) so it stays correct even if a team
# named "Alpha Team" already existed with a different id.
ALPHA_ID = "TEAM_ALPHA0000001"
ALPHA_NAME = "Alpha Team"


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

    # 1. teams
    if "teams" not in tables:
        op.create_table(
            "teams",
            sa.Column("id", sa.String(50), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint("name", name="uq_teams_name"),
        )

    # 2. site_team (M:N). CASCADE on the membership row only.
    if "site_team" not in tables:
        op.create_table(
            "site_team",
            sa.Column(
                "site_id",
                sa.String(255),
                sa.ForeignKey("sites.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "team_id",
                sa.String(50),
                sa.ForeignKey("teams.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("site_id", "team_id", name="pk_site_team"),
        )
        op.create_index("idx_site_team_team_id", "site_team", ["team_id"])

    # 3. team_id columns (nullable first — tightened after backfill)
    if "team_id" not in _columns(insp, "pcs"):
        op.add_column(
            "pcs",
            sa.Column(
                "team_id",
                sa.String(50),
                sa.ForeignKey("teams.id", name="fk_pcs_team"),
                nullable=True,
                comment="ID of the team this PC belongs to",
            ),
        )
    if "team_id" not in _columns(insp, "screen_layouts"):
        op.add_column(
            "screen_layouts",
            sa.Column(
                "team_id",
                sa.String(50),
                sa.ForeignKey("teams.id", name="fk_screen_layouts_team"),
                nullable=True,
                comment="ID of the team this screen layout belongs to",
            ),
        )

    # 4. Seed the single default team (idempotent).
    op.execute(
        sa.text(
            "INSERT INTO teams (id, name, created_at, updated_at) "
            "SELECT :id, :name, now(), now() "
            "WHERE NOT EXISTS (SELECT 1 FROM teams WHERE name = :name)"
        ).bindparams(id=ALPHA_ID, name=ALPHA_NAME)
    )

    # Resolve the id of the team named ALPHA_NAME (covers the case where a team
    # of that name already existed with a different id).
    alpha_id = bind.execute(
        sa.text("SELECT id FROM teams WHERE name = :name").bindparams(name=ALPHA_NAME)
    ).scalar_one()

    # 5. Backfill every existing row into the default team.
    op.execute(
        sa.text(
            "INSERT INTO site_team (site_id, team_id) "
            "SELECT id, :team FROM sites "
            "ON CONFLICT DO NOTHING"
        ).bindparams(team=alpha_id)
    )
    op.execute(
        sa.text("UPDATE pcs SET team_id = :team WHERE team_id IS NULL").bindparams(
            team=alpha_id
        )
    )
    op.execute(
        sa.text(
            "UPDATE screen_layouts SET team_id = :team WHERE team_id IS NULL"
        ).bindparams(team=alpha_id)
    )

    # 6. Tighten to NOT NULL now that every row is backfilled.
    op.alter_column("pcs", "team_id", existing_type=sa.String(50), nullable=False)
    op.alter_column(
        "screen_layouts", "team_id", existing_type=sa.String(50), nullable=False
    )

    # 7. Indexes on the new FKs.
    if "idx_pcs_team_id" not in _indexes(insp, "pcs"):
        op.create_index("idx_pcs_team_id", "pcs", ["team_id"])
    if "idx_screen_layouts_team_id" not in _indexes(insp, "screen_layouts"):
        op.create_index("idx_screen_layouts_team_id", "screen_layouts", ["team_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = _tables(insp)

    if "screen_layouts" in tables:
        if "idx_screen_layouts_team_id" in _indexes(insp, "screen_layouts"):
            op.drop_index("idx_screen_layouts_team_id", table_name="screen_layouts")
        if "team_id" in _columns(insp, "screen_layouts"):
            op.drop_column("screen_layouts", "team_id")

    if "pcs" in tables:
        if "idx_pcs_team_id" in _indexes(insp, "pcs"):
            op.drop_index("idx_pcs_team_id", table_name="pcs")
        if "team_id" in _columns(insp, "pcs"):
            op.drop_column("pcs", "team_id")

    if "site_team" in tables:
        op.drop_table("site_team")

    if "teams" in _tables(sa.inspect(bind)):
        op.drop_table("teams")
