"""rename user email to username

Revision ID: a7c3d9e15f42
Revises: e1a832b28b28
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a7c3d9e15f42"
down_revision: Union[str, Sequence[str], None] = "e1a832b28b28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Existing users keep their old email value as their username."""
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.alter_column("users", "email", new_column_name="username")
    op.create_index(
        op.f("ix_users_username"),
        "users",
        ["username"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.alter_column("users", "username", new_column_name="email")
    op.create_index(
        op.f("ix_users_email"),
        "users",
        ["email"],
        unique=True,
    )