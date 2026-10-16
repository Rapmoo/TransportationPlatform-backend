from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.routes import admin as admin_routes


def make_submission_db(submission):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        submission
    )
    return db


def test_valid_submission_status_change_records_audit(monkeypatch):
    admin_user = SimpleNamespace(id=1, role="ADMIN")
    submission = SimpleNamespace(id=10, status="SUBMITTED")
    db = make_submission_db(submission)
    audit_calls = []

    monkeypatch.setattr(
        admin_routes,
        "create_audit_log",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    result = admin_routes.update_submission_status(
        submission_id=10,
        new_status="approved",
        db=db,
        current_user=admin_user,
    )

    assert result["old_status"] == "SUBMITTED"
    assert result["new_status"] == "APPROVED"
    assert submission.status == "APPROVED"

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "STAFF_STATUS_CHANGE"
    assert audit_calls[0]["actor"] is admin_user
    assert audit_calls[0]["submission_id"] == 10
    assert audit_calls[0]["old_status"] == "SUBMITTED"
    assert audit_calls[0]["new_status"] == "APPROVED"

    db.commit.assert_called_once()
    db.refresh.assert_called_once_with(submission)


def test_invalid_submission_status_does_not_change_or_audit(
    monkeypatch,
):
    admin_user = SimpleNamespace(id=1, role="ADMIN")
    submission = SimpleNamespace(id=10, status="SUBMITTED")
    db = make_submission_db(submission)
    audit_calls = []

    monkeypatch.setattr(
        admin_routes,
        "create_audit_log",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.update_submission_status(
            submission_id=10,
            new_status="INVALID_STATUS",
            db=db,
            current_user=admin_user,
        )

    assert exc_info.value.status_code == 400
    assert submission.status == "SUBMITTED"
    assert audit_calls == []
    db.commit.assert_not_called()
    db.refresh.assert_not_called()


def test_unchanged_submission_status_does_not_create_audit(
    monkeypatch,
):
    admin_user = SimpleNamespace(id=1, role="ADMIN")
    submission = SimpleNamespace(id=10, status="SUBMITTED")
    db = make_submission_db(submission)
    audit_calls = []

    monkeypatch.setattr(
        admin_routes,
        "create_audit_log",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    result = admin_routes.update_submission_status(
        submission_id=10,
        new_status="SUBMITTED",
        db=db,
        current_user=admin_user,
    )

    assert result["old_status"] == "SUBMITTED"
    assert result["new_status"] == "SUBMITTED"
    assert audit_calls == []
    db.commit.assert_not_called()
    db.refresh.assert_not_called()
