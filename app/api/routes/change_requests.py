import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

import uuid
from app.api.dependencies import get_db, get_worker_submission, require_supervisor
from app.core.config import settings
from app.core.rate_limit import get_client_ip, ip_rate_limit, limiter
from app.core.security import create_worker_token
from app.models.location import Location
from app.models.change_request import ChangeRequest, ChangeRequestStatus
from app.models.submission import Submission
from app.models.user import User
from app.schemas.change_request import (
    ChangeRequestCreate,
    ChangeRequestReview,
    PublicChangeRequestCreate,
    PublicEditSession,
    PublicLookup,
    PublicStatusResponse,
    StaffChangeRequestResponse,
    WorkerChangeRequestResponse,
)
from app.services import change_requests as change_request_service


router = APIRouter(
    prefix="/submissions",
    tags=["Change requests"],
)

@router.post(
    "/{public_id:uuid}/change-requests",
    response_model=WorkerChangeRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_change_request(
    data: ChangeRequestCreate,
    submission: Submission = Depends(get_worker_submission),
    db: Session = Depends(get_db),
):

    change_request = change_request_service.create_change_request(
        db,
        submission,
        data.submission_number,
        data.worker_comment,
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an open change request for this submission.",
        )

    db.refresh(change_request)

    return change_request


@router.get(
    "/{public_id:uuid}/change-requests",
    response_model=list[WorkerChangeRequestResponse],
)
def list_my_change_requests(
    submission: Submission = Depends(get_worker_submission),
    db: Session = Depends(get_db),
):
    return (
        db.query(ChangeRequest)
        .filter(ChangeRequest.submission_id == submission.id)
        .order_by(ChangeRequest.created_at.desc())
        .all()
    )


staff_router = APIRouter(
    prefix="/change-requests",
    tags=["Change requests (supervisor)"],
)


def _staff_view(request: ChangeRequest) -> StaffChangeRequestResponse:
    submission = request.submission

    return StaffChangeRequestResponse(
        public_id=request.public_id,
        status=request.status,
        employee_name=submission.employee_name,
        employee_id=submission.employee_id,
        submission_number=submission.submission_number,
        worker_comment=request.worker_comment,
        created_at=request.created_at,
        reviewed_at=request.reviewed_at,
        review_comment=request.review_comment,
        edit_expires_at=request.edit_expires_at,
        completed_at=request.completed_at,
    )


@staff_router.get("", response_model=list[StaffChangeRequestResponse])
def list_change_requests(
    request_status: str | None = Query(ChangeRequestStatus.PENDING, alias="status"),
    current_user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    """Pending requests by default; pass ?status=APPROVED etc. to filter."""
    query = db.query(ChangeRequest).options(joinedload(ChangeRequest.submission))

    if request_status:
        if request_status not in ChangeRequestStatus.ALL:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Unknown status.",
            )
        query = query.filter(ChangeRequest.status == request_status)

    return [_staff_view(r) for r in query.order_by(ChangeRequest.created_at).all()]


@staff_router.post("/{public_id:uuid}/approve", response_model=StaffChangeRequestResponse)
def approve_change_request(
    public_id: uuid.UUID,
    data: ChangeRequestReview,
    current_user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    request = change_request_service.review_change_request(
        db, public_id, current_user, True, data.review_comment
    )
    return _staff_view(request)


@staff_router.post("/{public_id:uuid}/reject", response_model=StaffChangeRequestResponse)
def reject_change_request(
    public_id: uuid.UUID,
    data: ChangeRequestReview,
    current_user: User = Depends(require_supervisor),
    db: Session = Depends(get_db),
):
    request = change_request_service.review_change_request(
        db, public_id, current_user, False, data.review_comment
    )
    return _staff_view(request)


# ---------------------------------------------------------------------------
# Public (no login): a worker uses the landing page with the submission number
# ---------------------------------------------------------------------------

public_router = APIRouter(
    prefix="/change-requests",
    tags=["Change requests (public)"],
    # Every public endpoint: overall request cap per client IP.
    dependencies=[
        Depends(
            ip_rate_limit(
                "public-change-requests",
                settings.PUBLIC_IP_LIMIT,
                settings.PUBLIC_IP_WINDOW_SECONDS,
            )
        )
    ],
)

# Failed lookups are throttled so submission numbers cannot be brute-forced: at most _MAX_FAILURES wrong guesses per client
# and per submission number in _WINDOW_SECONDS. Correct lookups are not counted.
# Counters live in the shared limiter (app/core/rate_limit.py): per-process
# memory by default, Redis when REDIS_URL is set.
_WINDOW_SECONDS = settings.LOOKUP_FAILURE_WINDOW_SECONDS
_MAX_FAILURES = settings.LOOKUP_FAILURE_LIMIT


def _digits_tail(value: str | None) -> str:
    """Last 9 digits, so 0912345678 and +251912345678 compare equal."""
    return re.sub(r"\D", "", value or "")[-9:]


def _verification_matches(submission: Submission, verification: str) -> bool:
    entered_id = re.sub(r"\s+", " ", verification).strip().upper()

    if entered_id and entered_id == (submission.employee_id or "").strip().upper():
        return True

    entered_phone = _digits_tail(verification)
    stored_phone = _digits_tail(submission.phone_number)

    return len(entered_phone) == 9 and entered_phone == stored_phone


def _verified_submission(db: Session, request: Request, data: PublicLookup) -> Submission:
    client = f"lookup-fail:ip:{get_client_ip(request)}"
    number = f"lookup-fail:num:{data.submission_number}"

    if settings.RATE_LIMIT_ENABLED and (
        limiter.count(client, _WINDOW_SECONDS) >= _MAX_FAILURES
        or limiter.count(number, _WINDOW_SECONDS) >= _MAX_FAILURES
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please try again in a few minutes.",
        )

    submission = (
        db.query(Submission)
        .filter(Submission.submission_number == data.submission_number)
        .first()
    )

    # Same answer for "no such number" and "wrong phone/ID", so nobody can use
    # these endpoints to find out which submission numbers exist.
    # The submission number alone identifies the submission. A phone number /
    # Fayda ID is only checked when the client chooses to send one.
    if not submission or (
        data.verification and not _verification_matches(submission, data.verification)
    ):
        limiter.add(client, _WINDOW_SECONDS)
        limiter.add(number, _WINDOW_SECONDS)

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No submission matches those details.",
        )

    return submission


_SUBMISSION_FIELD_ATTRS = {
    "fayda_id": "employee_id",
    "phone_number": "phone_number",
    "organization": "organization_name",
    "public_bus_id": "public_bus_id",
    "bus_number_going": "bus_number_going",
    "bus_number_return": "bus_number_return",
}

_LOCATION_FIELD_TYPES = {
    "home_address": "HOME",
    "start_point_address": "STARTING_POINT",
    "boarding_address": "BOARDING",
    "after_work_bus_pickup_address": "RETURN_STARTING_POINT",
}


def _current_value(db: Session, submission: Submission, field: str) -> str:
    """What the submission says now, recorded with the request so a supervisor
    can compare it with the requested value."""
    if field in _SUBMISSION_FIELD_ATTRS:
        return str(getattr(submission, _SUBMISSION_FIELD_ATTRS[field]) or "")

    location_type = _LOCATION_FIELD_TYPES.get(field)

    if location_type:
        location = (
            db.query(Location)
            .filter(
                Location.submission_id == submission.id,
                Location.location_type == location_type,
            )
            .first()
        )
        return location.address_text if location else ""

    return ""


def _pack_comment(field: str, current: str, requested: str, reason: str) -> str:
    # One free-text column on the backend; the worker app and the supervisor
    # page both unpack this fixed layout (Current value is optional there).
    return (
        f"Field: {field}\n"
        f"Current value: {current}\n"
        f"Requested value: {requested}\n"
        f"Reason: {reason}"
    )


@public_router.post(
    "/by-number",
    response_model=WorkerChangeRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_change_request_by_number(
    data: PublicChangeRequestCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    submission = _verified_submission(db, request, data)

    change_request = change_request_service.create_change_request(
        db,
        submission,
        data.submission_number,
        _pack_comment(
            data.field,
            _current_value(db, submission, data.field).replace("\n", " "),
            data.requested_value,
            data.reason,
        ),
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an open change request for this submission.",
        )

    db.refresh(change_request)

    return change_request


@public_router.post(
    "/by-number/status",
    response_model=PublicStatusResponse,
)
def get_change_request_status_by_number(
    data: PublicLookup,
    request: Request,
    db: Session = Depends(get_db),
):
    """The worker's requests (newest first) and whether an approved edit is
    open right now."""
    submission = _verified_submission(db, request, data)

    # Approvals whose edit window has passed become EXPIRED before we report.
    if change_request_service.expire_stale_requests(db, submission.id):
        db.commit()

    requests = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.submission_id == submission.id)
        .order_by(ChangeRequest.created_at.desc())
        .all()
    )
    grant = change_request_service.get_active_grant(db, submission.id)

    return PublicStatusResponse(
        submission_number=submission.submission_number,
        requests=requests,
        can_edit=grant is not None,
        edit_expires_at=grant.edit_expires_at if grant else None,
    )


@public_router.post(
    "/by-number/edit-session",
    response_model=PublicEditSession,
)
def open_edit_session_by_number(
    data: PublicLookup,
    request: Request,
    db: Session = Depends(get_db),
):
    """Worker token for one submission, but only while a supervisor-approved
    edit is open. The normal worker routes then let the form be edited, and
    resubmitting consumes the approval."""
    submission = _verified_submission(db, request, data)

    if change_request_service.get_active_grant(db, submission.id) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No approved edit is open for this submission.",
        )

    return PublicEditSession(
        public_id=submission.public_id,
        worker_token=create_worker_token(submission.public_id),
    )