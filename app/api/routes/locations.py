from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_db,
    get_editable_worker_submission,
    get_worker_submission,
)
from app.models.location import Location
from app.models.submission import Submission
from app.schemas.location import LocationCreate, WorkerLocationResponse


router = APIRouter(
    prefix="/submissions",
    tags=["Locations"],
)

# Every route below requires the worker token issued for this exact
# submission (see get_worker_submission). Write routes additionally reject
# submissions that are already SUBMITTED.


def _location_dict(location: Location, longitude: float, latitude: float) -> dict:
    return {
        "id": location.id,
        "location_type": location.location_type,
        "address_text": location.address_text,
        "longitude": float(longitude),
        "latitude": float(latitude),
        "created_at": location.created_at,
    }


@router.post(
    "/{public_id:uuid}/locations",
    response_model=WorkerLocationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_location(
    location_data: LocationCreate,
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):
    # Prevent duplicate location types
    existing_location = (
        db.query(Location)
        .filter(
            Location.submission_id == submission.id,
            Location.location_type == location_data.location_type.upper(),
        )
        .first()
    )

    if existing_location:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A {location_data.location_type.upper()} "
                "location already exists for this submission."
            ),
        )

    location = Location(
        submission_id=submission.id,
        location_type=location_data.location_type,
        address_text=location_data.address_text,
        geom=func.ST_SetSRID(
            func.ST_MakePoint(
                location_data.longitude,
                location_data.latitude,
            ),
            4326,
        ),
    )

    db.add(location)
    db.commit()
    db.refresh(location)

    return _location_dict(location, location_data.longitude, location_data.latitude)


@router.get(
    "/{public_id:uuid}/locations",
    response_model=list[WorkerLocationResponse],
)
def get_submission_locations(
    submission: Submission = Depends(get_worker_submission),
    db: Session = Depends(get_db),
):
    locations = (
        db.query(
            Location,
            func.ST_X(Location.geom).label("longitude"),
            func.ST_Y(Location.geom).label("latitude"),
        )
        .filter(Location.submission_id == submission.id)
        .all()
    )

    return [
        _location_dict(location, longitude, latitude)
        for location, longitude, latitude in locations
    ]


@router.put(
    "/{public_id:uuid}/locations/{location_id}",
    response_model=WorkerLocationResponse,
)
def update_location(
    location_id: int,
    location_data: LocationCreate,
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):
    location = (
        db.query(Location)
        .filter(
            Location.id == location_id,
            Location.submission_id == submission.id,
        )
        .first()
    )

    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found for this submission.",
        )

    location.location_type = location_data.location_type
    location.address_text = location_data.address_text
    location.geom = func.ST_SetSRID(
        func.ST_MakePoint(
            location_data.longitude,
            location_data.latitude,
        ),
        4326,
    )

    db.commit()
    db.refresh(location)

    return _location_dict(location, location_data.longitude, location_data.latitude)


@router.delete(
    "/{public_id:uuid}/locations/{location_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_location(
    location_id: int,
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):
    location = (
        db.query(Location)
        .filter(
            Location.id == location_id,
            Location.submission_id == submission.id,
        )
        .first()
    )

    if not location:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Location not found for this submission.",
        )

    db.delete(location)
    db.commit()

    return None