import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

if TYPE_CHECKING:
    from app.models.change_request import ChangeRequest


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # Always exactly 9 digits, never starting with 0 (so Excel/CSV
        # exports cannot silently drop a leading zero).
        CheckConstraint(
            "submission_number ~ '^[1-9][0-9]{8}$'",
            name="ck_submissions_submission_number_format",
        ),
        # Public bus ID: exactly 16 digits. Leading zeros are allowed, which
        # is why it is stored as text and not as a number.
        CheckConstraint(
            "public_bus_id ~ '^[0-9]{16}$'",
            name="ck_submissions_public_bus_id_format",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # Non-guessable identifier used in every worker-facing URL.
    # The integer `id` stays internal (foreign keys, staff/admin routes).
    public_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        unique=True,
        nullable=False,
        index=True,
        default=uuid.uuid4,
    )

    # Worker-supplied identifier of the employee. Deliberately NOT unique:
    # several submissions may carry the same value, and that is exactly how
    # duplicates are detected (see app/services/duplicates.py). The plain
    # index below makes the GROUP BY / lookup fast.
    employee_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    # Backend-generated 9-digit number that identifies this specific
    # submission. NULL while the submission is a DRAFT; assigned once, when
    # the worker submits (see app/services/submission_number.py). Unrelated
    # to employee_id. Stored as text so it stays exactly 9 characters.
    submission_number: Mapped[str | None] = mapped_column(
        String(9),
        unique=True,
        nullable=True,
        index=True,
    )

    employee_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    organization_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    phone_number: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    # Bus details. Nullable only so rows created before these columns
    # existed stay valid; the worker API requires all three.
    public_bus_id: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )

    bus_number_going: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    bus_number_return: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="DRAFT",
        index=True,
    )

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # A submission can have many requests over its lifetime (oldest first);
    # at most one of them is open (PENDING / APPROVED) at any time.
    change_requests: Mapped[list["ChangeRequest"]] = relationship(
        back_populates="submission",
        order_by="ChangeRequest.created_at",
    )