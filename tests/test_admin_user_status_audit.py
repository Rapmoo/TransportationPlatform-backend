
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.api.routes import admin as admin_routes

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import dependencies


def test_update_user_status_records_structured_audit(monkeypatch):
    # Fake users: no real accounts or database are involved.
    admin_user = SimpleNamespace(
        id=1,
        username="test_admin",
        role="ADMIN",
    )

    staff_user = SimpleNamespace(
        id=2,
        username="test_supervisor",
        role="SUPERVISOR",
        is_active=False,
    )

    # Mock the database query to return our fake staff user.
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        staff_user
    )

    # Capture the audit-log arguments instead of inserting a record.
    audit_calls = []

    def fake_create_audit_log(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(
        admin_routes,
        "create_audit_log",
        fake_create_audit_log,
    )

    # Call the endpoint function directly.
    result = admin_routes.update_user_status(
        user_id=2,
        is_active=True,
        db=db,
        current_user=admin_user,
    )

    # Verify the status change.
    assert result["is_active"] is True
    assert staff_user.is_active is True

    # Verify the audit event.
    assert len(audit_calls) == 1
    audit = audit_calls[0]

    assert audit["action"] == "USER_STATUS_CHANGE"
    assert audit["actor"] is admin_user
    assert audit["old_status"] == "INACTIVE"
    assert audit["new_status"] == "ACTIVE"
    assert "test_supervisor" in audit["details"]

    # Verify the mocked transaction was committed.
    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(staff_user)


import pytest
from fastapi import HTTPException


def test_admin_cannot_deactivate_own_account():
    admin_user = SimpleNamespace(
        id=1,
        username="test_admin",
        role="ADMIN",
        is_active=True,
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        admin_user
    )

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.update_user_status(
            user_id=1,
            is_active=False,
            db=db,
            current_user=admin_user,
        )

    assert exc_info.value.status_code == 400
    assert "cannot deactivate your own account" in (
        exc_info.value.detail.lower()
    )

    # No status change or transaction should occur.
    assert admin_user.is_active is True
    db.commit.assert_not_called()


def test_worker_account_cannot_be_managed():
    admin_user = SimpleNamespace(
        id=1,
        username="test_admin",
        role="ADMIN",
        is_active=True,
    )

    worker_user = SimpleNamespace(
        id=3,
        username="test_worker",
        role="WORKER",
        is_active=True,
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        worker_user
    )

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.update_user_status(
            user_id=3,
            is_active=False,
            db=db,
            current_user=admin_user,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Only staff accounts can be managed."

    # The worker remains unchanged.
    assert worker_user.is_active is True
    db.commit.assert_not_called()


def test_worker_gets_forbidden_on_user_status_http_endpoint():
    test_app = FastAPI()
    test_app.include_router(
        admin_routes.router,
        prefix="/api",
    )

    worker_user = SimpleNamespace(
        id=99,
        username="test_worker",
        role="WORKER",
        is_active=True,
    )

    # Bypass token/database lookup, but keep the real require_admin check.
    def fake_current_user():
        return worker_user

    test_app.dependency_overrides[
        dependencies.get_current_user
    ] = fake_current_user

    try:
        with TestClient(test_app) as client:
            response = client.put(
                "/api/admin/users/2/status",
                params={"is_active": False},
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "Admin access required."
    finally:
        test_app.dependency_overrides.clear()
