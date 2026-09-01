import logging
import time
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import licenses
from app.core.config import settings
from app.core.database import Base, engine
from app.core.health_probes import run_readiness_probes
from app.core.limiter import limiter
from app.core.migrations import ensure_schema
from app.core.sentry import init_sentry

# Import models so every table registers on Base.metadata before create_all/sync_schema.
from app.models import models  # noqa: F401

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_sentry(
    dsn=settings.SENTRY_DSN or None,
    traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
)

# This service's DB is tiny (two tables) — unlike Command Center, there's
# no need to defer index creation to a background task; it's near-instant.
ensure_schema(engine, Base.metadata)

_STARTED_AT_MONO = time.monotonic()

app = FastAPI(
    title="Sentinel License Service",
    description="License validation for self-hosted Sentinel Command Center installs.",
    version="0.1.0",
    docs_url="/api-docs",
    redoc_url="/api-redoc",
    openapi_url="/api/openapi.json",
)

app.state.limiter = limiter


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    limit_str = str(exc.detail) if getattr(exc, "detail", None) else "rate limit exceeded"
    body = {
        "error": "rate_limit_exceeded",
        "message": "Too many requests. Back off and retry after the Retry-After window.",
        "limit": limit_str,
        "retry_after_seconds": 60,
    }
    return JSONResponse(status_code=429, content=body, headers={"Retry-After": "60"})


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(licenses.router)


@app.get("/health")
async def health():
    """Pure liveness — must never be slow. This is what Fly's health
    check polls; a slow dependency shouldn't pull the only machine out
    of rotation."""
    return {"status": "healthy", "version": "0.1.0"}


@app.get("/health/ready")
def health_ready():
    """Readiness — 503 if a critical probe fails, 200 otherwise.

    Plain `def`, not `async def` — run_readiness_probes() does blocking
    SQLite + disk I/O. Same reasoning as check_in in app/api/licenses.py:
    an async def running blocking calls inline would stall the sole
    event-loop thread (--workers 1) for the probe's duration, including
    this process's ability to dispatch the check-in endpoint itself.
    """
    report = run_readiness_probes()
    status_code = 200 if report.ready else 503
    body = {
        **report.to_dict(),
        "version": "0.1.0",
        "uptime_seconds": round(time.monotonic() - _STARTED_AT_MONO, 1),
    }
    return JSONResponse(status_code=status_code, content=body)


@app.get("/")
async def root():
    # Naive UTC + trailing "Z", matching the convention every other
    # timestamp in this service uses (see app/models/models.py and
    # app/api/licenses.py's _iso_z) rather than an aware isoformat()
    # with a +00:00 offset.
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    return {"service": "sentinel-license-service", "time": now.isoformat() + "Z"}
