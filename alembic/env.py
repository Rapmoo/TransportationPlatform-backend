from logging.config import fileConfig

from sqlalchemy import create_engine, pool, text

from alembic import context

from app.database.base import Base
from app.models import Submission, Location, User, AuditLog, ChangeRequest
from app.database.session import settings


config = context.config

config.set_main_option(
    "sqlalchemy.url",
    settings.DATABASE_URL.replace("%", "%%"),
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


MANUAL_INDEXES = {
    "ix_submissions_created_at",
    "ix_submissions_organization_name",
    "ix_submissions_submitted_time",
    "ix_submissions_employee_id_trgm",
    "ix_submissions_submission_number_trgm",
    "ix_submissions_employee_name_trgm",
    "ix_submissions_organization_name_trgm",
    "ix_submissions_phone_number_trgm",
    "ix_submissions_public_bus_id_trgm",
    "ix_submissions_bus_number_going_trgm",
    "ix_submissions_bus_number_return_trgm",
    "ix_locations_address_text_trgm",
}


def include_object(object, name, type_, reflected, compare_to):
    """
    Prevent Alembic from managing PostGIS system objects
    and the explicitly managed spatial index.
    """

    # spatial_ref_sys is managed by PostGIS
    if type_ == "table" and reflected and name == "spatial_ref_sys":
        return False

    # idx_locations_geom is created explicitly in our migration
    if type_ == "index" and name == "idx_locations_geom":
        return False

    # Performance indexes created by hand (CONCURRENTLY, pg_trgm, expression
    # index) in f3b8d2a6c9e1. They are not declared on the models, so without
    # this autogenerate would propose dropping them.
    if type_ == "index" and name in MANUAL_INDEXES:
        return False

    return True


def run_migrations_offline() -> None:
    """Run migrations in offline mode."""

    url = config.get_main_option("sqlalchemy.url")

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=False,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in online mode."""

    connectable = create_engine(
        settings.DATABASE_URL,
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=False,
            include_object=include_object,
        )

        with context.begin_transaction():
            connection.execute(
                text("SET LOCAL search_path TO public")
            )

            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()