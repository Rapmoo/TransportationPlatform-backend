
import logging
import math
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

# Client IP


def get_client_ip(request: Request) -> str:

    hops = settings.TRUSTED_PROXY_HOPS

    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]

        if len(parts) >= hops:
            return parts[-hops]

    return request.client.host if request.client else "unknown"



# Backends
class _MemoryBackend:

    _SWEEP_EVERY = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = {}
        self._windows: dict[str, int] = {}
        self._ops = 0

    def _prune(self, key: str, window: int, now: float) -> deque[float] | None:
        bucket = self._events.get(key)

        if bucket is None:
            return None

        while bucket and now - bucket[0] >= window:
            bucket.popleft()

        if not bucket:
            del self._events[key]
            self._windows.pop(key, None)
            return None

        return bucket

    def _sweep(self, now: float) -> None:

        self._ops += 1

        if self._ops % self._SWEEP_EVERY:
            return

        for key in list(self._events):
            self._prune(key, self._windows.get(key, 0), now)

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()

        with self._lock:
            self._sweep(now)
            bucket = self._prune(key, window, now)

            if bucket is not None and len(bucket) >= limit:
                retry = max(1, math.ceil(window - (now - bucket[0])))
                return False, retry

            if bucket is None:
                bucket = self._events[key] = deque()

            bucket.append(now)
            self._windows[key] = window
            return True, 0

    def count(self, key: str, window: int) -> int:
        now = time.monotonic()

        with self._lock:
            bucket = self._prune(key, window, now)
            return len(bucket) if bucket is not None else 0

    def add(self, key: str, window: int) -> None:
        now = time.monotonic()

        with self._lock:
            self._sweep(now)
            bucket = self._prune(key, window, now)

            if bucket is None:
                bucket = self._events[key] = deque()

            bucket.append(now)
            self._windows[key] = window

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
            self._windows.pop(key, None)


class _RedisBackend:
    def __init__(self, url: str) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "Run: pip install redis"
            ) from exc

        self._redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=1,
        )

    def _incr(self, key: str, window: int) -> tuple[int, int]:
        name = f"rl:{key}"
        value = int(self._redis.incr(name))
        ttl = int(self._redis.ttl(name))

        # First hit, or a key that somehow lost its expiry: (re)start the window.
        if value == 1 or ttl < 0:
            self._redis.expire(name, window)
            ttl = window

        return value, ttl

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        value, ttl = self._incr(key, window)
        return value <= limit, max(1, ttl)

    def count(self, key: str, window: int) -> int:
        return int(self._redis.get(f"rl:{key}") or 0)

    def add(self, key: str, window: int) -> None:
        self._incr(key, window)

    def reset(self, key: str) -> None:
        self._redis.delete(f"rl:{key}")


class RateLimiter:

    def __init__(self) -> None:
        if settings.REDIS_URL:
            self._backend = _RedisBackend(settings.REDIS_URL)
        else:
            self._backend = _MemoryBackend()

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        try:
            return self._backend.hit(key, limit, window)
        except Exception:
            logger.warning("Rate limiter backend error; allowing request.", exc_info=settings.ENV.lower() != "production")
            return True, 0

    def count(self, key: str, window: int) -> int:
        """How many events are currently recorded for key."""
        try:
            return self._backend.count(key, window)
        except Exception:
            logger.warning("Rate limiter backend error; allowing request.", exc_info=settings.ENV.lower() != "production")
            return 0

    def add(self, key: str, window: int) -> None:
        """Record one event (e.g. a failed login) without checking a limit."""
        try:
            self._backend.add(key, window)
        except Exception:
            logger.warning("Rate limiter backend error.", exc_info=settings.ENV.lower() != "production")

    def reset(self, key: str) -> None:
        try:
            self._backend.reset(key)
        except Exception:
            logger.warning("Rate limiter backend error.", exc_info=settings.ENV.lower() != "production")


limiter = RateLimiter()

# FastAPI helpers


def too_many_requests(
    retry_after: int | None = None,
    detail: str = "Too many requests. Please try again later.",
) -> HTTPException:
    headers = {"Retry-After": str(retry_after)} if retry_after else None

    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers=headers,
    )


def ip_rate_limit(scope: str, limit: int, window_seconds: int):

    def dependency(request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        allowed, retry_after = limiter.hit(
            f"{scope}:ip:{get_client_ip(request)}",
            limit,
            window_seconds,
        )

        if not allowed:
            raise too_many_requests(retry_after)

    return dependency