from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.submission import Submission

# A draft is a worker still filling in the form; it is not a submission yet,
# so it does not take part in duplicate detection.
_COUNTED = Submission.status != "DRAFT"


def count_same_employee_id(db: Session, submission: Submission) -> int:
    """How many OTHER submitted records carry the same employee ID."""
    return (
        db.query(func.count(Submission.id))
        .filter(
            Submission.employee_id == submission.employee_id,
            Submission.id != submission.id,
            _COUNTED,
        )
        .scalar()
        or 0
    )


def is_duplicate_employee_id(db: Session, submission: Submission) -> bool:
    return count_same_employee_id(db, submission) > 0


def find_duplicate_employee_ids(db: Session) -> list[tuple[str, int]]:
    """Every employee ID used by more than one submitted record, with the
    number of records, biggest groups first.

    Duplicates are derived from the data rather than stored as a flag, so
    they can never go stale when a submission is edited, rejected or
    corrected. ix_submissions_employee_id keeps this query fast.
    """
    rows = (
        db.query(Submission.employee_id, func.count(Submission.id))
        .filter(_COUNTED)
        .group_by(Submission.employee_id)
        .having(func.count(Submission.id) > 1)
        .order_by(func.count(Submission.id).desc(), Submission.employee_id)
        .all()
    )

    return [(employee_id, count) for employee_id, count in rows]


def get_group_submissions(db: Session, employee_id: str) -> list[Submission]:
    """All submitted (non-draft) records for one employee ID, oldest first."""
    return (
        db.query(Submission)
        .filter(Submission.employee_id == employee_id, _COUNTED)
        .order_by(
            func.coalesce(Submission.submitted_at, Submission.created_at),
            Submission.id,
        )
        .all()
    )