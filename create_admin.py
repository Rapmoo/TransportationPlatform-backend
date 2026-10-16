"""Create the first ADMIN (Superadmin) account.

Run from the backend folder, with your venv active:

    python create_admin.py

The password is never stored in this file. It is read from the
ADMIN_PASSWORD environment variable if set (useful for CI / deployment),
otherwise you are prompted for it (input is hidden). Optional env vars:
ADMIN_USERNAME (default "admin") and ADMIN_NAME (default "Super Admin").

Safe to re-run: it won't create a duplicate if the username already exists.
"""
import getpass
import os
import sys

from app.core.security import hash_password
from app.database.session import SessionLocal
from app.models.user import User

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128  # matches the API schemas
# Passwords that shipped in earlier versions of this project.
KNOWN_BAD_PASSWORDS = {"changeme123!", "changeme", "password", "admin"}


def validate_password(password: str, username: str) -> str | None:
    """Return an error message, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if password.lower() in KNOWN_BAD_PASSWORDS or "changeme" in password.lower():
        return "That password is a known default. Choose a different one."
    if username.lower() in password.lower():
        return "Password must not contain the username."
    return None


def get_password(username: str) -> str:
    env_password = os.environ.get("ADMIN_PASSWORD")
    if env_password:
        error = validate_password(env_password, username)
        if error:
            sys.exit(f"ADMIN_PASSWORD rejected: {error}")
        return env_password

    if not sys.stdin.isatty():
        sys.exit("No terminal available. Set the ADMIN_PASSWORD environment variable.")

    while True:
        password = getpass.getpass("Admin password: ")
        error = validate_password(password, username)
        if error:
            print(error)
            continue
        if getpass.getpass("Confirm password: ") != password:
            print("Passwords do not match. Try again.")
            continue
        return password


def main() -> None:
    name = os.environ.get("ADMIN_NAME", "Super Admin")
    username = os.environ.get("ADMIN_USERNAME", "admin")

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"User {username} already exists (role={existing.role}). Nothing to do.")
            return

        password = get_password(username)

        db.add(
            User(
                name=name,
                username=username,
                password_hash=hash_password(password),
                role="ADMIN",
                is_active=True,
            )
        )
        db.commit()
        print(f"Created ADMIN user: {username}")
    finally:
        db.close()


if __name__ == "__main__":
    main()