"""Initial schema with all tables

Revision ID: 001_initial_schema
Revises:
Create Date: 2025-10-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Import all models at module level to ensure they're registered
from app.database import Base
from app.models import (
    User, InvitationToken, AuditLog,
    SiteCategory, SiteCategoryMapping,
    Site, Camera, Screenshot,
    SiteCamerasLayoutConfig, SiteCamerasLayout,
    PC, Screen, View, ScreenMapping
)

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all initial tables with all fields including SureView fields."""
    # Get the connection from the current context
    bind = op.get_bind()

    # Create all tables based on the models
    Base.metadata.create_all(bind)


def downgrade() -> None:
    """Drop all tables."""
    bind = op.get_bind()

    # Drop all tables
    Base.metadata.drop_all(bind)
