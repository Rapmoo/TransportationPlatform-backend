import csv
import io
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import extract, func, or_, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.dependencies import (
    get_db,
    require_admin,
    require_supervisor,
    require_supervisor_or_admin,
)
from app.core.security import hash_password, verify_password
from app.database.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.duplicate_employee_notification import (
    DuplicateEmployeeNotification,
    DuplicateNotificationStatus,
)
from app.models.location import Location
from app.models.submission import Submission
from app.models.user import User
from app.schemas.auth import CredentialsUpdate, UserResponse
from app.schemas.duplicate_notification import DuplicateNotificationList
from app.schemas.submission import AdminSubmissionUpdate
from app.services.audit import create_audit_log

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover - optional dependency
    Workbook = None


router = APIRouter(prefix="/admin", tags=["Admin"])

EXPORT_COLUMNS = [
    "id",
    "employee_id",
    "submission_number",
    "employee_name",
    "organization_name",
    "phone_number",
    "public_bus_id",
    "bus_number_going",
    "bus_number_return",
    "status",
    "submitted_at",
    "created_at",
    "updated_at",
]

LOCATION_TYPES = (
    "HOME",
    "STARTING_POINT",
    "BOARDING",
    "RETURN_STARTING_POINT",
)

LOCATION_EXPORT_PREFIXES = {
    "HOME": "home",
    "STARTING_POINT": "start_point",
    "BOARDING": "boarding",
    "RETURN_STARTING_POINT": "after_work_pickup",
}

MAX_EXPORT_IDS = 1000
EXPORT_BATCH_SIZE = 1000
GEOJSON_EXPORT_BATCH_SIZE = 5000

MAP_DEFAULT_LIMIT = 20_000
MAP_MAX_LIMIT = 100_000


def _load_locations(
    db: Session,
    submission_ids: list[int],
    include_coordinates: bool,
) -> dict[int, list[dict]]:
    """Group locations by submission ID.

    Coordinates are returned only to administrators.
    """
    grouped: dict[int, list[dict]] = {}

    if not submission_ids:
        return grouped

    rows = (
        db.query(
            Location.submission_id,
            Location.location_type,
            Location.address_text,
            func.ST_X(Location.geom).label("longitude"),
            func.ST_Y(Location.geom).label("latitude"),
        )
        .filter(Location.submission_id.in_(submission_ids))
        .all()
    )

    for row in rows:
        entry = {
            "location_type": row.location_type,
            "address_text": row.address_text,
        }

        if include_coordinates:
            entry["longitude"] = (
                float(row.longitude) if row.longitude is not None else None
            )
            entry["latitude"] = (
                float(row.latitude) if row.latitude is not None else None
            )

        grouped.setdefault(row.submission_id, []).append(entry)

    return grouped


def _submission_item(
    submission: Submission,
    locations: list[dict],
) -> dict:
    return {
        "id": submission.id,
        "employee_id": submission.employee_id,
        "submission_number": submission.submission_number,
        "employee_name": submission.employee_name,
        "organization_name": submission.organization_name,
        "phone_number": submission.phone_number,
        "public_bus_id": submission.public_bus_id,
        "bus_number_going": submission.bus_number_going,
        "bus_number_return": submission.bus_number_return,
        "locations": locations,
        "status": submission.status,
        "submitted_at": submission.submitted_at,
        "created_at": submission.created_at,
        "updated_at": submission.updated_at,
    }


def _parse_ids(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None

    try:
        ids = sorted(
            {int(part) for part in raw.split(",") if part.strip()}
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ids must be a comma-separated list of submission ids.",
        )

    if any(item < 1 for item in ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Submission IDs must be positive integers.",
        )

    if len(ids) > MAX_EXPORT_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"You can export at most {MAX_EXPORT_IDS} "
                "selected submissions at once."
            ),
        )

    return ids


@router.put("/submissions/{submission_id}")
def edit_submission(
    data: AdminSubmissionUpdate,
    submission_id: int = Path(..., ge=1, le=2147483647),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Administrator correction of a submission.

    Employee ID and submission number are never editable.
    """
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

    employee_name = data.employee_name.strip()
    organization_name = data.organization_name.strip()
    phone_number = (data.phone_number or "").strip() or None

    if not employee_name or not organization_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Employee name and organization cannot be blank.",
        )

    changes: list[str] = []

    edits = [
        ("employee_name", employee_name),
        ("organization_name", organization_name),
        ("phone_number", phone_number),
    ]

    for bus_field in (
        "public_bus_id",
        "bus_number_going",
        "bus_number_return",
    ):
        if bus_field in data.model_fields_set:
            new_bus_value = getattr(data, bus_field)

            if not new_bus_value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{bus_field} cannot be blank.",
                )

            edits.append((bus_field, new_bus_value))

    for field, new_value in edits:
        if getattr(submission, field) != new_value:
            setattr(submission, field, new_value)
            changes.append(field)

    existing_locations = {
        location.location_type.upper(): location
        for location in (
            db.query(Location)
            .filter(Location.submission_id == submission_id)
            .all()
        )
    }

    seen_types: set[str] = set()

    for item in data.locations:
        location_type = item.location_type.upper()

        if location_type not in LOCATION_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Invalid location type.",
                    "allowed_location_types": list(LOCATION_TYPES),
                },
            )

        if location_type in seen_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{location_type} was sent more than once.",
            )

        seen_types.add(location_type)
        location = existing_locations.get(location_type)

        if not location:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"This submission has no {location_type} "
                    "location to edit."
                ),
            )

        address_text = item.address_text.strip()

        if not address_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"The {location_type} location name cannot be blank."
                ),
            )

        current_x, current_y = (
            db.query(
                func.ST_X(Location.geom),
                func.ST_Y(Location.geom),
            )
            .filter(Location.id == location.id)
            .one()
        )

        moved = (
            abs(float(current_x) - item.longitude) > 1e-9
            or abs(float(current_y) - item.latitude) > 1e-9
        )

        if location.address_text != address_text or moved:
            location.address_text = address_text

            if moved:
                location.geom = func.ST_SetSRID(
                    func.ST_MakePoint(
                        item.longitude,
                        item.latitude,
                    ),
                    4326,
                )

            changes.append(f"{location_type} location")

    if changes:
        create_audit_log(
            db=db,
            action="ADMIN_EDIT",
            actor=current_user,
            submission_id=submission.id,
            details="Edited: " + ", ".join(changes) + ".",
        )
        db.commit()
        db.refresh(submission)

    locations = _load_locations(
        db,
        [submission.id],
        include_coordinates=True,
    )

    return _submission_item(
        submission,
        locations.get(submission.id, []),
    )


@router.put("/submissions/{submission_id}/status")
def update_submission_status(
    submission_id: int,
    new_status: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
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

    allowed_statuses = {
        "DRAFT",
        "SUBMITTED",
        "REVIEWED",
        "APPROVED",
        "REJECTED",
    }

    new_status = new_status.upper()

    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Invalid submission status.",
                "allowed_statuses": sorted(allowed_statuses),
            },
        )

    old_status = submission.status

    if old_status != new_status:
        submission.status = new_status

        create_audit_log(
            db=db,
            action="STAFF_STATUS_CHANGE",
            actor=current_user,
            submission_id=submission.id,
            old_status=old_status,
            new_status=new_status,
            details=f"Submission status changed by {current_user.role}.",
        )

        db.commit()
        db.refresh(submission)

    return {
        "message": "Submission status updated successfully.",
        "submission_id": submission.id,
        "old_status": old_status,
        "new_status": submission.status,
    }


# Ethiopia (Africa/Addis_Ababa) uses UTC+3 year-round.
LOCAL_UTC_OFFSET = timedelta(hours=3)
OVERVIEW_WEEKS = 6
OVERVIEW_CACHE_SECONDS = 30

_overview_cache: tuple[float, dict] | None = None
_overview_cache_lock = threading.Lock()


@router.get("/statistics/overview")
def get_overview_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    global _overview_cache

    with _overview_cache_lock:
        cached = _overview_cache

    if (
        cached is not None
        and time.monotonic() - cached[0] < OVERVIEW_CACHE_SECONDS
    ):
        return cached[1]

    result = _build_overview_statistics(db)

    with _overview_cache_lock:
        _overview_cache = (time.monotonic(), result)

    return result


def _build_overview_statistics(db: Session) -> dict:
    """Build dashboard statistics using database-side aggregations."""
    counted = Submission.status != "DRAFT"
    submitted_time = func.coalesce(
        Submission.submitted_at,
        Submission.created_at,
    )

    total_submissions = (
        db.query(func.count(Submission.id))
        .filter(counted)
        .scalar()
        or 0
    )

    now_local = datetime.utcnow() + LOCAL_UTC_OFFSET
    today_start_local = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_start_utc = today_start_local - LOCAL_UTC_OFFSET

    todays_submissions = (
        db.query(func.count(Submission.id))
        .filter(
            counted,
            submitted_time >= today_start_utc,
        )
        .scalar()
        or 0
    )

    org_rows = (
        db.query(
            Submission.organization_name,
            func.count(Submission.id).label("submission_count"),
        )
        .filter(counted)
        .group_by(Submission.organization_name)
        .order_by(func.count(Submission.id).desc())
        .all()
    )

    this_week_start = today_start_local - timedelta(
        days=today_start_local.weekday()
    )
    first_week_start = this_week_start - timedelta(
        weeks=OVERVIEW_WEEKS - 1
    )
    buckets = [
        first_week_start + timedelta(weeks=i)
        for i in range(OVERVIEW_WEEKS)
    ]
    counts = [0] * OVERVIEW_WEEKS

    first_week_start_utc = first_week_start - LOCAL_UTC_OFFSET
    week_index = func.floor(
        extract(
            "epoch",
            submitted_time - first_week_start_utc,
        ) / (7 * 24 * 3600)
    )

    weekly_rows = (
        db.query(
            week_index.label("week"),
            func.count(Submission.id),
        )
        .filter(
            counted,
            submitted_time >= first_week_start_utc,
        )
        .group_by(week_index)
        .all()
    )

    for week, count in weekly_rows:
        index = int(week)
        if 0 <= index < OVERVIEW_WEEKS:
            counts[index] += count

    locations_submitted = (
        db.query(func.count(Location.id))
        .join(Submission, Submission.id == Location.submission_id)
        .filter(counted)
        .scalar()
        or 0
    )

    locations_draft = (
        db.query(func.count(Location.id))
        .join(Submission, Submission.id == Location.submission_id)
        .filter(Submission.status == "DRAFT")
        .scalar()
        or 0
    )

    return {
        "total_submissions": total_submissions,
        "todays_submissions": todays_submissions,
        "organizations": len(org_rows),
        "geocoded_locations": locations_submitted,
        "submissions_by_week": [
            {
                "week": f"{start:%b} {start.day}",
                "submissions": count,
            }
            for start, count in zip(buckets, counts)
        ],
        "geocoding": {
            "success": locations_submitted,
            "pending": locations_draft,
            "failed": 0,
        },
        "organization_breakdown": [
            {
                "organization": row.organization_name,
                "count": row.submission_count,
            }
            for row in org_rows
        ],
    }


@router.get("/statistics/organizations")
def get_organization_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    results = (
        db.query(
            Submission.organization_name,
            func.count(Submission.id).label("submission_count"),
        )
        .group_by(Submission.organization_name)
        .order_by(func.count(Submission.id).desc())
        .all()
    )

    total_submissions = sum(
        row.submission_count for row in results
    )

    statistics = []

    for row in results:
        percentage = (
            (row.submission_count / total_submissions) * 100
            if total_submissions > 0
            else 0
        )

        statistics.append(
            {
                "organization_name": row.organization_name,
                "submission_count": row.submission_count,
                "percentage": round(percentage, 2),
            }
        )

    return {
        "total_submissions": total_submissions,
        "organizations": statistics,
    }


@router.get("/submissions")
def get_admin_submissions(
    organization_name: str | None = None,
    submission_status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    if page < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page must be greater than or equal to 1.",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page size must be between 1 and 100.",
        )

    query = db.query(Submission)

    if organization_name:
        query = query.filter(
            Submission.organization_name == organization_name
        )

    if submission_status:
        query = query.filter(
            Submission.status == submission_status.upper()
        )

    if search and search.strip():
        escaped = (
            search.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"

        matching_submission_ids = union(
            select(Submission.id).where(
                or_(
                    Submission.employee_id.ilike(pattern, escape="\\"),
                    Submission.submission_number.ilike(pattern, escape="\\"),
                    Submission.employee_name.ilike(pattern, escape="\\"),
                    Submission.organization_name.ilike(pattern, escape="\\"),
                    Submission.phone_number.ilike(pattern, escape="\\"),
                    Submission.public_bus_id.ilike(pattern, escape="\\"),
                    Submission.bus_number_going.ilike(pattern, escape="\\"),
                    Submission.bus_number_return.ilike(pattern, escape="\\"),
                )
            ),
            select(Location.submission_id).where(
                Location.address_text.ilike(pattern, escape="\\")
            ),
        )

        query = query.filter(
            Submission.id.in_(matching_submission_ids)
        )

    total = query.count()
    total_pages = ceil(total / page_size) if total > 0 else 0
    offset = (page - 1) * page_size

    submissions = (
        query.order_by(Submission.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    is_admin = current_user.role == "ADMIN"

    locations_by_submission = _load_locations(
        db,
        [submission.id for submission in submissions],
        include_coordinates=is_admin,
    )

    return {
        "items": [
            _submission_item(
                submission,
                locations_by_submission.get(submission.id, []),
            )
            for submission in submissions
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/statistics/progress")
def get_progress_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    total_submissions = (
        db.query(func.count(Submission.id)).scalar()
    )

    submitted = (
        db.query(func.count(Submission.id))
        .filter(Submission.status == "SUBMITTED")
        .scalar()
    )

    reviewed = (
        db.query(func.count(Submission.id))
        .filter(Submission.status == "REVIEWED")
        .scalar()
    )

    approved = (
        db.query(func.count(Submission.id))
        .filter(Submission.status == "APPROVED")
        .scalar()
    )

    rejected = (
        db.query(func.count(Submission.id))
        .filter(Submission.status == "REJECTED")
        .scalar()
    )

    draft = (
        db.query(func.count(Submission.id))
        .filter(Submission.status == "DRAFT")
        .scalar()
    )

    return {
        "total_submissions": total_submissions,
        "status_counts": {
            "DRAFT": draft,
            "SUBMITTED": submitted,
            "REVIEWED": reviewed,
            "APPROVED": approved,
            "REJECTED": rejected,
        },
    }


@router.get("/locations/geojson")
def get_locations_geojson(
    organization_name: str | None = None,
    location_type: str | None = None,
    min_lon: float | None = Query(None, ge=-180, le=180),
    min_lat: float | None = Query(None, ge=-90, le=90),
    max_lon: float | None = Query(None, ge=-180, le=180),
    max_lat: float | None = Query(None, ge=-90, le=90),
    limit: int = Query(MAP_DEFAULT_LIMIT, ge=1, le=MAP_MAX_LIMIT),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    query = (
        db.query(
            Location.id,
            Location.submission_id,
            Location.location_type,
            Location.address_text,
            Submission.employee_id,
            Submission.submission_number,
            Submission.employee_name,
            Submission.organization_name,
            Submission.status,
            func.ST_X(Location.geom).label("longitude"),
            func.ST_Y(Location.geom).label("latitude"),
        )
        .join(
            Submission,
            Submission.id == Location.submission_id,
        )
    )

    if organization_name:
        query = query.filter(
            Submission.organization_name == organization_name
        )

    if location_type:
        query = query.filter(
            Location.location_type == location_type.upper()
        )

    bbox = (min_lon, min_lat, max_lon, max_lat)

    if all(value is not None for value in bbox):
        if min_lon > max_lon or min_lat > max_lat:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid map bounding box.",
            )

        query = query.filter(
            func.ST_Intersects(
                Location.geom,
                func.ST_MakeEnvelope(*bbox, 4326),
            )
        )
    elif any(value is not None for value in bbox):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Send all of min_lon, min_lat, max_lon, max_lat, "
                "or none of them."
            ),
        )

    locations = query.order_by(Location.id).limit(limit + 1).all()
    truncated = len(locations) > limit
    locations = locations[:limit]

    features = []

    for location in locations:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        location.longitude,
                        location.latitude,
                    ],
                },
                "properties": {
                    "location_id": location.id,
                    "submission_id": location.submission_id,
                    "location_type": location.location_type,
                    "address_text": location.address_text,
                    "employee_id": location.employee_id,
                    "submission_number": location.submission_number,
                    "employee_name": location.employee_name,
                    "organization_name": location.organization_name,
                    "status": location.status,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "truncated": truncated,
        "limit": limit,
        "features_returned": len(features),
    }


@router.get("/users")
def get_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    users = (
        db.query(User)
        .filter(User.role.in_(["ADMIN", "SUPERVISOR"]))
        .order_by(User.created_at.desc())
        .all()
    )

    return {
        "users": [
            {
                "id": staff_user.id,
                "name": staff_user.name,
                "username": staff_user.username,
                "role": staff_user.role,
                "is_active": staff_user.is_active,
                "created_at": staff_user.created_at,
                "updated_at": staff_user.updated_at,
            }
            for staff_user in users
        ]
    }


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    is_active: bool,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    staff_user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not staff_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    if staff_user.role not in {"ADMIN", "SUPERVISOR"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only staff accounts can be managed.",
        )

    if staff_user.id == current_user.id and not is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )

    old_is_active = staff_user.is_active

    if old_is_active != is_active:
        old_status = "ACTIVE" if old_is_active else "INACTIVE"
        new_status = "ACTIVE" if is_active else "INACTIVE"

        staff_user.is_active = is_active


        create_audit_log(
            db=db,
            action="USER_STATUS_CHANGE",
            actor=current_user,
            old_status=old_status,
            new_status=new_status,
            details=(
                f"Changed account status for user ID {staff_user.id} "
                f"({staff_user.username})."
            ),
        )


        try:
            db.commit()
            db.refresh(staff_user)
        except Exception:
            db.rollback()
            raise

    return {
        "message": "User status updated successfully.",
        "user_id": staff_user.id,
        "username": staff_user.username,
        "is_active": staff_user.is_active,
    }


@router.put(
    "/users/{user_id}/credentials",
    response_model=UserResponse,
)
def update_user_credentials(
    user_id: int,
    data: CredentialsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Change a username and/or password.

    Passwords are hashed and never written to the audit log.
    """
    staff_user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not staff_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    is_self = staff_user.id == current_user.id

    if not is_self and staff_user.role != "SUPERVISOR":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You can only change your own account "
                "or a supervisor's account."
            ),
        )

    new_username = data.username.strip() if data.username else None

    if new_username is not None and len(new_username) < 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters.",
        )

    if not new_username and not data.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Provide a new username, a new password, or both."
            ),
        )

    if is_self and not (
        data.current_password
        and verify_password(
            data.current_password,
            staff_user.password_hash,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your current password is incorrect.",
        )

    old_username = staff_user.username
    changes: list[str] = []

    if new_username and new_username != old_username:
        taken = (
            db.query(User.id)
            .filter(
                User.username == new_username,
                User.id != staff_user.id,
            )
            .first()
        )

        if taken:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this username already exists.",
            )

        staff_user.username = new_username
        changes.append(
            f"username ({old_username} -> {new_username})"
        )

    if data.password:
        staff_user.password_hash = hash_password(data.password)
        changes.append("password")

    if changes:
        create_audit_log(
            db=db,
            action="USER_CREDENTIALS_CHANGE",
            actor=current_user,
            details=(
                f"Changed {' and '.join(changes)} for account "
                f"'{staff_user.username}'."
            ),
        )

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this username already exists.",
            )

        db.refresh(staff_user)

    return staff_user


@router.get("/audit-logs")
def get_audit_logs(
    action: str | None = None,
    actor_role: str | None = None,
    submission_id: int | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    if page < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page must be greater than or equal to 1.",
        )

    if page_size < 1 or page_size > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page size must be between 1 and 200.",
        )

    query = (
        db.query(AuditLog, User.name.label("actor_name"))
        .outerjoin(User, User.id == AuditLog.actor_id)
        # Staff activity only: worker submissions and system events have
        # no actor and are not part of this log.
        .filter(AuditLog.actor_id.isnot(None))
    )

    if action:
        query = query.filter(AuditLog.action == action.upper())

    if actor_role:
        query = query.filter(
            AuditLog.actor_role == actor_role.upper()
        )

    if submission_id is not None:
        query = query.filter(
            AuditLog.submission_id == submission_id
        )

    if search and search.strip():
        escaped = (
            search.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        query = query.filter(
            or_(
                AuditLog.details.ilike(pattern, escape="\\"),
                User.name.ilike(pattern, escape="\\"),
                User.username.ilike(pattern, escape="\\"),
            )
        )

    if start_date:
        query = query.filter(AuditLog.created_at >= start_date)

    if end_date:
        query = query.filter(AuditLog.created_at <= end_date)

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date cannot be later than end_date.",
        )

    total = query.count()
    total_pages = ceil(total / page_size) if total > 0 else 0
    offset = (page - 1) * page_size

    rows = (
        query.order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    return {
        "items": [
            {
                "id": audit_log.id,
                "submission_id": audit_log.submission_id,
                "actor_id": audit_log.actor_id,
                "actor_name": actor_name,
                "actor_role": audit_log.actor_role,
                "action": audit_log.action,
                "old_status": audit_log.old_status,
                "new_status": audit_log.new_status,
                "details": audit_log.details,
                "created_at": audit_log.created_at,
            }
            for audit_log, actor_name in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


# Events only the browser can observe. The server records everything else
# (logins, edits, account changes, server-side exports) itself.
CLIENT_AUDIT_ACTIONS = {
    "LOGOUT",
    "RECORD_VIEW",
    "MAP_VIEW",
    "DATA_EXPORT",
}


class AuditEventCreate(BaseModel):
    action: str = Field(..., min_length=1, max_length=50)
    details: str | None = Field(None, max_length=500)


@router.post(
    "/audit-logs/events",
    status_code=status.HTTP_201_CREATED,
)
def record_audit_event(
    event: AuditEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    """Record an action the server cannot see by itself (logout, viewing
    a record or the map, a client-side export). The actor is always the
    authenticated user, never a value sent by the client."""
    action = event.action.strip().upper()

    if action not in CLIENT_AUDIT_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported audit event.",
        )

    details = (
        " ".join(event.details.split()) if event.details else None
    )

    create_audit_log(
        db=db,
        action=action,
        actor=current_user,
        details=details,
    )
    db.commit()

    return {"message": "Audit event recorded."}


def _export_detail(
    label: str,
    ids: list[int] | None,
) -> str:
    if ids is None:
        return f"Exported submissions data as {label}."

    return f"Exported {len(ids)} selected submissions as {label}."


def _submission_conditions(
    organization_name: str | None,
    submission_status: str | None,
    ids: list[int] | None,
) -> list:
    conditions = []

    if ids is not None:
        conditions.append(Submission.id.in_(ids))

    if organization_name:
        conditions.append(
            Submission.organization_name == organization_name
        )

    if submission_status:
        conditions.append(
            Submission.status == submission_status.upper()
        )

    return conditions


def _export_header(include_coordinates: bool) -> list[str]:
    header = list(EXPORT_COLUMNS)

    for prefix in LOCATION_EXPORT_PREFIXES.values():
        header.append(f"{prefix}_address")

        if include_coordinates:
            header += [
                f"{prefix}_latitude",
                f"{prefix}_longitude",
            ]

    return header


def _export_row(
    submission: Submission,
    locations: dict[int, list[dict]],
    include_coordinates: bool,
) -> list:
    row = [
        getattr(submission, column)
        for column in EXPORT_COLUMNS
    ]

    by_type = {
        entry["location_type"].upper(): entry
        for entry in locations.get(submission.id, [])
    }

    for location_type in LOCATION_EXPORT_PREFIXES:
        entry = by_type.get(location_type)
        row.append(entry["address_text"] if entry else "")

        if include_coordinates:
            row += (
                [entry["latitude"], entry["longitude"]]
                if entry
                else ["", ""]
            )

    return row


def _iter_export_batches(
    organization_name: str | None,
    submission_status: str | None,
    ids: list[int] | None,
    include_coordinates: bool,
) -> Iterator[list[list]]:
    conditions = _submission_conditions(
        organization_name,
        submission_status,
        ids,
    )

    with SessionLocal() as db:
        last_id = None

        while True:
            query = db.query(Submission).filter(*conditions)

            if last_id is not None:
                query = query.filter(Submission.id < last_id)

            batch = (
                query.order_by(Submission.id.desc())
                .limit(EXPORT_BATCH_SIZE)
                .all()
            )

            if not batch:
                return

            last_id = batch[-1].id

            locations = _load_locations(
                db,
                [submission.id for submission in batch],
                include_coordinates,
            )

            yield [
                _export_row(
                    submission,
                    locations,
                    include_coordinates,
                )
                for submission in batch
            ]

            db.expunge_all()


def _csv_chunks(
    organization_name: str | None,
    submission_status: str | None,
    ids: list[int] | None,
    include_coordinates: bool,
) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def flush() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    writer.writerow(_export_header(include_coordinates))
    yield flush()

    for rows in _iter_export_batches(
        organization_name,
        submission_status,
        ids,
        include_coordinates,
    ):
        writer.writerows(rows)
        yield flush()


def _geojson_export_chunks(
    organization_name: str | None,
    submission_status: str | None,
    ids: list[int] | None,
) -> Iterator[str]:
    conditions = _submission_conditions(
        organization_name,
        submission_status,
        ids,
    )

    yield '{"type": "FeatureCollection", "features": ['

    with SessionLocal() as db:
        last_id = None
        first = True

        while True:
            query = (
                db.query(
                    Location.id,
                    Location.submission_id,
                    Location.location_type,
                    Location.address_text,
                    Submission.employee_id,
                    Submission.submission_number,
                    Submission.employee_name,
                    Submission.organization_name,
                    Submission.phone_number,
                    Submission.public_bus_id,
                    Submission.bus_number_going,
                    Submission.bus_number_return,
                    Submission.status,
                    func.ST_X(Location.geom).label("longitude"),
                    func.ST_Y(Location.geom).label("latitude"),
                )
                .join(
                    Submission,
                    Submission.id == Location.submission_id,
                )
                .filter(*conditions)
            )

            if last_id is not None:
                query = query.filter(Location.id > last_id)

            rows = (
                query.order_by(Location.id)
                .limit(GEOJSON_EXPORT_BATCH_SIZE)
                .all()
            )

            if not rows:
                break

            last_id = rows[-1].id

            features = [
                json.dumps(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [
                                row.longitude,
                                row.latitude,
                            ],
                        },
                        "properties": {
                            "location_id": row.id,
                            "submission_id": row.submission_id,
                            "location_type": row.location_type,
                            "address_text": row.address_text,
                            "employee_id": row.employee_id,
                            "submission_number": row.submission_number,
                            "employee_name": row.employee_name,
                            "organization_name": row.organization_name,
                            "phone_number": row.phone_number,
                            "public_bus_id": row.public_bus_id,
                            "bus_number_going": row.bus_number_going,
                            "bus_number_return": row.bus_number_return,
                            "status": row.status,
                        },
                    },
                    default=str,
                )
                for row in rows
            ]

            yield ("" if first else ", ") + ", ".join(features)
            first = False

    yield "]}"


@router.get("/exports/submissions")
def export_submissions(
    format: str = Query(..., pattern="^(csv|excel|geojson)$"),
    organization_name: str | None = None,
    submission_status: str | None = None,
    ids: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor_or_admin),
):
    """Stream exports in batches and record the export in the audit log."""
    export_format = format.lower()
    selected_ids = _parse_ids(ids)
    include_coordinates = current_user.role == "ADMIN"
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if export_format == "excel" and Workbook is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Excel export requires the 'openpyxl' package to be "
                "installed on the server. Run: pip install openpyxl"
            ),
        )

    label = {
        "csv": "CSV",
        "excel": "Excel",
        "geojson": "GeoJSON",
    }[export_format]

    create_audit_log(
        db=db,
        action="DATA_EXPORT",
        actor=current_user,
        details=_export_detail(label, selected_ids),
    )
    db.commit()

    if export_format == "csv":
        return StreamingResponse(
            _csv_chunks(
                organization_name,
                submission_status,
                selected_ids,
                include_coordinates,
            ),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="submissions_{timestamp}.csv"'
                ),
            },
        )

    if export_format == "excel":
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Submissions")
        sheet.append(_export_header(include_coordinates))

        handle = tempfile.NamedTemporaryFile(
            suffix=".xlsx",
            delete=False,
        )
        handle.close()

        try:
            for rows in _iter_export_batches(
                organization_name,
                submission_status,
                selected_ids,
                include_coordinates,
            ):
                for row in rows:
                    sheet.append([
                        str(value) if value is not None else ""
                        for value in row
                    ])

            workbook.save(handle.name)
        except Exception:
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise

        return FileResponse(
            handle.name,
            media_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            filename=f"submissions_{timestamp}.xlsx",
            background=BackgroundTask(os.unlink, handle.name),
        )

    return StreamingResponse(
        _geojson_export_chunks(
            organization_name,
            submission_status,
            selected_ids,
        ),
        media_type="application/geo+json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="submissions_{timestamp}.geojson"'
            ),
        },
    )


# Duplicate employee ID notifications (SUPERVISOR only)

@router.get(
    "/notifications/duplicates",
    response_model=DuplicateNotificationList,
)
def get_duplicate_notifications(
    notification_status: str = "ACTIVE",
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor),
):
    """List duplicate employee ID warnings without modifying them."""
    notification_status = notification_status.upper()

    if notification_status not in {"ACTIVE", "ACKNOWLEDGED", "ALL"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "notification_status must be ACTIVE, "
                "ACKNOWLEDGED or ALL."
            ),
        )

    if page < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page must be greater than or equal to 1.",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Page size must be between 1 and 100.",
        )

    query = db.query(DuplicateEmployeeNotification)

    if notification_status != "ALL":
        query = query.filter(
            DuplicateEmployeeNotification.status == notification_status
        )

    total = query.count()

    rows = (
        query.order_by(
            DuplicateEmployeeNotification.created_at.desc(),
            DuplicateEmployeeNotification.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    members: dict[str, list[Submission]] = {}

    if rows:
        group_rows = (
            db.query(Submission)
            .filter(
                Submission.employee_id.in_(
                    {row.employee_id for row in rows}
                ),
                Submission.status != "DRAFT",
            )
            .order_by(
                func.coalesce(
                    Submission.submitted_at,
                    Submission.created_at,
                ),
                Submission.id,
            )
            .all()
        )

        for member in group_rows:
            members.setdefault(member.employee_id, []).append(member)

    items = []

    for row in rows:
        group = members.get(row.employee_id, [])

        items.append(
            {
                "id": row.id,
                "employee_id": row.employee_id,
                "message": (
                    "Duplicate Employee ID detected. Employee ID "
                    f"{row.employee_id} is associated with multiple "
                    "submissions."
                ),
                "status": row.status,
                "submission_count": len(group),
                "submissions": [
                    {
                        "submission_number": member.submission_number,
                        "employee_name": member.employee_name,
                        "organization_name": member.organization_name,
                        "submitted_at": member.submitted_at,
                    }
                    for member in group
                ],
                "detected_at": row.created_at,
                "acknowledged_at": row.acknowledged_at,
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": ceil(total / page_size) if total > 0 else 0,
    }


@router.post(
    "/notifications/duplicates/{notification_id}/acknowledge",
)
def acknowledge_duplicate_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor),
):
    """Acknowledge a duplicate warning and preserve its audit trail."""
    notification = (
        db.query(DuplicateEmployeeNotification)
        .filter(
            DuplicateEmployeeNotification.id == notification_id
        )
        .with_for_update()
        .first()
    )

    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )

    if notification.status != DuplicateNotificationStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Notification is already acknowledged.",
        )

    notification.status = DuplicateNotificationStatus.ACKNOWLEDGED
    notification.acknowledged_at = datetime.utcnow()
    notification.acknowledged_by_id = current_user.id

    create_audit_log(
        db=db,
        action="DUPLICATE_ACKNOWLEDGED",
        actor=current_user,
        details=(
            "Acknowledged duplicate warning for employee ID "
            f"{notification.employee_id}."
        ),
    )

    db.commit()

    return {
        "id": notification.id,
        "status": notification.status,
        "acknowledged_at": notification.acknowledged_at,
    }


class DuplicateAlertDismiss(BaseModel):
    employee_ids: list[str] = Field(..., min_length=1, max_length=200)


@router.get("/notifications/duplicates/dismissed")
def get_dismissed_duplicate_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor),
):
    """Employee IDs whose duplicate alert was deleted and has not come back.

    An ID drops out of this list as soon as a new ACTIVE warning is raised
    for it (e.g. another submission with the same ID arrives).
    """
    still_active = select(
        DuplicateEmployeeNotification.employee_id
    ).where(
        DuplicateEmployeeNotification.status
        == DuplicateNotificationStatus.ACTIVE
    )

    rows = (
        db.query(DuplicateEmployeeNotification.employee_id)
        .filter(
            DuplicateEmployeeNotification.status
            == DuplicateNotificationStatus.ACKNOWLEDGED,
            DuplicateEmployeeNotification.employee_id.notin_(still_active),
        )
        .distinct()
        .all()
    )

    return {"employee_ids": sorted(row[0] for row in rows)}


@router.post("/notifications/duplicates/dismiss")
def dismiss_duplicate_alerts(
    payload: DuplicateAlertDismiss,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_supervisor),
):
    """Delete (dismiss) the selected duplicate alerts.

    Warning rows are kept for the audit trail and marked ACKNOWLEDGED; each
    deletion is written to the audit log with the supervisor who did it.
    """
    employee_ids = sorted(
        {
            employee_id.strip()[:50]
            for employee_id in payload.employee_ids
            if employee_id and employee_id.strip()
        }
    )

    if not employee_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No alerts selected.",
        )

    now = datetime.utcnow()
    dismissed: list[str] = []

    for employee_id in employee_ids:
        active = (
            db.query(DuplicateEmployeeNotification)
            .filter(
                DuplicateEmployeeNotification.employee_id == employee_id,
                DuplicateEmployeeNotification.status
                == DuplicateNotificationStatus.ACTIVE,
            )
            .with_for_update()
            .first()
        )

        if active:
            active.status = DuplicateNotificationStatus.ACKNOWLEDGED
            active.acknowledged_at = now
            active.acknowledged_by_id = current_user.id
        else:
            already_dismissed = (
                db.query(DuplicateEmployeeNotification.id)
                .filter(
                    DuplicateEmployeeNotification.employee_id == employee_id,
                    DuplicateEmployeeNotification.status
                    == DuplicateNotificationStatus.ACKNOWLEDGED,
                )
                .first()
            )

            if already_dismissed:
                continue

            # The alert exists in the dashboard but the server never raised a
            # warning for it (e.g. duplicates from before warnings existed).
            db.add(
                DuplicateEmployeeNotification(
                    employee_id=employee_id,
                    status=DuplicateNotificationStatus.ACKNOWLEDGED,
                    acknowledged_at=now,
                    acknowledged_by_id=current_user.id,
                )
            )

        create_audit_log(
            db=db,
            action="DUPLICATE_ALERT_DELETED",
            actor=current_user,
            details=f"Deleted duplicate ID alert for ID {employee_id}.",
        )
        dismissed.append(employee_id)

    db.commit()

    return {"dismissed": dismissed}