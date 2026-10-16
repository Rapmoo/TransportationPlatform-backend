import itertools
import os
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

TEST_DB = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")

if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

from app.api.dependencies import get_db  # noqa: E402
from app.core.security import create_access_token, create_worker_token  # noqa: E402
from app.database.base import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.duplicate_employee_notification import (  # noqa: E402
    DuplicateEmployeeNotification as N,
    DuplicateNotificationStatus as S,
)
from app.models.location import Location  # noqa: E402
from app.models.submission import Submission  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.duplicate_notifications import notify_if_duplicate  # noqa: E402

URL = "/admin/notifications/duplicates"
_numbers = itertools.count(500000001)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(TEST_DB)
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    tables = [
        User.__table__,
        Submission.__table__,
        AuditLog.__table__,
        N.__table__,
        Location.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    with eng.begin() as c:
        c.execute(text(
            "DROP TABLE IF EXISTS locations, duplicate_employee_notifications, "
            "audit_logs, submissions, users CASCADE"
        ))


@pytest.fixture()
def env(engine):
    with engine.begin() as c:
        c.execute(text(
            "TRUNCATE locations, duplicate_employee_notifications, audit_logs, submissions, users "
            "RESTART IDENTITY CASCADE"
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
    db.add_all([sup, adm])
    db.commit()

    class E:
        pass

    e = E()
    e.client = TestClient(app)
    e.db = db
    e.sup = {"Authorization": "Bearer " + create_access_token({"sub": str(sup.id), "role": "SUPERVISOR"})}
    e.adm = {"Authorization": "Bearer " + create_access_token({"sub": str(adm.id), "role": "ADMIN"})}
    e.worker = {"Authorization": f"Bearer {create_worker_token(uuid.uuid4())}"}
    yield e
    db.close()
    app.dependency_overrides.clear()


def make(db, employee_id="EMP12345", name="John Doe", status="SUBMITTED"):
    s = Submission(
        employee_id=employee_id,
        employee_name=name,
        organization_name="ABC Company",
        public_bus_id="1" * 16,
        bus_number_going="A1",
        bus_number_return="A2",
        status=status,
        submitted_at=datetime.utcnow() if status != "DRAFT" else None,
        submission_number=str(next(_numbers)) if status != "DRAFT" else None,
    )
    db.add(s)
    db.flush()
    return s


def payload(emp="EMP12345"):
    return {
        "employee_id": emp,
        "employee_name": "X",
        "organization_name": "ABC",
        "public_bus_id": "1" * 16,
        "bus_number_going": "A1",
        "bus_number_return": "A2",
    }


def test_01_same_employee_id_never_409(env):
    assert env.client.post("/submissions/", json=payload()).status_code == 201
    assert env.client.post("/submissions/", json=payload()).status_code == 201


def test_02_single_submission_no_notification(env):
    s = make(env.db)
    assert notify_if_duplicate(env.db, s) is None
    assert env.db.query(N).count() == 0


def test_03_second_submission_creates_one_active_notification(env):
    make(env.db)
    b = make(env.db, name="Jane Doe")
    n = notify_if_duplicate(env.db, b)
    assert n is not None and n.status == S.ACTIVE and n.employee_id == "EMP12345"


def test_04_third_and_fourth_do_not_create_more(env):
    make(env.db)
    notify_if_duplicate(env.db, make(env.db))
    assert notify_if_duplicate(env.db, make(env.db)) is None
    assert notify_if_duplicate(env.db, make(env.db)) is None
    env.db.commit()
    items = env.client.get(URL, headers=env.sup).json()["items"]
    assert len(items) == 1 and items[0]["submission_count"] == 4


def test_05_drafts_are_ignored(env):
    make(env.db, status="DRAFT")
    b = make(env.db)
    assert notify_if_duplicate(env.db, b) is None


def test_06_different_employee_ids_not_grouped(env):
    make(env.db, "EMP1")
    b = make(env.db, "EMP2")
    assert notify_if_duplicate(env.db, b) is None


def test_07_supervisor_response_shape(env):
    make(env.db, name="John Doe")
    notify_if_duplicate(env.db, make(env.db, name="Jane Doe"))
    env.db.commit()
    item = env.client.get(URL, headers=env.sup).json()["items"][0]
    assert item["employee_id"] == "EMP12345"
    assert "Duplicate Employee ID detected" in item["message"]
    assert {s["employee_name"] for s in item["submissions"]} == {"John Doe", "Jane Doe"}
    assert all(len(s["submission_number"]) == 9 for s in item["submissions"])


def test_08_admin_forbidden(env):
    assert env.client.get(URL, headers=env.adm).status_code == 403


def test_09_worker_token_forbidden(env):
    assert env.client.get(URL, headers=env.worker).status_code == 403
    assert env.client.post(f"{URL}/1/acknowledge", headers=env.worker).status_code == 403


def test_10_unauthenticated_rejected(env):
    assert env.client.get(URL).status_code in (401, 403)


def test_11_refreshing_creates_no_rows(env):
    make(env.db)
    notify_if_duplicate(env.db, make(env.db))
    env.db.commit()
    for _ in range(3):
        env.client.get(URL, headers=env.sup)
    assert env.db.query(N).count() == 1


def test_12_acknowledge_keeps_history_and_new_duplicate_renotifies(env):
    make(env.db)
    n = notify_if_duplicate(env.db, make(env.db))
    env.db.commit()
    nid = n.id
    assert env.client.post(f"{URL}/{nid}/acknowledge", headers=env.sup).status_code == 200
    assert env.client.post(f"{URL}/{nid}/acknowledge", headers=env.sup).status_code == 409
    assert notify_if_duplicate(env.db, make(env.db)) is not None
    env.db.commit()
    assert env.db.query(N).count() == 2  # history kept


def test_13_admin_cannot_acknowledge(env):
    make(env.db)
    n = notify_if_duplicate(env.db, make(env.db))
    env.db.commit()
    assert env.client.post(f"{URL}/{n.id}/acknowledge", headers=env.adm).status_code == 403


def test_14_db_rejects_two_active_for_same_employee_id(env):
    env.db.add(N(employee_id="EMP9"))
    env.db.flush()
    env.db.add(N(employee_id="EMP9"))
    with pytest.raises(IntegrityError):
        env.db.flush()
    env.db.rollback()


def _create_and_submit(env, name):
    created = env.client.post("/submissions/", json={**payload(), "employee_name": name}).json()
    sid = env.db.query(Submission.id).filter(
        Submission.public_id == uuid.UUID(created["public_id"])
    ).scalar()
    for loc_type in ("HOME", "STARTING_POINT", "BOARDING", "RETURN_STARTING_POINT"):
        env.db.execute(
            text(
                "INSERT INTO locations "
                "(submission_id, location_type, address_text, geom, created_at) "
                "VALUES (:sid, :t, 'Addis Ababa', "
                "ST_SetSRID(ST_MakePoint(38.75, 9.02), 4326), NOW())"
            ),
            {"sid": sid, "t": loc_type},
        )
    env.db.commit()
    return env.client.post(
        f"/submissions/{created['public_id']}/submit",
        headers={"Authorization": f"Bearer {created['worker_token']}"},
    )


def test_15_real_submit_endpoint_detects_duplicates(env):
    first = _create_and_submit(env, "John Doe")
    assert first.status_code == 200
    assert env.client.get(URL, headers=env.sup).json()["total"] == 0  # no duplicate yet

    second = _create_and_submit(env, "Jane Doe")
    assert second.status_code == 200  # never blocked

    data = env.client.get(URL, headers=env.sup).json()
    assert data["total"] == 1
    assert data["items"][0]["submission_count"] == 2

    third = _create_and_submit(env, "Third Person")
    assert third.status_code == 200
    data = env.client.get(URL, headers=env.sup).json()
    assert data["total"] == 1  # no repeat notification
    assert data["items"][0]["submission_count"] == 3