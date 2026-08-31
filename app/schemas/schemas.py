from typing import Optional

from pydantic import BaseModel


class CheckInRequest(BaseModel):
    install_id: Optional[str] = None
    product: Optional[str] = None
    client_version: Optional[str] = None


class CheckInResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None
    tier: Optional[str] = None
    status: Optional[str] = None
    monthly_run_cap: Optional[int] = None
    renews_at: Optional[str] = None
    server_time: str
