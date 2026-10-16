from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def test_unexpected_exception_returns_safe_500():
    test_app = FastAPI()

    @test_app.exception_handler(Exception)
    async def unexpected_exception_handler(
        request: Request,
        exc: Exception,
    ):
        return JSONResponse(
            status_code=500,
            content={
                "detail": "An unexpected server error occurred."
            },
        )

    @test_app.get("/trigger-test-error")
    def trigger_test_error():
        raise RuntimeError("synthetic test error")

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/trigger-test-error")

    assert response.status_code == 500
    assert response.json() == {
        "detail": "An unexpected server error occurred."
    }
    assert "synthetic test error" not in response.text