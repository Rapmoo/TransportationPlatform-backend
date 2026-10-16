
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.security import hash_password, verify_password
from app.core.config import settings
from app.database.session import engine
from app.api.routes.auth import router as auth_router
from app.api.routes.locations import router as locations_router
from app.api.routes.submissions import router as submissions_router
from app.api.routes.admin import router as admin_router
from app.api.routes.change_requests import router as change_requests_router
from app.api.routes.change_requests import staff_router as change_request_review_router
from app.api.routes.change_requests import public_router as change_request_public_router


logger = logging.getLogger("app.timing")

SLOW_REQUEST_SECONDS = 0.5


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.warning(
            "Database warm-up failed; connecting lazily.",
            exc_info=settings.ENV.lower() != "production",
        )

    try:
        verify_password("warm-up", hash_password("warm-up"))
    except Exception:
        logger.warning(
            "Password hashing warm-up failed.",
            exc_info=settings.ENV.lower() != "production",
        )

    yield


app = FastAPI(
    title="Worker Transportation Data Platform",
    description="Backend API for collecting and managing worker transportation data.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unexpected_exception_handler(
    request: Request,
    exc: Exception,
):
    request_id = getattr(request.state, "request_id", "unknown")

    logger.error(
        "unhandled_exception request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
        exc_info=(
            type(exc),
            exc,
            exc.__traceback__,
        ),
    )

    return JSONResponse(
        status_code=500,
        headers={"X-Request-ID": request_id},
        content={
            "detail": "An unexpected server error occurred.",
            "request_id": request_id,
        },
    )


@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id

    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started

    response.headers["X-Request-ID"] = request_id
    response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.0f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'"

    if request.headers.get("Authorization"):
        response.headers["Cache-Control"] = "no-store"

    if elapsed > SLOW_REQUEST_SECONDS:
        logger.warning(
            "SLOW request_id=%s method=%s path=%s status=%s duration_ms=%.0f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed * 1000,
        )

    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS or [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Server-Timing"],
    max_age=600,
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


app.include_router(auth_router, prefix="/api")
app.include_router(submissions_router, prefix="/api")
app.include_router(locations_router, prefix="/api")
app.include_router(change_requests_router, prefix="/api")
app.include_router(change_request_review_router, prefix="/api")
app.include_router(change_request_public_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
