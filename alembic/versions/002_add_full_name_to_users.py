"""Add full_name column to users table

Revision ID: 002_add_full_name
Revises: 001_initial_schema
Create Date: 2025-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '002_add_full_name'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add full_name column to users table.

    For existing users, set full_name to username as default.
    """
    # Add full_name column as nullable first
    op.add_column('users', sa.Column('full_name', sa.String(255), nullable=True,
                                     comment="User's full name"))

    # Update existing users: set full_name = username
    op.execute("UPDATE users SET full_name = username WHERE full_name IS NULL")

    # Make the column non-nullable
    op.alter_column('users', 'full_name', nullable=False)


def downgrade() -> None:
    """Remove full_name column from users table."""
    op.drop_column('users', 'full_name')
