import logging

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app


SENSITIVE_MARKER = "SYNTHETIC_SECRET_DO_NOT_EXPOSE"


def simulated_database_error():
    raise OperationalError(
        "SELECT * FROM users WHERE username = :username",
        {"username": SENSITIVE_MARKER},
        RuntimeError("synthetic database failure"),
        hide_parameters=True,
    )


app.add_api_route(
    "/_test/simulated-database-error",
    simulated_database_error,
    methods=["GET"],
)


def test_real_app_returns_safe_database_error_response(caplog):
    caplog.set_level(logging.ERROR, logger="app.timing")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/simulated-database-error")


    assert response.status_code == 500

    body = response.json()
    assert body["detail"] == "An unexpected server error occurred."

    request_id = body["request_id"]
    assert request_id != "unknown"
    assert response.headers["X-Request-ID"] == request_id

    assert SENSITIVE_MARKER not in response.text
    assert SENSITIVE_MARKER not in caplog.text
    assert "unhandled_exception" in caplog.text
    assert f"request_id={request_id}" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text
