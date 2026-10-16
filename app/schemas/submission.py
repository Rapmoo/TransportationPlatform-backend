import uuid
from datetime import datetime, timezone

import re

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.core.identifiers import normalize_employee_id

_PUBLIC_BUS_ID_RE = re.compile(r"^[0-9]{16}$")


def _reject_nul(value: str | None) -> str | None:
    if value is not None and "\x00" in value:
        raise ValueError("Text fields cannot contain NUL characters.")
    return value


def _clean_public_bus_id(value: str | None) -> str | None:
    if value is None:
        return None

    value = value.strip()

    if not _PUBLIC_BUS_ID_RE.match(value):
        raise ValueError("public_bus_id must be exactly 16 digits.")

    return value


def _clean_bus_number(value: str | None) -> str | None:
    if value is None:
        return None

    value = " ".join(value.split())

    if not value:
        raise ValueError("Bus number cannot be blank.")

    return value

def _serialize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class BusFieldsMixin:
    """Shared validators for the three bus fields."""

    @field_validator("public_bus_id", check_fields=False)
    @classmethod
    def validate_public_bus_id(cls, value):
        return _clean_public_bus_id(value)

    @field_validator("bus_number_going", "bus_number_return", check_fields=False)
    @classmethod
    def validate_bus_number(cls, value):
        return _clean_bus_number(value)

    @field_validator(
        "employee_name",
        "organization_name",
        "phone_number",
        "public_bus_id",
        "bus_number_going",
        "bus_number_return",
        check_fields=False,
    )
    @classmethod
    def validate_no_nul(cls, value):
        return _reject_nul(value)


class SubmissionCreate(BusFieldsMixin, BaseModel):
    employee_id: str = Field(..., min_length=1, max_length=50)
    employee_name: str = Field(..., min_length=1, max_length=255)
    organization_name: str = Field(..., min_length=1, max_length=255)
    phone_number: str | None = Field(None, max_length=50)
    public_bus_id: str = Field(..., min_length=1, max_length=16)
    bus_number_going: str = Field(..., min_length=1, max_length=50)
    bus_number_return: str = Field(..., min_length=1, max_length=50)

    @field_validator("employee_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        value = normalize_employee_id(value)

        if not value:
            raise ValueError("employee_id cannot be blank.")

        return value


class SubmissionUpdate(BusFieldsMixin, BaseModel):
    employee_name: str = Field(..., min_length=1, max_length=255)
    organization_name: str = Field(..., min_length=1, max_length=255)
    phone_number: str | None = Field(None, max_length=50)
    public_bus_id: str = Field(..., min_length=1, max_length=16)
    bus_number_going: str = Field(..., min_length=1, max_length=50)
    bus_number_return: str = Field(..., min_length=1, max_length=50)


class AdminLocationUpdate(BaseModel):
    location_type: str = Field(..., min_length=1, max_length=50)
    address_text: str = Field(..., min_length=1, max_length=500)
    latitude: float
    longitude: float

    @field_validator("location_type", "address_text")
    @classmethod
    def validate_no_nul(cls, value: str) -> str:
        return _reject_nul(value)


class AdminSubmissionUpdate(BusFieldsMixin, BaseModel):
    employee_name: str = Field(..., min_length=1, max_length=255)
    organization_name: str = Field(..., min_length=1, max_length=255)
    phone_number: str | None = Field(None, max_length=50)
    # Optional: a field the admin form does not send is left unchanged.
    public_bus_id: str | None = Field(None, max_length=16)
    bus_number_going: str | None = Field(None, max_length=50)
    bus_number_return: str | None = Field(None, max_length=50)
    locations: list[AdminLocationUpdate] = Field(default_factory=list)



class SubmissionResponse(BaseModel):
    """Staff view (supervisor / admin). Includes the internal integer id."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    public_id: uuid.UUID
    employee_id: str
    submission_number: str | None = None
    employee_name: str
    organization_name: str
    phone_number: str | None
    public_bus_id: str | None = None
    bus_number_going: str | None = None
    bus_number_return: str | None = None
    status: str
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    @field_serializer("submitted_at", "created_at", "updated_at")
    def serialize_datetime(self, value: datetime | None) -> datetime | None:
        return _serialize_utc(value)


class WorkerSubmissionResponse(BaseModel):
    """Worker view. Never exposes the internal integer id."""
    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    employee_id: str
    submission_number: str | None = None
    employee_name: str
    organization_name: str
    phone_number: str | None
    public_bus_id: str | None = None
    bus_number_going: str | None = None
    bus_number_return: str | None = None
    status: str
    submitted_at: datetime | None
    created_at: datetime
    updated_at: datetime

class WorkerSubmissionCreated(WorkerSubmissionResponse):
    worker_token: str
    token_type: str = "bearer"

    @field_serializer("submitted_at", "created_at", "updated_at")
    def serialize_datetime(self, value: datetime | None) -> datetime | None:
        return _serialize_utc(value)