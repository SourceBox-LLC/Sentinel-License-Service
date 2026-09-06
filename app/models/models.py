"""Data model for the Sentinel License Service.

Real columns, not a generic KV table (unlike Command Center's
`Setting` table) — license state needs indexed queries ("expiring
soon," "revoked") that a KV shape handles badly. Conventions mirror
Command Center's models.py: naive UTC datetimes (tzinfo stripped after
capture — SQLite has no native timezone-aware column type, and mixing
aware/naive within one column is a classic source of comparison bugs),
integer autoincrement PKs.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(tz=UTC).replace(tzinfo=None)


# Not a DB-level enum/CheckConstraint (SQLite migration churn isn't worth
# it for a single string column) — but every writer of License.status
# should import these instead of retyping the literals, so a typo is a
# NameError at write time instead of a silently-persisted bad value that
# fails a paying customer closed for the wrong reason.
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_REVOKED = "revoked"


class License(Base):
    __tablename__ = "licenses"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    key_last4 = Column(String(4), nullable=False)

    # Namespaced separately from Command Center's own plan slugs
    # (pro/pro_plus/self_host) to avoid confusing the two systems'
    # vocabularies — this is what a *license* entitles, not a Clerk plan.
    tier = Column(String(40), nullable=False, default="self_host_standard")
    monthly_run_cap = Column(Integer, nullable=False, default=500)

    # Separate opt-in entitlement, not folded into `tier` — sync is a
    # distinct product (Command Center's local-DB-to-cloud-Postgres
    # mirror, see the Sentinel-Sync-Service plan) that a self-host
    # customer may or may not have bought regardless of their AI tier.
    sync_enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # STATUS_ACTIVE | STATUS_SUSPENDED | STATUS_REVOKED (see above)
    status = Column(String(20), nullable=False, default=STATUS_ACTIVE, index=True)

    customer_email = Column(String(255), nullable=True)
    customer_label = Column(String(255), nullable=True)

    issued_at = Column(DateTime, nullable=False, default=_utcnow)
    # Null = perpetual. Avoid unless a lifetime key is genuinely intended —
    # see issue_license.py, which requires --renews-days unless --perpetual
    # is passed explicitly.
    renews_at = Column(DateTime, nullable=True)
    issued_by = Column(String(60), nullable=False, default="manual")
    notes = Column(Text, nullable=True)

    last_seen_at = Column(DateTime, nullable=True)
    last_seen_ip = Column(String(45), nullable=True)


class LicenseCheckIn(Base):
    """Append-only check-in log — abuse visibility (repeated check-ins
    from many distinct IPs/install_ids for one key, or a flood of
    not_found attempts against a range of hashes). Not a substitute for
    real fraud detection; just enough signal to notice something worth
    a human look.

    key_hash is always populated (even when no License row matches) so
    a not_found attempt is still visible — license_id is nullable and
    only set when the hash matched a real row.
    """

    __tablename__ = "license_checkins"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), nullable=False, index=True)
    license_id = Column(Integer, ForeignKey("licenses.id"), nullable=True, index=True)
    checked_in_at = Column(DateTime, nullable=False, default=_utcnow, index=True)
    source_ip = Column(String(45), nullable=True)
    install_id = Column(String(64), nullable=True)

    # ok | suspended | revoked | expired | not_found — never rate_limited:
    # slowapi's @limiter.limit rejects an over-cap request before
    # check_in()'s body (and thus this insert) ever runs.
    result = Column(String(20), nullable=False)

    __table_args__ = (
        Index("ix_license_checkins_license_checked_at", "license_id", "checked_in_at"),
    )
