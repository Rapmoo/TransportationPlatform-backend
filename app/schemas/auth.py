from pydantic import BaseModel, ConfigDict, Field, field_validator

STAFF_ROLES = ("ADMIN", "SUPERVISOR")


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(..., min_length=1, max_length=50)


class StaffCreate(BaseModel):
    """Admin creates a staff account (ADMIN or SUPERVISOR).

    username and password are optional: leave either one out and the
    server generates it. A generated password is returned once in the
    response and is never stored in plain text.
    """

    name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., min_length=1, max_length=50)
    username: str | None = Field(None, min_length=3, max_length=255)
    password: str | None = Field(None, min_length=8, max_length=128)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        role = value.strip().upper()
        if role not in STAFF_ROLES:
            raise ValueError("Role must be ADMIN or SUPERVISOR.")
        return role

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required.")
        return value

    @field_validator("username")
    @classmethod
    def strip_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Username must be at least 3 characters.")
        return value


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    username: str
    role: str
    is_active: bool


class StaffCreateResponse(UserResponse):
    """Returned once, right after creation.

    temporary_password is set only when the server generated the
    password. Share it with the new user; it cannot be shown again.
    """

    temporary_password: str | None = None


class CredentialsUpdate(BaseModel):
    """Change a staff account's username, password, or both.

    current_password is required only when an administrator changes
    their own account.
    """

    username: str | None = Field(None, min_length=3, max_length=255)
    password: str | None = Field(None, min_length=8, max_length=128)
    current_password: str | None = Field(None, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    # Returned with the token so the client does not need a second
    # round-trip to /auth/me right after signing in.
    user: UserResponse | None = None