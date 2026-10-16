import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import (
    get_current_user,
    get_db,
    require_admin,
)
from app.core.config import settings
from app.core.rate_limit import ip_rate_limit, limiter, too_many_requests
from app.core.security import (
    create_access_token,
    generate_password,
    hash_password,
    username_base,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    StaffCreate,
    StaffCreateResponse,
    TokenResponse,
    UserResponse,
)
from app.services.audit import create_audit_log


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


def _unique_username(db: Session, name: str) -> str:
    base = username_base(name)
    candidate = base
    suffix = 1
    while db.query(User.id).filter(User.username == candidate).first():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


@router.post(
    "/staff",
    response_model=StaffCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_staff_user(
    user_data: StaffCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Admin creates an ADMIN or SUPERVISOR account with login credentials.

    Username and password may be supplied by the admin; whichever is
    left out is generated. A generated password is returned once.
    """
    if user_data.username:
        username = user_data.username
        if db.query(User.id).filter(User.username == username).first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this username already exists.",
            )
    else:
        username = _unique_username(db, user_data.name)

    generated_password = None
    if user_data.password:
        password = user_data.password
    else:
        password = generated_password = generate_password()

    user = User(
        name=user_data.name,
        username=username,
        password_hash=hash_password(password),
        role=user_data.role,
        is_active=True,
    )
    db.add(user)

    try:
        db.flush()  # assigns user.id and surfaces username collisions
        # The password is never written to the audit log.
        create_audit_log(
            db=db,
            action="USER_CREATED",
            actor=current_user,
            new_status="ACTIVE",
            details=(
                f"Created {user.role} account '{user.username}' "
                f"(user ID {user.id})."
            ),
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this username already exists.",
        )

    db.refresh(user)

    logger.info(
        "staff_account_created actor_id=%s actor_role=%s new_user_id=%s new_role=%s",
        current_user.id,
        current_user.role,
        user.id,
        user.role,
    )

    return StaffCreateResponse(
        id=user.id,
        name=user.name,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        temporary_password=generated_password,
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[
        Depends(
            ip_rate_limit(
                "login",
                settings.LOGIN_IP_LIMIT,
                settings.LOGIN_IP_WINDOW_SECONDS,
            )
        )
    ],
)
def login_user(
    login_data: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    # Sanitize log fields to prevent newline-based log injection.
    username_for_log = (
        login_data.username.strip().lower()[:100]
        .replace("\r", "")
        .replace("\n", "")
    )
    client_ip = request.client.host if request.client else "unknown"

    failure_key = f"login-fail:user:{username_for_log}"
    window = settings.LOGIN_FAILURE_WINDOW_SECONDS

    if (
        settings.RATE_LIMIT_ENABLED
        and limiter.count(failure_key, window) >= settings.LOGIN_FAILURE_LIMIT
    ):
        logger.warning(
            "login_locked_out username=%s ip=%s reason=too_many_failed_attempts",
            username_for_log,
            client_ip,
        )
        raise too_many_requests(
            window,
            detail="Too many failed login attempts. Please try again later.",
        )

    user = (
        db.query(User)
        .filter(User.username == login_data.username)
        .first()
    )

    if not user:
        limiter.add(failure_key, window)
        logger.warning(
            "login_failed username=%s ip=%s reason=unknown_user",
            username_for_log,
            client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    if not verify_password(login_data.password, user.password_hash):
        limiter.add(failure_key, window)
        logger.warning(
            "login_failed username=%s ip=%s reason=bad_password",
            username_for_log,
            client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    limiter.reset(failure_key)

    if not user.is_active:
        logger.warning(
            "login_failed username=%s ip=%s user_id=%s reason=inactive_account",
            username_for_log,
            client_ip,
            user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive.",
        )

    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "role": user.role,
        }
    )

    create_audit_log(
        db=db,
        action="LOGIN",
        actor=user,
        details=f"Signed in as '{user.username}'.",
    )
    db.commit()

    logger.info(
        "login_success username=%s ip=%s user_id=%s role=%s",
        username_for_log,
        client_ip,
        user.id,
        user.role,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user,
    }


@router.get(
    "/me",
    response_model=UserResponse,
)
def get_me(
    current_user: User = Depends(get_current_user),
):
    return current_user