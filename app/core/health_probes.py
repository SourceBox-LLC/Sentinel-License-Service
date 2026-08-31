"""Dependency probes — trimmed from Sentinel Command Center's
backend/app/core/health_probes.py to just the two probes that apply
here: database and disk. No Clerk (this service has no Clerk
dependency at all) and no email worker (this service sends no email).

Same probe contract: a ProbeResult with status in
ok/warn/critical, never raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from app.core.database import SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, **self.data}

    @property
    def is_critical(self) -> bool:
        return self.status == "critical"


def probe_database() -> ProbeResult:
    """SELECT 1 round-trip. A failure here is the most pager-worthy
    signal in this service — every check-in reads the DB."""
    try:
        db = SessionLocal()
        try:
            t0 = time.perf_counter()
            db.execute(text("SELECT 1"))
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return ProbeResult(status="ok", data={"latency_ms": latency_ms})
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[Health] DB ping failed", exc_info=True)
        return ProbeResult(status="critical", data={"error_class": type(exc).__name__})


DISK_CRITICAL_PCT = 95.0
DISK_WARN_PCT = 80.0


def probe_disk() -> ProbeResult:
    """Check the SQLite volume usage. /data in production (Fly volume
    mount), current directory in dev."""
    disk_path = "/data" if os.path.isdir("/data") else "."
    try:
        usage = shutil.disk_usage(disk_path)
        pct = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
        if pct >= DISK_CRITICAL_PCT:
            status = "critical"
        elif pct >= DISK_WARN_PCT:
            status = "warn"
        else:
            status = "ok"
        return ProbeResult(
            status=status,
            data={
                "path": disk_path,
                "bytes_used": usage.used,
                "bytes_free": usage.free,
                "bytes_total": usage.total,
                "percent_used": pct,
            },
        )
    except OSError as exc:
        logger.warning("[Health] disk_usage(%s) failed", disk_path, exc_info=True)
        return ProbeResult(status="critical", data={"path": disk_path, "error_class": type(exc).__name__})


@dataclass
class ReadinessReport:
    ready: bool
    probes: dict[str, ProbeResult]

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": {name: p.to_dict() for name, p in self.probes.items()}}


def run_readiness_probes() -> ReadinessReport:
    probes = {"database": probe_database(), "disk": probe_disk()}
    ready = not any(p.is_critical for p in probes.values())
    return ReadinessReport(ready=ready, probes=probes)
