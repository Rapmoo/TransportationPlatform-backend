
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class ChangeRequestStatus(str):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"

    ALL = {
        PENDING,
        APPROVED,
        REJECTED,
        EXPIRED,
        COMPLETED,
    }

    OPEN = {
        PENDING,
        APPROVED,
    }

    @classmethod
    def open_statuses(cls) -> set[str]:
        return cls.OPEN


class ChangeRequest(Base):
    __tablename__ = "change_requests"

    __table_args__ = (
        Index(
            "uq_change_requests_one_open_per_submission",
            "submission_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'APPROVED')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    public_id: Mapped[uuid.UUID] = mapped_column(
        unique=True,
        index=True,
        nullable=False,
        default=uuid.uuid4,
    )

    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id"),
        nullable=False,
        index=True,
    )

    worker_comment: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ChangeRequestStatus.PENDING,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,

    )

    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    review_comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    edit_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    submission = relationship(
        "Submission",
        back_populates="change_requests",
    )

    reviewed_by = relationship(
        "User",
        foreign_keys=[reviewed_by_id],
    )

    def grants_edit(self, now: datetime) -> bool:
        return (
            self.status == ChangeRequestStatus.APPROVED
            and self.completed_at is None
            and self.edit_expires_at is not None
            and self.edit_expires_at > now
        )
