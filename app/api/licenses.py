"""License check-in endpoint.

Contract (see the Sentinel Command Center plan doc for the full
rationale): this endpoint returns HTTP 200 whenever it can actually
determine the key's validity, with `valid: bool` in the body —
including for a key that's revoked, expired, suspended, or simply
never issued. That's deliberate: it's the only way a caller can
distinguish "the service answered and said no" (apply immediately, no
grace) from "I couldn't reach it" (a real HTTP error — apply grace).
Never blur that line by returning a 4xx for anything the service can
actually answer.

Real HTTP errors cover every case the service genuinely can't answer:
a malformed Authorization header (no key was even presented), a rate
limit trip, request validation failures, and any unexpected server
error (e.g. the database is unreachable) — a caller that can't get a
trustworthy verdict must never be told `valid: false` as if that were
one, so these fall through to FastAPI's normal error handling and are
treated as "unreachable" by callers, same as a timeout.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.keys import hash_key
from app.core.limiter import limiter
from app.models.models import License, LicenseCheckIn
from app.schemas.schemas import CheckInRequest, CheckInResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/licenses", tags=["licenses"])


def _extract_raw_key(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return parts[1].strip()


def _iso_z(dt: datetime) -> str:
    return dt.isoformat() + "Z"


def _log_checkin(
    db: Session,
    *,
    key_hash: str,
    license_id: int | None,
    source_ip: str | None,
    install_id: str | None,
    result: str,
) -> None:
    db.add(
        LicenseCheckIn(
            key_hash=key_hash,
            license_id=license_id,
            source_ip=source_ip,
            install_id=install_id,
            result=result,
        )
    )
    db.commit()


@router.post("/check-in", response_model=CheckInResponse)
@limiter.limit("20/minute")
def check_in(
    request: Request,
    payload: CheckInRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> CheckInResponse:
    # Plain `def`, not `async def`: the body below is entirely blocking
    # SQLAlchemy I/O. FastAPI runs sync path functions in a threadpool
    # automatically — an `async def` doing sync DB calls would instead
    # run them straight on the single event-loop thread (this service
    # runs `--workers 1`), so one slow write could stall every other
    # in-flight request, including Fly's own health-check probe.
    raw_key = _extract_raw_key(authorization)
    key_hash = hash_key(raw_key)
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    source_ip = request.client.host if request.client else None

    license_row = db.query(License).filter_by(key_hash=key_hash).first()

    if license_row is None:
        _log_checkin(
            db, key_hash=key_hash, license_id=None, source_ip=source_ip,
            install_id=payload.install_id, result="not_found",
        )
        return CheckInResponse(valid=False, reason="not_found", server_time=_iso_z(now))

    # Allow-list, not deny-list: any status other than "active" (a typo,
    # data corruption, or a future status value nothing here recognizes
    # yet) must fail closed, not silently pass through as valid.
    if license_row.status != "active":
        result = license_row.status
    elif license_row.renews_at is not None and license_row.renews_at < now:
        result = "expired"
    else:
        result = "ok"

    # Update last-seen regardless of outcome — even a revoked key still
    # checking in is a useful signal (an install that hasn't noticed yet).
    license_row.last_seen_at = now
    license_row.last_seen_ip = source_ip
    db.add(license_row)
    db.commit()

    _log_checkin(
        db, key_hash=key_hash, license_id=license_row.id, source_ip=source_ip,
        install_id=payload.install_id, result=result,
    )

    if result != "ok":
        return CheckInResponse(valid=False, reason=result, server_time=_iso_z(now))

    return CheckInResponse(
        valid=True,
        reason=None,
        tier=license_row.tier,
        status=license_row.status,
        monthly_run_cap=license_row.monthly_run_cap,
        renews_at=_iso_z(license_row.renews_at) if license_row.renews_at else None,
        server_time=_iso_z(now),
    )
