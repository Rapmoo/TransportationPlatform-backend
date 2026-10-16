
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d2a6c9e1"
down_revision: Union[str, Sequence[str], None] = "e7f1b4c82a95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TRIGRAM_INDEXES = [
    ("ix_submissions_employee_id_trgm", "submissions", "employee_id"),
    ("ix_submissions_submission_number_trgm", "submissions", "submission_number"),
    ("ix_submissions_employee_name_trgm", "submissions", "employee_name"),
    ("ix_submissions_organization_name_trgm", "submissions", "organization_name"),
    ("ix_submissions_phone_number_trgm", "submissions", "phone_number"),
    ("ix_submissions_public_bus_id_trgm", "submissions", "public_bus_id"),
    ("ix_submissions_bus_number_going_trgm", "submissions", "bus_number_going"),
    ("ix_submissions_bus_number_return_trgm", "submissions", "bus_number_return"),
    ("ix_locations_address_text_trgm", "locations", "address_text"),
]


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("SET search_path TO public")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA public")

        op.create_index(
            "ix_submissions_created_at",
            "submissions",
            ["created_at"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_submissions_organization_name",
            "submissions",
            ["organization_name"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "ix_submissions_submitted_time",
            "submissions",
            [sa.text("coalesce(submitted_at, created_at)")],
            postgresql_concurrently=True,
        )

        for name, table, column in _TRIGRAM_INDEXES:
            op.create_index(
                name,
                table,
                [column],
                postgresql_using="gin",
                postgresql_ops={column: "gin_trgm_ops"},
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET search_path TO public")

        for name, table, _column in reversed(_TRIGRAM_INDEXES):
            op.drop_index(name, table_name=table, postgresql_concurrently=True)

        op.drop_index(
            "ix_submissions_submitted_time",
            table_name="submissions",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_submissions_organization_name",
            table_name="submissions",
            postgresql_concurrently=True,
        )
        op.drop_index(
            "ix_submissions_created_at",
            table_name="submissions",
            postgresql_concurrently=True,
        )
        # pg_trgm is left installed: other objects may rely on it.