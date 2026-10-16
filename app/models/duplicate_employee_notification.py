from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class DuplicateNotificationStatus:
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class DuplicateEmployeeNotification(Base):
    """Supervisor-only warning that an employee ID is used by several
    submissions. Which submissions belong to it is derived live from
    `submissions`; this row only records that the warning was raised, and
    whether the supervisor has acknowledged it. Rows are never deleted."""

    __tablename__ = "duplicate_employee_notifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'ACKNOWLEDGED')",
            name="ck_duplicate_notifications_status",
        ),
        CheckConstraint(
            "(status = 'ACKNOWLEDGED') = (acknowledged_at IS NOT NULL)",
            name="ck_duplicate_notifications_ack_iff_acknowledged",
        ),
        # At most one ACTIVE notification per employee ID.
        Index(
            "uq_duplicate_notifications_one_active_per_employee_id",
            "employee_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    employee_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DuplicateNotificationStatus.ACTIVE, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )