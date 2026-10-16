import logging
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# Settings now live in app/core/config.py. `settings` is re-exported here so
# existing imports (e.g. alembic/env.py) keep working.
from app.core.config import settings

logger = logging.getLogger("app.db_pool")

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=1800,
    # Fail in 5 s instead of hanging if the database is unreachable.
    connect_args={"connect_timeout": 5},
    hide_parameters=True,
)


@event.listens_for(engine.pool, "checkout")
def log_pool_checkout(dbapi_connection, connection_record, connection_proxy):
    connection_record.info["pool_checkout_started"] = time.monotonic()

    checked_out = engine.pool.checkedout()
    pool_limit = settings.DB_POOL_SIZE

    if checked_out >= pool_limit:
        logger.warning(
            "DB pool at or above base capacity: checked_out=%s "
            "pool_size=%s overflow=%s",
            checked_out,
            pool_limit,
            engine.pool.overflow(),
        )


@event.listens_for(engine.pool, "checkin")
def log_pool_checkin(dbapi_connection, connection_record):
    started = connection_record.info.pop("pool_checkout_started", None)

    if started is None:
        return

    held_ms = (time.monotonic() - started) * 1000

    if held_ms >= 500:
        logger.warning(
            "DB connection held for %.0f ms; %s",
            held_ms,
            engine.pool.status(),
        )


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)
