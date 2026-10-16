import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import InvalidTokenError
from passlib.context import CryptContext

from app.core.config import settings

# The signing key and token lifetime come from environment variables
# (see app/core/config.py). Nothing secret lives in the source code.
SECRET_KEY = settings.SECRET_KEY
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(
    plain_password: str,
    hashed_password: str,
) -> bool:
    return pwd_context.verify(
        plain_password,
        hashed_password,
    )


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
) -> str:
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})

    return jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


WORKER_ROLE = "worker"


def create_worker_token(public_id: uuid.UUID) -> str:
    """Short-lived ownership token for one worker submission.

    sub  = the submission's public_id (UUID)
    role = "worker"  (staff tokens carry ADMIN / SUPERVISOR instead)
    """
    return create_access_token(
        data={"sub": str(public_id), "role": WORKER_ROLE},
        expires_delta=timedelta(hours=settings.WORKER_TOKEN_EXPIRE_HOURS),
    )


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
        )
    except InvalidTokenError as exc:
        raise ValueError("Invalid or expired token.") from exc


# No 0/O, 1/l/I so a password read out loud or copied by hand is not misread.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def generate_password(length: int = 12) -> str:
    """Random password that always mixes upper, lower and digits."""
    while True:
        password = "".join(
            secrets.choice(_PASSWORD_ALPHABET) for _ in range(length)
        )
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


def username_base(name: str) -> str:
    """'Abebe Kebede' -> 'abebe.kebede' (at least 3 characters)."""
    base = re.sub(r"[^a-z0-9]+", ".", name.strip().lower()).strip(".")
    return (base or "user")[:40].ljust(3, "0")