
import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from app.api.dependencies import get_db, get_editable_worker_submission  # noqa: E402
from app.core.security import create_access_token, create_worker_token  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.change_request import ChangeRequest, ChangeRequestStatus  # noqa: E402
from app.models.submission import Submission  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.change_requests import complete_edit  # noqa: E402

NUMBER = "123456789"

# --- test-only stand-ins for the worker edit / resubmit endpoints -----------
_stub = APIRouter()


@_stub.post("/_test/{public_id:uuid}/resubmit")
def _resubmit(
    submission: Submission = Depends(get_editable_worker_submission),
    db: Session = Depends(get_db),
):
    complete_edit(db, submission)
    db.commit()
    return {"ok": True}


app.include_router(_stub)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB)
    tables = [User.__table__, Submission.__table__, ChangeRequest.__table__, AuditLog.__table__]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    Base.metadata.drop_all(eng, tables=tables)


@pytest.fixture()
def env(engine):
    with engine.begin() as c:
        c.execute(text(
            "TRUNCATE change_requests, audit_logs, submissions, users RESTART IDENTITY CASCADE"
        ))

    Session_ = sessionmaker(bind=engine, autoflush=False)

    def override():
        db = Session_()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override

    db = Session_()
    sup = User(name="Sup", username="sup", password_hash="x", role="SUPERVISOR")
    adm = User(name="Adm", username="adm", password_hash="x", role="ADMIN")
    sub = Submission(
        public_id=uuid.uuid4(), employee_id="E1", submission_number=NUMBER,
        employee_name="Original", organization_name="Org",
        public_bus_id="1" * 16, bus_number_going="1", bus_number_return="2",
        status="SUBMITTED", submitted_at=datetime.utcnow(),
    )
    db.add_all([sup, adm, sub])
    db.commit()

    class E:
        pass

    e = E()
    e.client = TestClient(app)
    e.Session = Session_
    e.pid = sub.public_id
    e.worker = {"Authorization": f"Bearer {create_worker_token(sub.public_id)}"}
    e.sup = {"Authorization": "Bearer " + create_access_token({"sub": str(sup.id), "role": "SUPERVISOR"})}
    e.adm = {"Authorization": "Bearer " + create_access_token({"sub": str(adm.id), "role": "ADMIN"})}
    db.close()
    yield e
    app.dependency_overrides.clear()


def ask(e, comment="typo", number=NUMBER):
    return e.client.post(
        f"/submissions/{e.pid}/change-requests",
        json={"submission_number": number, "worker_comment": comment},
        headers=e.worker,
    )


def review(e, rid, action, headers=None, comment=None):
    return e.client.post(
        f"/change-requests/{rid}/{action}",
        json={"review_comment": comment},
        headers=headers or e.sup,
    )


def edit(e):
    return e.client.put(  # the real worker update route
        f"/submissions/{e.pid}",
        json={
            "employee_name": "Fixed",
            "organization_name": "Org",
            "phone_number": None,
            "public_bus_id": "1" * 16,
            "bus_number_going": "1",
            "bus_number_return": "2",
        },
        headers=e.worker,
    )


def resubmit(e):
    return e.client.post(f"/_test/{e.pid}/resubmit", headers=e.worker)


def test_01_submitted_form_is_locked_and_08_edit_without_approval_denied(env):
    assert edit(env).status_code == 409


def test_02_request_is_pending_and_supervisor_sees_it(env):
    r = ask(env)
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    listing = env.client.get("/change-requests", headers=env.sup).json()
    assert len(listing) == 1
    row = listing[0]
    assert row["employee_name"] == "Original" and row["employee_id"] == "E1"
    assert row["submission_number"] == NUMBER and row["worker_comment"] == "typo"
    assert row["status"] == "PENDING" and row["created_at"]


def test_full_flow_reject_then_approve_then_resubmit_then_no_third(env):
    r1 = ask(env).json()
    rej = review(env, r1["public_id"], "reject", comment="no")
    assert rej.status_code == 200 and rej.json()["status"] == "REJECTED"
    assert edit(env).status_code == 409  # 3: stays locked

    r2 = ask(env, "again")  # 4: request #2 allowed
    assert r2.status_code == 201
    app_ = review(env, r2.json()["public_id"], "approve")
    assert app_.status_code == 200 and app_.json()["status"] == "APPROVED"  # 5
    assert app_.json()["edit_expires_at"]
    assert edit(env).status_code == 200

    assert resubmit(env).status_code == 200  # 6
    assert edit(env).status_code == 409  # locked again

    third = ask(env, "third")  # 7
    assert third.status_code == 409
    assert "Maximum edit requests reached" in third.json()["detail"]

    with env.Session() as db:
        done = db.query(ChangeRequest).filter_by(status="COMPLETED").one()
        assert done.completed_at is not None
        rej_row = db.query(ChangeRequest).filter_by(status="REJECTED").one()
        assert rej_row.reviewed_at and rej_row.reviewed_by_id


def test_approve_then_resubmit_then_second_request_then_no_third(env):
    r1 = ask(env).json()
    review(env, r1["public_id"], "approve")
    edit(env)
    resubmit(env)
    r2 = ask(env, "second correction")
    assert r2.status_code == 201
    review(env, r2.json()["public_id"], "approve")
    resubmit(env)
    assert ask(env, "third").status_code == 409


def test_09_worker_cannot_review_own_request(env):
    rid = ask(env).json()["public_id"]
    assert review(env, rid, "approve", headers=env.worker).status_code == 403
    assert review(env, rid, "reject", headers=env.worker).status_code == 403
    assert env.client.get("/change-requests", headers=env.worker).status_code == 403
    assert edit(env).status_code == 409


def test_10_non_supervisor_cannot_review(env):
    rid = ask(env).json()["public_id"]
    assert review(env, rid, "approve", headers=env.adm).status_code == 403
    assert review(env, rid, "reject", headers=env.adm).status_code == 403


def test_11_completed_approval_cannot_be_reused(env):
    rid = ask(env).json()["public_id"]
    review(env, rid, "approve")
    assert resubmit(env).status_code == 200
    assert resubmit(env).status_code == 409
    assert edit(env).status_code == 409
    assert review(env, rid, "approve").status_code == 409
    assert review(env, rid, "reject").status_code == 409


def test_12_decided_requests_cannot_be_changed(env):
    rid = ask(env).json()["public_id"]
    review(env, rid, "reject")
    assert review(env, rid, "approve").status_code == 409
    assert edit(env).status_code == 409

    rid2 = ask(env, "b").json()["public_id"]
    review(env, rid2, "approve")
    assert review(env, rid2, "reject").status_code == 409
    assert edit(env).status_code == 200  # approval untouched


def test_only_one_open_request_and_wrong_number_rejected(env):
    assert ask(env, number="987654321").status_code == 403
    assert ask(env).status_code == 201
    assert ask(env).status_code == 409
    assert env.client.post(
        f"/submissions/{env.pid}/change-requests",
        json={"submission_number": "12", "worker_comment": "x"},
        headers=env.worker,
    ).status_code == 422


def test_other_workers_token_is_rejected(env):
    other = {"Authorization": f"Bearer {create_worker_token(uuid.uuid4())}"}
    r = env.client.post(
        f"/submissions/{env.pid}/change-requests",
        json={"submission_number": NUMBER, "worker_comment": "x"},
        headers=other,
    )
    assert r.status_code == 403


def test_expired_approval_denies_edit_and_frees_the_slot(env):
    rid = ask(env).json()["public_id"]
    review(env, rid, "approve")
    with env.Session() as db:
        cr = db.query(ChangeRequest).one()
        cr.edit_expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()

    assert edit(env).status_code == 409
    assert resubmit(env).status_code == 409
    assert ask(env, "after expiry").status_code == 201  # expired one counts as #1

    with env.Session() as db:
        assert db.query(ChangeRequest).filter_by(status="EXPIRED").count() == 1
        assert db.query(AuditLog).filter_by(action="CHANGE_REQUEST_EXPIRED").count() == 1