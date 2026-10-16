from datetime import datetime, timedelta
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.change_request import ChangeRequest, ChangeRequestStatus
from app.models.submission import Submission
from app.models.user import User
from app.services.audit import create_audit_log

# Total requests per submission, whatever their final status.
MAX_REQUESTS_PER_SUBMISSION = 2

MAX_REQUESTS_MESSAGE = (
    "Maximum edit requests reached. You can only request to edit this form twice."
)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def get_active_grant(
    db: Session, submission_id: int, now: datetime | None = None
) -> ChangeRequest | None:
    """The request that currently lets the worker edit, or None."""
    now = now or datetime.utcnow()

    request = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.submission_id == submission_id,
            ChangeRequest.status == ChangeRequestStatus.APPROVED,
        )
        .first()
    )

    return request if request and request.grants_edit(now) else None


def expire_stale_requests(
    db: Session, submission_id: int, now: datetime | None = None
) -> int:
    """APPROVED requests whose edit window has ended become EXPIRED, so they
    stop counting as "open" (the unique index would block new requests)."""
    now = now or datetime.utcnow()

    stale = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.submission_id == submission_id,
            ChangeRequest.status == ChangeRequestStatus.APPROVED,
            ChangeRequest.completed_at.is_(None),
            ChangeRequest.edit_expires_at <= now,
        )
        .all()
    )

    for request in stale:
        request.status = ChangeRequestStatus.EXPIRED
        create_audit_log(
            db=db,
            action="CHANGE_REQUEST_EXPIRED",
            submission_id=submission_id,
            old_status=ChangeRequestStatus.APPROVED,
            new_status=ChangeRequestStatus.EXPIRED,
            details="Edit window ended before the worker resubmitted.",
        )

    db.flush()

    return len(stale)


def create_change_request(
    db: Session,
    submission: Submission,
    submission_number: str,
    worker_comment: str,
) -> ChangeRequest:
    """Create a PENDING request. Caller commits."""
    # Lock the submission row so concurrent requests are counted one at a time.
    locked = (
        db.query(Submission)
        .filter(Submission.id == submission.id)
        .with_for_update()
        .one()
    )

    if locked.submission_number != submission_number:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Submission number does not match this submission.",
        )

    if locked.status != "SUBMITTED":
        raise _conflict(
            "This submission has not been submitted yet, so it can still be edited directly."
        )

    expire_stale_requests(db, locked.id)

    existing = (
        db.query(ChangeRequest).filter(ChangeRequest.submission_id == locked.id).all()
    )

    if any(r.status in ChangeRequestStatus.OPEN for r in existing):
        raise _conflict("You already have an open change request for this submission.")

    if len(existing) >= MAX_REQUESTS_PER_SUBMISSION:
        raise _conflict(MAX_REQUESTS_MESSAGE)

    request = ChangeRequest(
        submission_id=locked.id,
        worker_comment=worker_comment,
        status=ChangeRequestStatus.PENDING,
    )
    db.add(request)

    create_audit_log(
        db=db,
        action="WORKER_CHANGE_REQUEST",
        submission_id=locked.id,
        new_status=ChangeRequestStatus.PENDING,
        details=f"Worker requested a change to a submitted form (request {len(existing) + 1} of {MAX_REQUESTS_PER_SUBMISSION}).",
    )

    return request


def review_change_request(
    db: Session,
    public_id: uuid.UUID,
    reviewer: User,
    approve: bool,
    review_comment: str | None,
) -> ChangeRequest:
    """Approve or reject a PENDING request. Decisions are final. Commits."""
    request = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.public_id == public_id)
        .with_for_update()
        .first()
    )

    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Change request not found.",
        )

    if request.status != ChangeRequestStatus.PENDING:
        raise _conflict(
            f"This request was already decided ({request.status}) and cannot be reviewed again."
        )

    now = datetime.utcnow()
    old_status = request.status

    request.status = (
        ChangeRequestStatus.APPROVED if approve else ChangeRequestStatus.REJECTED
    )
    request.reviewed_at = now
    request.reviewed_by_id = reviewer.id
    request.review_comment = review_comment

    if approve:
        request.edit_expires_at = now + timedelta(
            hours=settings.CHANGE_REQUEST_EDIT_WINDOW_HOURS
        )

    create_audit_log(
        db=db,
        action="CHANGE_REQUEST_APPROVED" if approve else "CHANGE_REQUEST_REJECTED",
        actor=reviewer,
        submission_id=request.submission_id,
        old_status=old_status,
        new_status=request.status,
        details=(
            f"{'Approved' if approve else 'Rejected'} a worker's correction request"
            + (f": {review_comment}" if review_comment else ".")
        ),
    )

    db.commit()
    db.refresh(request)

    return request


def complete_edit(db: Session, submission: Submission) -> ChangeRequest:
    """Call from the worker's resubmit endpoint, in the same transaction as the
    save. Consumes the approval: it becomes COMPLETED and cannot be reused.
    Caller commits."""
    now = datetime.utcnow()

    request = (
        db.query(ChangeRequest)
        .filter(
            ChangeRequest.submission_id == submission.id,
            ChangeRequest.status == ChangeRequestStatus.APPROVED,
        )
        .with_for_update()
        .first()
    )

    if request is None or not request.grants_edit(now):
        raise _conflict(
            "This submission is locked. Request permission from a supervisor to edit it."
        )

    request.status = ChangeRequestStatus.COMPLETED
    request.completed_at = now

    create_audit_log(
        db=db,
        action="CHANGE_REQUEST_COMPLETED",
        submission_id=submission.id,
        old_status=ChangeRequestStatus.APPROVED,
        new_status=ChangeRequestStatus.COMPLETED,
        details="Worker resubmitted the corrected form; form locked again.",
    )

    return request