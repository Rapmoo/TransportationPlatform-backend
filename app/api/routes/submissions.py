import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db,
    get_editable_worker_submission,
    get_worker_submission,
    require_supervisor_or_admin,
)
from app.core.config import settings
from app.core.rate_limit import ip_rate_limit
from app.core.security import create_worker_token
from app.models.location import Location
from app.models.submission import Submission
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionResponse,
    SubmissionUpdate,
    WorkerSubmissionCreated,
    WorkerSubmissionResponse,
)
from app.services.audit import create_audit_log
from app.services.change_requests import complete_edit
from app.services.duplicate_notifications import notify_if_duplicate  # NEW
from app.services.submission_number import assign_submission_number


logger = logging.getLogger(__name__)  # NEW

router = APIRouter(
    prefix="/submissions",
    tags=["Submissions"],
)

# Worker URLs use the non-guessable UUID `public_id` ({public_id:uuid}).
# Staff URLs keep the internal integer id ({submission_id:int}). The two path
# converters never match the same value, so the routes cannot collide.


@router.post(
    "/",
    response_model=WorkerSubmissionCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            ip_rate_limit(
                "submission-create",
                settings.SUBMISSION_CREATE_LIMIT,
                settings.SUBMISSION_CREATE_WINDOW_SECONDS,
            )
        )
    ],
)
def create_submission(
    submission_data: SubmissionCreate,
    db: Session = Depends(get_db),
):
    submission = Submission(
        employee_id=submission_data.employee_id,
        employee_name=submission_data.employee_name,
        organization_name=submission_data.organization_name,
        phone_number=submission_data.phone_number,
        public_bus_id=submission_data.public_bus_id,
        bus_number_going=submission_data.bus_number_going,
        bus_number_return=submission_data.bus_number_return,
    )

    db.add(submission)
    db.commit()
    db.refresh(submission)

    response = WorkerSubmissionResponse.model_validate(submission)
    return WorkerSubmissionCreated(
        **response.model_dump(),
        worker_token=create_worker_token(submission.public_id),
    )


@router.get(
    "/{public_id:uuid}",
    response_model=WorkerSubmissionResponse,
)
def get_my_submission(
    submission: Submission = Depends(get_worker_submission),
):
    return submission


@router.put(
    "/{public_id:uuid}",
    response_model=WorkerSubmissionResponse,
)
def update_submission(
    submission_data: SubmissionUpdate,
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):
    submission.employee_name = submission_data.employee_name
    submission.organization_name = submission_data.organization_name
    submission.phone_number = submission_data.phone_number
    submission.public_bus_id = submission_data.public_bus_id
    submission.bus_number_going = submission_data.bus_number_going
    submission.bus_number_return = submission_data.bus_number_return

    db.commit()
    db.refresh(submission)

    return submission


@router.get(
    "/{submission_id:int}",
    response_model=SubmissionResponse,
)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_supervisor_or_admin),
):
    submission = (
        db.query(Submission)
        .filter(Submission.id == submission_id)
        .first()
    )

    if not submission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Submission not found.",
        )

    return submission


@router.post(
    "/{public_id:uuid}/submit",
    response_model=WorkerSubmissionResponse,
)
def submit_submission(
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):

    db.refresh(submission, with_for_update=True)


    is_correction = submission.status == "SUBMITTED"

    if submission.status not in {"DRAFT", "SUBMITTED"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This submission has already been submitted.",
        )

    # Drafts created before the bus columns existed have none of them.
    missing_bus_fields = [
        name
        for name in ("public_bus_id", "bus_number_going", "bus_number_return")
        if not getattr(submission, name)
    ]

    if missing_bus_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Cannot submit. Bus details are missing.",
                "missing_fields": missing_bus_fields,
            },
        )

    required_location_types = {
        "HOME",
        "STARTING_POINT",
        "BOARDING",
        "RETURN_STARTING_POINT",
    }

    existing_location_types = {
        location.location_type.upper()
        for location in (
            db.query(Location)
            .filter(Location.submission_id == submission.id)
            .all()
        )
    }

    missing_location_types = required_location_types - existing_location_types

    if missing_location_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Cannot submit. Required locations are missing.",
                "missing_location_types": sorted(missing_location_types),
            },
        )

    if is_correction:
        # Consumes the approval (APPROVED -> COMPLETED); the form is locked
        # again and keeps its original submission number.
        complete_edit(db, submission)

        create_audit_log(
            db=db,
            action="WORKER_RESUBMIT",
            submission_id=submission.id,
            old_status="SUBMITTED",
            new_status="SUBMITTED",
            details="Worker resubmitted the corrected form.",
        )
    else:
        # Assign the number first: it flushes inside a savepoint, which must
        # happen before other attributes are changed on this object.
        assign_submission_number(db, submission)

        submission.status = "SUBMITTED"
        submission.submitted_at = datetime.utcnow()

        create_audit_log(
            db=db,
            action="WORKER_SUBMIT",
            submission_id=submission.id,
            old_status="DRAFT",
            new_status="SUBMITTED",
            details="Worker submitted the transportation form.",
        )

        # NEW: Duplicate employee IDs are a warning for the supervisor, never
        # an error. A failure here must not block the worker's submission.
        try:
            with db.begin_nested():
                notify_if_duplicate(db, submission)
        except Exception:
            logger.exception(
                "duplicate_employee_check_failed submission_id=%s",
                submission.id,
            )

    db.commit()
    db.refresh(submission)

    return submission