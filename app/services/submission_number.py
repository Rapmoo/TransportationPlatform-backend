import secrets

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.submission import Submission

# Name of the unique index created for submissions.submission_number.
SUBMISSION_NUMBER_INDEX = "ix_submissions_submission_number"

# 100_000_000 .. 999_999_999: exactly 9 digits, never a leading zero.
_LOWEST = 100_000_000
_SPAN = 900_000_000

MAX_ATTEMPTS = 8


def generate_submission_number() -> str:
    """A random 9-digit number from the operating system's CSPRNG.

    Random on purpose: a counter would be guessable and would reveal how
    many submissions exist.
    """
    return str(_LOWEST + secrets.randbelow(_SPAN))


def _is_number_collision(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None)

    if constraint is not None:
        return constraint == SUBMISSION_NUMBER_INDEX

    return "submission_number" in str(exc.orig)


def assign_submission_number(db: Session, submission: Submission) -> str:

    if submission.submission_number:
        return submission.submission_number

    for _ in range(MAX_ATTEMPTS):
        candidate = generate_submission_number()

        try:
            with db.begin_nested():
                submission.submission_number = candidate
                db.flush()
        except IntegrityError as exc:
            if not _is_number_collision(exc):
                raise
            continue

        return candidate

    raise RuntimeError(
        f"Could not generate a unique submission number in {MAX_ATTEMPTS} attempts."
    )