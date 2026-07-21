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
    """Fix invitation_tokens table to match the model.

    Brings the table to exactly the shape app/models/user.py declares and that
    production carries: `email` and `invited_by_id` NOT NULL, `role` NOT NULL with
    no database default (the model supplies 'user' Python-side), both the
    `idx_*` and `ix_*` index families, and the two CHECK constraints.

    `email` / `invited_by_id` cannot be backfilled — there is no source for an
    invitee address on a pre-005 row — so any rows predating this revision are
    removed first. Invitation tokens are short-lived, single-use credentials, and
    a pre-005 row has neither an addressee nor an inviter, so it could never be
    redeemed anyway. Deleting them is the only way to reach the NOT NULL shape.
    """
    # Add missing columns, nullable to start with.
    op.add_column('invitation_tokens',
        sa.Column('email', sa.String(255), nullable=True, comment='Email address of the invited user')
    )
    op.add_column('invitation_tokens',
        sa.Column('role', sa.String(50), nullable=True, comment='Role to assign to the user (user, admin, super_admin)')
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

    # Indexes: the model declares both families — `idx_*` via __table_args__ and
    # `ix_*` via index=True on the columns — and production carries both.
    op.create_index('idx_invitation_tokens_email', 'invitation_tokens', ['email'])
    op.create_index('idx_invitation_tokens_invited_by_id', 'invitation_tokens', ['invited_by_id'])
    op.create_index('ix_invitation_tokens_email', 'invitation_tokens', ['email'])
    op.create_index('ix_invitation_tokens_invited_by_id', 'invitation_tokens', ['invited_by_id'])

    # Rename token_id column to id
    op.alter_column('invitation_tokens', 'token_id', new_column_name='id')

    # Backfill role, then tighten every column to the model's nullability.
    # Rows with no email/inviter are unredeemable; drop them (see docstring).
    op.execute("UPDATE invitation_tokens SET role = 'user' WHERE role IS NULL")
    op.execute("DELETE FROM invitation_tokens WHERE email IS NULL OR invited_by_id IS NULL")
    op.alter_column('invitation_tokens', 'role', nullable=False)
    op.alter_column('invitation_tokens', 'email', nullable=False)
    op.alter_column('invitation_tokens', 'invited_by_id', nullable=False)

    # Make user_id nullable (it's only set after invitation is accepted)
    op.alter_column('invitation_tokens', 'user_id', nullable=True)

    # CHECK constraints declared by the model
    op.create_check_constraint(
        'valid_invitation_role',
        'invitation_tokens',
        "role IN ('user', 'admin', 'super_admin')"
    )
    op.create_check_constraint(
        'invitation_email_format',
        'invitation_tokens',
        r"email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$'"
    )


def downgrade() -> None:
    """Revert invitation_tokens table changes."""

    # Drop CHECK constraints
    op.drop_constraint('invitation_email_format', 'invitation_tokens', type_='check')
    op.drop_constraint('valid_invitation_role', 'invitation_tokens', type_='check')

    # Rename id back to token_id
    op.alter_column('invitation_tokens', 'id', new_column_name='token_id')

    # Drop indexes
    op.drop_index('ix_invitation_tokens_invited_by_id', table_name='invitation_tokens')
    op.drop_index('ix_invitation_tokens_email', table_name='invitation_tokens')
    op.drop_index('idx_invitation_tokens_invited_by_id', table_name='invitation_tokens')
    op.drop_index('idx_invitation_tokens_email', table_name='invitation_tokens')

    # Drop foreign key constraint
    op.drop_constraint('invitation_tokens_invited_by_id_fkey', 'invitation_tokens', type_='foreignkey')

    # Drop columns
    op.drop_column('invitation_tokens', 'invited_by_id')
    op.drop_column('invitation_tokens', 'role')
    op.drop_column('invitation_tokens', 'email')

    # user_id was NOT NULL before this revision
    op.alter_column('invitation_tokens', 'user_id', nullable=False)
