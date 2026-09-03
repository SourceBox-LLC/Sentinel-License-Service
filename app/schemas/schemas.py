from typing import Optional

from pydantic import BaseModel, Field


class CheckInRequest(BaseModel):
    # Bounded to the column widths these values are stored in
    # (app/models/models.py) — SQLite doesn't enforce VARCHAR(N) at the
    # engine level, so without a validation-layer cap a caller could
    # otherwise store an unbounded string in every check-in row.
    install_id: Optional[str] = Field(default=None, max_length=64)
    product: Optional[str] = Field(default=None, max_length=64)
    client_version: Optional[str] = Field(default=None, max_length=32)


class CheckInResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None
    tier: Optional[str] = None
    status: Optional[str] = None
    monthly_run_cap: Optional[int] = None
    sync_enabled: bool = False
    renews_at: Optional[str] = None
    server_time: str


class EntitlementsResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None
    # Stable, server-derived tenant identifier for a validated key — NOT
    # the caller-asserted `install_id` from CheckInRequest, which a
    # client can set to anything and so can't be trusted as a tenant
    # boundary. key_hash is already unique/immutable per License row,
    # so callers needing a stable per-license partition key (e.g.
    # Sentinel-Sync-Service scoping synced rows) use this instead.
    license_key_hash: Optional[str] = None
    tier: Optional[str] = None
    monthly_run_cap: Optional[int] = None
    sync_enabled: bool = False
    server_time: str
