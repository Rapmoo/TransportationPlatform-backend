"""add public_id (UUID) to submissions

Revision ID: b4e7c1a90d3f
Revises: a7c3d9e15f42
Create Date: 2026-09-29 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b4e7c1a90d3f"
down_revision: Union[str, Sequence[str], None] = "a7c3d9e15f42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. add as nullable so existing rows are allowed
    op.add_column("submissions", sa.Column("public_id", sa.Uuid(), nullable=True))

    # 2. backfill existing rows with a random UUID4 (built in since PostgreSQL 13)
    op.execute("UPDATE submissions SET public_id = gen_random_uuid() WHERE public_id IS NULL")

    # 3. enforce NOT NULL + uniqueness
    op.alter_column("submissions", "public_id", nullable=False)
    op.create_index("ix_submissions_public_id", "submissions", ["public_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_submissions_public_id", table_name="submissions")
    op.drop_column("submissions", "public_id")