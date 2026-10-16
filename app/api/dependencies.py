
import logging
import uuid
from collections.abc import Generator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.security import WORKER_ROLE, decode_access_token
from app.database.session import SessionLocal
from app.models.submission import Submission
from app.models.user import User
from app.services.change_requests import get_active_grant


logger = logging.getLogger(__name__)

http_bearer = HTTPBearer()
worker_bearer = HTTPBearer(
    description="Worker token returned when the submission was created"
)


def log_access_denial(
    request: Request,
    reason: str,
    actor_id: str = "unknown",
    actor_role: str = "unknown",
) -> None:
    """Log an access denial without recording credentials or tokens."""
    client_ip = request.client.host if request.client else "unknown"

    logger.warning(
        "access_denied actor_id=%s actor_role=%s method=%s path=%s "
        "ip=%s reason=%s",
        str(actor_id)[:100].replace("\r", "").replace("\n", ""),
        str(actor_role)[:50].replace("\r", "").replace("\n", ""),
        request.method,
        request.url.path,
        client_ip,
        reason,
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError:
        log_access_denial(request, reason="invalid_or_expired_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_role = payload.get("role", "unknown")
    token_subject = payload.get("sub", "unknown")

    if token_role == WORKER_ROLE:
        log_access_denial(
            request,
            actor_id=token_subject,
            actor_role=WORKER_ROLE,
            reason="worker_token_used_on_staff_endpoint",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Worker tokens cannot access staff endpoints.",
        )

    user_id = payload.get("sub")

    if not user_id:
        log_access_denial(
            request,
            actor_role=token_role,
            reason="token_missing_subject",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log_access_denial(
            request,
            actor_id=user_id,
            actor_role=token_role,
            reason="invalid_token_subject",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user:
        log_access_denial(
            request,
            actor_id=user_id,
            actor_role=token_role,
            reason="user_not_found",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        log_access_denial(
            request,
            actor_id=user.id,
            actor_role=user.role,
            reason="inactive_account",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive.",
        )

    return user


def require_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != "ADMIN":
        log_access_denial(
            request,
            actor_id=current_user.id,
            actor_role=current_user.role,
            reason="admin_role_required",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )

    return current_user


def require_supervisor(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != "SUPERVISOR":
        log_access_denial(
            request,
            actor_id=current_user.id,
            actor_role=current_user.role,
            reason="supervisor_role_required",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Supervisor access required.",
        )

    return current_user


def require_supervisor_or_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role not in {"SUPERVISOR", "ADMIN"}:
        log_access_denial(
            request,
            actor_id=current_user.id,
            actor_role=current_user.role,
            reason="supervisor_or_admin_role_required",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Supervisor or admin access required.",
        )

    return current_user


def get_worker_submission(
    public_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(worker_bearer),
    db: Session = Depends(get_db),
) -> Submission:
    """Resolve a submission only when the worker token owns that submission."""
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError:
        log_access_denial(
            request,
            reason="invalid_or_expired_worker_token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_role = payload.get("role", "unknown")
    token_subject = payload.get("sub", "unknown")

    if (
        token_role != WORKER_ROLE
        or token_subject != str(public_id)
    ):
        log_access_denial(
            request,
            actor_id=token_subject,
            actor_role=token_role,
            reason="worker_submission_ownership_mismatch",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not your submission.",
        )

    submission = (
        db.query(Submission)
        .filter(Submission.public_id == public_id)
        .first()
    )

    if not submission:
        log_access_denial(
            request,
            actor_id=token_subject,
            actor_role=token_role,
            reason="owned_submission_not_found",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Submission not found.",
        )

    return submission


def get_editable_worker_submission(
    request: Request,
    submission: Submission = Depends(get_worker_submission),
    db: Session = Depends(get_db),
) -> Submission:
    """Require an active approval grant to edit a submitted form."""
    if submission.status != "SUBMITTED":
        return submission

    if get_active_grant(db, submission.id) is None:
        log_access_denial(
            request,
            actor_id=submission.public_id,
            actor_role=WORKER_ROLE,
            reason="submitted_form_locked_no_active_edit_grant",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This submission is locked. Request permission from a "
                "supervisor to edit it."
            ),
        )

    return submission
