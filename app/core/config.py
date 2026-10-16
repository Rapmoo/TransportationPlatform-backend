from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, resolved independent of the current working directory
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

_PLACEHOLDER_MARKERS = (
    "CHANGE",
    "SECRET_KEY",
    "YOUR_",
    "EXAMPLE",
    "PLACEHOLDER",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str
    SECRET_KEY: str

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    WORKER_TOKEN_EXPIRE_HOURS: int = 24

    # How long an approved change request lets the worker edit.
    CHANGE_REQUEST_EDIT_WINDOW_HOURS: int = 24

    # JSON list in the env file, e.g.
    # CORS_ORIGINS=["https://admin.example.com"]
    CORS_ORIGINS: list[str] = []

    ENV: str = "production"

    # ---- Rate limiting (see app/core/rate_limit.py) ----
    RATE_LIMIT_ENABLED: bool = True

    # Set to share counters between several workers/instances,
    # e.g. REDIS_URL=redis://localhost:6379/0 (needs: pip install redis).
    # Leave unset to use per-process memory (fine for a single worker).
    REDIS_URL: str | None = None

    # Number of reverse proxies you control in front of the app (nginx = 1).
    # 0 = trust only the direct connection and ignore X-Forwarded-For.
    TRUSTED_PROXY_HOPS: int = 0

    # POST /api/auth/login: requests per IP, and failed logins per username.
    LOGIN_IP_LIMIT: int = 20
    LOGIN_IP_WINDOW_SECONDS: int = 60
    LOGIN_FAILURE_LIMIT: int = 8
    LOGIN_FAILURE_WINDOW_SECONDS: int = 15 * 60

    # POST /api/submissions/ (public): drafts created per IP. Many workers can
    # share one IP (office Wi-Fi, mobile-carrier NAT), so this is generous; it
    # only stops a single address flooding the database.
    SUBMISSION_CREATE_LIMIT: int = 500
    SUBMISSION_CREATE_WINDOW_SECONDS: int = 60 * 60

    # SQLAlchemy connection pool PER PROCESS. Total connections the database
    # must allow = workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW).
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10

    # /api/change-requests/* (public): all requests per IP, and wrong
    # submission-number guesses per IP and per number.
    PUBLIC_IP_LIMIT: int = 60
    PUBLIC_IP_WINDOW_SECONDS: int = 60
    LOOKUP_FAILURE_LIMIT: int = 8
    LOOKUP_FAILURE_WINDOW_SECONDS: int = 15 * 60

    @field_validator("SECRET_KEY")
    @classmethod
    def strong_secret(cls, v: str) -> str:
        if len(v) < 32 or any(m in v.upper() for m in _PLACEHOLDER_MARKERS):
            raise ValueError(
                "SECRET_KEY must be a random string of 32+ chars. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        return v

    @field_validator(
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "WORKER_TOKEN_EXPIRE_HOURS",
        "CHANGE_REQUEST_EDIT_WINDOW_HOURS",
    )
    @classmethod
    def positive_expiry(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Token expiry settings must be greater than 0")
        return v

    @field_validator(
        "LOGIN_IP_LIMIT",
        "LOGIN_IP_WINDOW_SECONDS",
        "LOGIN_FAILURE_LIMIT",
        "LOGIN_FAILURE_WINDOW_SECONDS",
        "SUBMISSION_CREATE_LIMIT",
        "SUBMISSION_CREATE_WINDOW_SECONDS",
        "PUBLIC_IP_LIMIT",
        "PUBLIC_IP_WINDOW_SECONDS",
        "LOOKUP_FAILURE_LIMIT",
        "LOOKUP_FAILURE_WINDOW_SECONDS",
        "DB_POOL_SIZE",
    )
    @classmethod
    def positive_rate_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Rate limit settings must be greater than 0")
        return v

    @field_validator("TRUSTED_PROXY_HOPS")
    @classmethod
    def non_negative_hops(cls, v: int) -> int:
        if v < 0:
            raise ValueError("TRUSTED_PROXY_HOPS cannot be negative")
        return v

    @field_validator("CORS_ORIGINS")
    @classmethod
    def no_wildcard_origin(cls, v: list[str]) -> list[str]:
        # The API sends credentials, so a wildcard origin is unsafe.
        if any(o.strip() == "*" for o in v):
            raise ValueError(
                "CORS_ORIGINS must list explicit origins, not '*'."
            )
        return [o.strip().rstrip("/") for o in v if o.strip()]


settings = Settings()