from datetime import datetime, timedelta

from app.models.change_request import ChangeRequest, ChangeRequestStatus


def make_request(status, expires_at, completed_at=None):
    request = ChangeRequest(
        status=status,
        edit_expires_at=expires_at,
        completed_at=completed_at,
    )
    return request


def test_active_approval_grants_edit_before_expiry():
    now = datetime.utcnow()
    request = make_request(
        ChangeRequestStatus.APPROVED,
        now + timedelta(minutes=5),
    )
    assert request.grants_edit(now) is True


def test_approval_does_not_grant_edit_at_exact_expiry():
    now = datetime.utcnow()
    request = make_request(ChangeRequestStatus.APPROVED, now)
    assert request.grants_edit(now) is False


def test_expired_approval_does_not_grant_edit():
    now = datetime.utcnow()
    request = make_request(
        ChangeRequestStatus.APPROVED,
        now - timedelta(seconds=1),
    )
    assert request.grants_edit(now) is False


def test_completed_approval_does_not_grant_edit():
    now = datetime.utcnow()
    request = make_request(
        ChangeRequestStatus.APPROVED,
        now + timedelta(minutes=5),
        completed_at=now,
    )
    assert request.grants_edit(now) is False


def test_pending_request_does_not_grant_edit():
    now = datetime.utcnow()
    request = make_request(
        ChangeRequestStatus.PENDING,
        now + timedelta(minutes=5),
    )
    assert request.grants_edit(now) is False


def test_approval_without_expiry_does_not_grant_edit():
    now = datetime.utcnow()
    request = make_request(ChangeRequestStatus.APPROVED, None)
    assert request.grants_edit(now) is False
