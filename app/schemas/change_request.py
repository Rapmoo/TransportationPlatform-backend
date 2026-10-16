import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChangeRequestCreate(BaseModel):
    submission_number: str = Field(..., pattern=r"^[1-9][0-9]{8}$")
    worker_comment: str = Field(..., min_length=1, max_length=2000)

    @field_validator("worker_comment")
    @classmethod
    def strip_comment(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("worker_comment cannot be blank.")

        return value


class WorkerChangeRequestResponse(BaseModel):
    """Worker view of a change request. Never exposes internal integer ids."""
    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    status: str
    worker_comment: str
    review_comment: str | None
    created_at: datetime
    reviewed_at: datetime | None
    edit_expires_at: datetime | None
    completed_at: datetime | None


class ChangeRequestReview(BaseModel):
    review_comment: str | None = Field(None, max_length=2000)

    @field_validator("review_comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.strip() or None


class StaffChangeRequestResponse(BaseModel):
    """Supervisor view of a change request."""
    public_id: uuid.UUID
    status: str
    employee_name: str
    employee_id: str
    submission_number: str | None
    worker_comment: str
    created_at: datetime
    reviewed_at: datetime | None
    review_comment: str | None
    edit_expires_at: datetime | None
    completed_at: datetime | None


# Fields a worker can ask to change from the landing page.
PUBLIC_REQUEST_FIELDS = (
    "fayda_id",
    "phone_number",
    "organization",
    "home_address",
    "start_point_address",
    "boarding_address",
    "after_work_bus_pickup_address",
    "public_bus_id",
    "bus_number_going",
    "bus_number_return",
    "other",
)


class PublicLookup(BaseModel):
    """Identifies a submission for requests made without a session: the
    9-digit submission number. `verification` (phone number or Fayda ID) is
    optional; when a client sends it, it must match."""
    submission_number: str = Field(..., pattern=r"^[1-9][0-9]{8}$")
    verification: str | None = Field(None, max_length=50)

    @field_validator("verification")
    @classmethod
    def strip_verification(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return value.strip() or None


class PublicChangeRequestCreate(PublicLookup):
    field: str
    requested_value: str = Field(..., min_length=1, max_length=1000)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("field")
    @classmethod
    def known_field(cls, value: str) -> str:
        if value not in PUBLIC_REQUEST_FIELDS:
            raise ValueError("Unknown field.")

        return value

    @field_validator("requested_value", "reason")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("This field cannot be blank.")

        return value


class PublicStatusResponse(BaseModel):
    submission_number: str
    requests: list[WorkerChangeRequestResponse]
    can_edit: bool
    edit_expires_at: datetime | None = None


class PublicEditSession(BaseModel):
    """Lets the worker app open the submission for the approved edit."""
    public_id: uuid.UUID
    worker_token: str
    token_type: str = "bearer"