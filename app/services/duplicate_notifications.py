from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.duplicate_employee_notification import (
    DuplicateEmployeeNotification,
    DuplicateNotificationStatus,
)
from app.models.submission import Submission
from app.services.audit import create_audit_log
from app.services.duplicates import get_group_submissions, is_duplicate_employee_id


def notify_if_duplicate(
    db: Session, submission: Submission
) -> DuplicateEmployeeNotification | None:
    """Call after `submission` became SUBMITTED, inside the same transaction.

    Returns a new notification only when the duplicate is newly detected.
    If an ACTIVE one already exists for this employee ID, nothing new is
    created (the supervisor sees all members live anyway).
    """
    # Serialise concurrent submits of the same employee ID: the second one
    # waits for the first to commit, so it sees it and detects the duplicate.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"duplicate-employee-id:{submission.employee_id}"},
    )

    if not is_duplicate_employee_id(db, submission):
        return None

    numbers = ", ".join(
        s.submission_number or "-"
        for s in get_group_submissions(db, submission.employee_id)
    )
    create_audit_log(
        db=db,
        action="DUPLICATE_EMPLOYEE_ID_DETECTED",
        submission_id=submission.id,
        details=(
            f"Employee ID {submission.employee_id} is used by multiple "
            f"submissions: {numbers}."
        ),
    )

    active = (
        db.query(DuplicateEmployeeNotification.id)
        .filter(
            DuplicateEmployeeNotification.employee_id == submission.employee_id,
            DuplicateEmployeeNotification.status == DuplicateNotificationStatus.ACTIVE,
        )
        .first()
    )
    if active:
        return None

    notification = DuplicateEmployeeNotification(employee_id=submission.employee_id)
    db.add(notification)
    db.flush()
    return notification