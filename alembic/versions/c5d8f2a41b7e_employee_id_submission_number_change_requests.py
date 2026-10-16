import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5d8f2a41b7e"
down_revision: Union[str, Sequence[str], None] = "b4e7c1a90d3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _random_number() -> str:
    return str(100_000_000 + secrets.randbelow(900_000_000))


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. faida_number -> employee_id (not unique)
    # ------------------------------------------------------------------
    op.drop_index(op.f("ix_submissions_faida_number"), table_name="submissions")
    op.alter_column("submissions", "faida_number", new_column_name="employee_id")

    op.execute(
        r"UPDATE submissions "
        r"SET employee_id = upper(btrim(regexp_replace(employee_id, '\s+', ' ', 'g')))"
    )

    op.create_index(
        op.f("ix_submissions_employee_id"),
        "submissions",
        ["employee_id"],
        unique=False,
    )

    # ------------------------------------------------------------------
    # 2. submission_number
    # ------------------------------------------------------------------
    op.add_column(
        "submissions",
        sa.Column("submission_number", sa.String(length=9), nullable=True),
    )

    # Backfill: every record that has already left DRAFT gets a number.
    # Drafts get theirs when the worker submits.
    submission_ids = (
        bind.execute(
            sa.text("SELECT id FROM submissions WHERE status <> 'DRAFT' ORDER BY id")
        )
        .scalars()
        .all()
    )

    used: set[str] = set()
    assignments = []

    for submission_id in submission_ids:
        number = _random_number()

        while number in used:
            number = _random_number()

        used.add(number)
        assignments.append({"id": submission_id, "number": number})

    if assignments:
        bind.execute(
            sa.text("UPDATE submissions SET submission_number = :number WHERE id = :id"),
            assignments,
        )

    op.create_index(
        op.f("ix_submissions_submission_number"),
        "submissions",
        ["submission_number"],
        unique=True,
    )

    op.create_check_constraint(
        "ck_submissions_submission_number_format",
        "submissions",
        "submission_number ~ '^[1-9][0-9]{8}$'",
    )

    # ------------------------------------------------------------------
    # 3. change_requests
    # ------------------------------------------------------------------
    op.create_table(
        "change_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("worker_comment", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("edit_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'COMPLETED', 'EXPIRED')",
            name="ck_change_requests_status",
        ),
        sa.CheckConstraint(
            "(status = 'PENDING') = (reviewed_at IS NULL)",
            name="ck_change_requests_reviewed_iff_decided",
        ),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR edit_expires_at IS NOT NULL",
            name="ck_change_requests_approved_has_edit_window",
        ),
    )

    op.create_index(op.f("ix_change_requests_id"), "change_requests", ["id"])
    op.create_index(
        op.f("ix_change_requests_public_id"),
        "change_requests",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_change_requests_submission_id"), "change_requests", ["submission_id"]
    )
    op.create_index(op.f("ix_change_requests_status"), "change_requests", ["status"])
    op.create_index(
        op.f("ix_change_requests_reviewed_by_id"), "change_requests", ["reviewed_by_id"]
    )
    op.create_index(
        "uq_change_requests_one_open_per_submission",
        "change_requests",
        ["submission_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'APPROVED')"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # faida_number was UNIQUE. Duplicate employee IDs are legal now, so the
    # unique index can only be restored if none exist.
    duplicate = bind.execute(
        sa.text(
            "SELECT employee_id FROM submissions "
            "GROUP BY employee_id HAVING count(*) > 1 LIMIT 1"
        )
    ).first()

    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade: employee_id '%s' is used by more than one "
            "submission, so the old UNIQUE faida_number index cannot be "
            "recreated. Resolve the duplicates first." % duplicate[0]
        )

    # 3. change_requests
    op.drop_index(
        "uq_change_requests_one_open_per_submission", table_name="change_requests"
    )
    op.drop_index(op.f("ix_change_requests_reviewed_by_id"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_status"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_submission_id"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_public_id"), table_name="change_requests")
    op.drop_index(op.f("ix_change_requests_id"), table_name="change_requests")
    op.drop_table("change_requests")

    # 2. submission_number
    op.drop_constraint(
        "ck_submissions_submission_number_format", "submissions", type_="check"
    )
    op.drop_index(op.f("ix_submissions_submission_number"), table_name="submissions")
    op.drop_column("submissions", "submission_number")

    # 1. employee_id -> faida_number (unique again)
    op.drop_index(op.f("ix_submissions_employee_id"), table_name="submissions")
    op.alter_column("submissions", "employee_id", new_column_name="faida_number")
    op.create_index(
        op.f("ix_submissions_faida_number"),
        "submissions",
        ["faida_number"],
        unique=True,
    )