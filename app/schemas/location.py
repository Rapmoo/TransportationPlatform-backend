from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LocationCreate(BaseModel):
    location_type: str = Field(..., min_length=1, max_length=50)
    address_text: str = Field(..., min_length=1, max_length=500)
    longitude: float = Field(..., ge=-180, le=180)
    latitude: float = Field(..., ge=-90, le=90)


class LocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    submission_id: int
    location_type: str
    address_text: str
    longitude: float
    latitude: float
    created_at: datetime


class WorkerLocationResponse(BaseModel):
    """Worker view of a location: no internal submission id."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    location_type: str
    address_text: str
    longitude: float
    latitude: float
    created_at: datetime