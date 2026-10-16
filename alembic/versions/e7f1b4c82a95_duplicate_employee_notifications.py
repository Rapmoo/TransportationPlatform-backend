from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e7f1b4c82a95"
down_revision: Union[str, Sequence[str], None] = "d6e9a3b57c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "duplicate_employee_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["acknowledged_by_id"], ["users.id"]),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'ACKNOWLEDGED')",
            name="ck_duplicate_notifications_status",
        ),
        sa.CheckConstraint(
            "(status = 'ACKNOWLEDGED') = (acknowledged_at IS NOT NULL)",
            name="ck_duplicate_notifications_ack_iff_acknowledged",
        ),
    )
    op.create_index(op.f("ix_duplicate_employee_notifications_id"), "duplicate_employee_notifications", ["id"])
    op.create_index(op.f("ix_duplicate_employee_notifications_employee_id"), "duplicate_employee_notifications", ["employee_id"])
    op.create_index(op.f("ix_duplicate_employee_notifications_status"), "duplicate_employee_notifications", ["status"])
    op.create_index(
        "uq_duplicate_notifications_one_active_per_employee_id",
        "duplicate_employee_notifications",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_duplicate_notifications_one_active_per_employee_id", table_name="duplicate_employee_notifications")
    op.drop_index(op.f("ix_duplicate_employee_notifications_status"), table_name="duplicate_employee_notifications")
    op.drop_index(op.f("ix_duplicate_employee_notifications_employee_id"), table_name="duplicate_employee_notifications")
    op.drop_index(op.f("ix_duplicate_employee_notifications_id"), table_name="duplicate_employee_notifications")
    op.drop_table("duplicate_employee_notifications")