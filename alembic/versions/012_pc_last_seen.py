"""Add pcs.last_seen for the rolling heartbeat presence timestamp.

Adds a nullable BIGINT ``last_seen`` (unix ts) to ``pcs``, rolled by the
websocket server's presence sweep. Distinct from ``last_connected`` (written only
when a PC (re)connects) — ``last_seen`` advances while the PC is alive on the
heartbeat. Purely additive; no data backfill needed (NULL until first sweep).

Revision ID: 012_pc_last_seen
Revises: 011_screen_layouts
Create Date: 2026-07-22
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "012_pc_last_seen"
down_revision: Union[str, None] = "011_screen_layouts"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = _columns(insp, "pcs")

    if "last_seen" not in cols:
        op.add_column(
            "pcs",
            sa.Column(
                "last_seen",
                sa.BigInteger(),
                nullable=True,
                comment=(
                    "Unix timestamp the PC was last seen alive on the websocket "
                    "(rolled by the heartbeat presence sweep)"
                ),
            ),
        )

    existing_idx = {i["name"] for i in insp.get_indexes("pcs")}
    if "idx_pcs_last_seen" not in existing_idx:
        op.create_index(
            "idx_pcs_last_seen",
            "pcs",
            ["last_seen"],
            postgresql_ops={"last_seen": "DESC"},
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())

    existing_idx = {i["name"] for i in insp.get_indexes("pcs")}
    if "idx_pcs_last_seen" in existing_idx:
        op.drop_index("idx_pcs_last_seen", table_name="pcs")

    if "last_seen" in _columns(insp, "pcs"):
        op.drop_column("pcs", "last_seen")
