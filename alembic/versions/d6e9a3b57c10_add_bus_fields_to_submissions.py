"""add bus fields to submissions

Revision ID: d6e9a3b57c10
Revises: c5d8f2a41b7e
Create Date: 2026-10-02 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d6e9a3b57c10"
down_revision: Union[str, Sequence[str], None] = "c5d8f2a41b7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable: rows that exist today have no bus details.
    op.add_column("submissions", sa.Column("public_bus_id", sa.String(length=16), nullable=True))
    op.add_column("submissions", sa.Column("bus_number_going", sa.String(length=50), nullable=True))
    op.add_column("submissions", sa.Column("bus_number_return", sa.String(length=50), nullable=True))

    op.create_check_constraint(
        "ck_submissions_public_bus_id_format",
        "submissions",
        "public_bus_id ~ '^[0-9]{16}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_submissions_public_bus_id_format", "submissions", type_="check")
    op.drop_column("submissions", "bus_number_return")
    op.drop_column("submissions", "bus_number_going")
    op.drop_column("submissions", "public_bus_id")