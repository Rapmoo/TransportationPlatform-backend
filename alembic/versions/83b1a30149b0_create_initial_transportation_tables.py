"""create initial transportation tables

Revision ID: 83b1a30149b0
Revises:
Create Date: 2026-09-22 09:25:40.765506

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import geoalchemy2


# revision identifiers, used by Alembic.
revision: str = "83b1a30149b0"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create initial transportation tables."""

    # ---------------------------------------------------------
    # Submissions
    # ---------------------------------------------------------
    op.create_table(
        "submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("faida_number", sa.String(length=50), nullable=False),
        sa.Column("employee_name", sa.String(length=255), nullable=False),
        sa.Column("organization_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_submissions_faida_number"),
        "submissions",
        ["faida_number"],
        unique=True,
    )

    op.create_index(
        op.f("ix_submissions_id"),
        "submissions",
        ["id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_submissions_status"),
        "submissions",
        ["status"],
        unique=False,
    )

    # ---------------------------------------------------------
    # Users
    # ---------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_users_email"),
        "users",
        ["email"],
        unique=True,
    )

    op.create_index(
        op.f("ix_users_id"),
        "users",
        ["id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_users_role"),
        "users",
        ["role"],
        unique=False,
    )

    # ---------------------------------------------------------
    # Audit logs
    # ---------------------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_role", sa.String(length=50), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("old_status", sa.String(length=50), nullable=True),
        sa.Column("new_status", sa.String(length=50), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_audit_logs_actor_id"),
        "audit_logs",
        ["actor_id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_audit_logs_id"),
        "audit_logs",
        ["id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_audit_logs_submission_id"),
        "audit_logs",
        ["submission_id"],
        unique=False,
    )

    # ---------------------------------------------------------
    # Locations
    # ---------------------------------------------------------
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submission_id", sa.Integer(), nullable=False),
        sa.Column("location_type", sa.String(length=50), nullable=False),
        sa.Column("address_text", sa.String(length=500), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
                spatial_index=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        op.f("ix_locations_id"),
        "locations",
        ["id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_locations_submission_id"),
        "locations",
        ["submission_id"],
        unique=False,
    )

    # Explicit GiST spatial index for PostGIS geometry.
    op.create_index(
        "idx_locations_geom",
        "locations",
        ["geom"],
        unique=False,
        postgresql_using="gist",
    )


def downgrade() -> None:
    """Drop initial transportation tables."""

    op.drop_index(
        "idx_locations_geom",
        table_name="locations",
        postgresql_using="gist",
    )

    op.drop_index(
        op.f("ix_locations_submission_id"),
        table_name="locations",
    )

    op.drop_index(
        op.f("ix_locations_id"),
        table_name="locations",
    )

    op.drop_table("locations")

    op.drop_index(
        op.f("ix_audit_logs_submission_id"),
        table_name="audit_logs",
    )

    op.drop_index(
        op.f("ix_audit_logs_id"),
        table_name="audit_logs",
    )

    op.drop_index(
        op.f("ix_audit_logs_actor_id"),
        table_name="audit_logs",
    )

    op.drop_table("audit_logs")

    op.drop_index(
        op.f("ix_users_role"),
        table_name="users",
    )

    op.drop_index(
        op.f("ix_users_id"),
        table_name="users",
    )

    op.drop_index(
        op.f("ix_users_email"),
        table_name="users",
    )

    op.drop_table("users")

    op.drop_index(
        op.f("ix_submissions_status"),
        table_name="submissions",
    )

    op.drop_index(
        op.f("ix_submissions_id"),
        table_name="submissions",
    )

    op.drop_index(
        op.f("ix_submissions_faida_number"),
        table_name="submissions",
    )

    op.drop_table("submissions")