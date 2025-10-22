"""Fix invitation_tokens table structure

Revision ID: 005_fix_invitation_tokens
Revises: 004_add_system_settings
Create Date: 2025-10-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '005_fix_invitation_tokens'
down_revision: Union[str, None] = '004_add_system_settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Fix invitation_tokens table to match the model."""

    # Add missing columns
    op.add_column('invitation_tokens',
        sa.Column('email', sa.String(255), nullable=True, comment='Email address of the invited user')
    )
    op.add_column('invitation_tokens',
        sa.Column('role', sa.String(50), nullable=True, server_default='user', comment='Role to assign to the user')
    )
    op.add_column('invitation_tokens',
        sa.Column('invited_by_id', postgresql.UUID(as_uuid=True), nullable=True, comment='User who sent the invitation')
    )

    # Add foreign key constraint for invited_by_id
    op.create_foreign_key(
        'invitation_tokens_invited_by_id_fkey',
        'invitation_tokens', 'users',
        ['invited_by_id'], ['user_id'],
        ondelete='CASCADE'
    )

    # Add index on email
    op.create_index('idx_invitation_tokens_email', 'invitation_tokens', ['email'])
    op.create_index('idx_invitation_tokens_invited_by_id', 'invitation_tokens', ['invited_by_id'])

    # Rename token_id column to id
    op.alter_column('invitation_tokens', 'token_id', new_column_name='id')

    # Update role column to not null after adding default
    op.execute("UPDATE invitation_tokens SET role = 'user' WHERE role IS NULL")
    op.alter_column('invitation_tokens', 'role', nullable=False)

    # Make user_id nullable (it's only set after invitation is accepted)
    op.alter_column('invitation_tokens', 'user_id', nullable=True)


def downgrade() -> None:
    """Revert invitation_tokens table changes."""

    # Rename id back to token_id
    op.alter_column('invitation_tokens', 'id', new_column_name='token_id')

    # Drop indexes
    op.drop_index('idx_invitation_tokens_invited_by_id', table_name='invitation_tokens')
    op.drop_index('idx_invitation_tokens_email', table_name='invitation_tokens')

    # Drop foreign key constraint
    op.drop_constraint('invitation_tokens_invited_by_id_fkey', 'invitation_tokens', type_='foreignkey')

    # Drop columns
    op.drop_column('invitation_tokens', 'invited_by_id')
    op.drop_column('invitation_tokens', 'role')
    op.drop_column('invitation_tokens', 'email')
