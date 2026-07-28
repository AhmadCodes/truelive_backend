"""
Team model — the top-level organizational grouping.

A **Team** groups three top-level entities:

- **Sites** — many-to-many via the ``site_team`` association table (a Site may
  belong to several Teams simultaneously).
- **PCs** — each PC belongs to exactly one Team (``pcs.team_id``, NOT NULL).
- **Screen Layouts** — each layout belongs to exactly one Team
  (``screen_layouts.team_id``, NOT NULL).

Teams constrain which cameras may appear on a team's layouts (only cameras whose
site is a member of the team) and which PCs a team's layouts may be assigned to
(only PCs in the same team). Introduced by migration ``013_teams``, which seeds a
single "Alpha Team" and backfills all pre-existing rows into it.

Note: Teams are an organizational + validation grouping only. They do NOT scope
which rows a user can see — every authenticated user still sees all teams, sites,
PCs, and layouts.
"""

from sqlalchemy import (
    Column,
    String,
    Table,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base
from app.models.base import BaseModel, ActorStampMixin


# ---------------------------------------------------------------------------
# Site <-> Team association (many-to-many).
#
# ON DELETE CASCADE lives on the *membership row* only: deleting a Site or a
# Team removes its membership rows, but never the other parent. (Team deletion
# is additionally blocked while non-empty at the API layer.)
# ---------------------------------------------------------------------------
site_team = Table(
    "site_team",
    Base.metadata,
    Column(
        "site_id",
        String(255),
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "team_id",
        String(50),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
    ),
    PrimaryKeyConstraint("site_id", "team_id", name="pk_site_team"),
    Index("idx_site_team_team_id", "team_id"),
)


class Team(BaseModel, ActorStampMixin):
    """
    Team model — organizational owner of sites (M:N), PCs (1:N), and layouts (1:N).

    Attributes:
        id: Unique identifier for the team (application-minted ``TEAM_<hex>``)
        name: Unique display name of the team
    """

    __tablename__ = "teams"

    id = Column(String(50), primary_key=True, comment="Unique identifier for the team")
    name = Column(
        String(255),
        nullable=False,
        comment="Unique display name of the team",
    )

    # The unique constraint on `name` already creates a backing index, so no
    # separate Index is declared here.
    __table_args__ = (UniqueConstraint("name", name="uq_teams_name"),)

    # Relationships
    sites = relationship(
        "Site",
        secondary="site_team",
        back_populates="teams",
        doc="Sites assigned to this team (many-to-many)",
    )
    pcs = relationship(
        "PC",
        back_populates="team",
        foreign_keys="PC.team_id",
        doc="PCs belonging to this team",
    )
    layouts = relationship(
        "ScreenLayout",
        back_populates="team",
        foreign_keys="ScreenLayout.team_id",
        doc="Screen layouts belonging to this team",
    )

    def __repr__(self):
        """String representation of Team."""
        return f"<Team(id='{self.id}', name='{self.name}')>"

    def to_dict(self):
        """Convert Team instance to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
