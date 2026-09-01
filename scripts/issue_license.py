#!/usr/bin/env python3
"""Issue a new self-hosted Sentinel AI license key.

V1 has no HTTP admin API by design — issuance is a manual, operator-run
action against this service's own SQLite file (the same way Command
Center's backup_db.sh/restore_db.sh are already operated: `fly ssh
console -a sentinel-license -C "python scripts/issue_license.py ..."`).

Usage (from the repo root):
    uv run python scripts/issue_license.py --email customer@example.com \\
        --label "Jane Doe — invoice #123" --renews-days 365

Prints the raw key exactly once. It is never stored or logged in
plaintext anywhere — only its SHA-256 hash is persisted.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Run as `uv run python scripts/issue_license.py` from the repo root —
# the script's own directory (scripts/) isn't on sys.path by default,
# so `app` isn't importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.keys import generate_key, hash_key  # noqa: E402
from app.core.migrations import ensure_schema  # noqa: E402
from app.models.models import License  # noqa: E402,F401 (registers the table on Base.metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="self_host_standard")
    parser.add_argument("--cap", type=int, default=500, help="monthly_run_cap")
    parser.add_argument("--email", default=None, help="customer_email (internal reference only)")
    parser.add_argument("--label", default=None, help="customer_label (internal reference only)")
    parser.add_argument("--notes", default=None)
    parser.add_argument(
        "--renews-days", type=int, default=365,
        help="License is valid this many days from now (default 365).",
    )
    parser.add_argument(
        "--perpetual", action="store_true",
        help="Never expires. Avoid unless a lifetime key is genuinely intended — "
        "overrides --renews-days.",
    )
    parser.add_argument(
        "--issued-by", default=None,
        help="Defaults to the current OS username, prefixed 'manual:'.",
    )
    args = parser.parse_args()

    if args.cap <= 0:
        print("--cap must be positive.", file=sys.stderr)
        return 1

    if not args.perpetual and args.renews_days <= 0:
        print("--renews-days must be positive (or pass --perpetual).", file=sys.stderr)
        return 1

    raw_key = generate_key()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    renews_at = None if args.perpetual else now + timedelta(days=args.renews_days)
    issued_by = args.issued_by or f"manual:{getpass.getuser()}"

    # Safety net for a DB file that's never had the server run against
    # it yet, or was created by an older server build — same
    # ensure_schema() app/main.py runs at boot, so this CLI never
    # operates against a schema the web process would consider stale.
    # Idempotent, cheap given this service's tiny schema.
    ensure_schema(engine, Base.metadata)

    db = SessionLocal()
    try:
        license_row = License(
            key_hash=hash_key(raw_key),
            key_last4=raw_key[-4:],
            tier=args.tier,
            monthly_run_cap=args.cap,
            status="active",
            customer_email=args.email,
            customer_label=args.label,
            issued_at=now,
            renews_at=renews_at,
            issued_by=issued_by,
            notes=args.notes,
        )
        db.add(license_row)
        db.commit()
        db.refresh(license_row)
    finally:
        db.close()

    print(f"\nLicense issued (id={license_row.id}, tier={args.tier}, cap={args.cap}/mo)")
    print(f"Expires: {renews_at.isoformat() + 'Z' if renews_at else 'never (perpetual)'}")
    print("\nRaw key (shown once — save it now, it cannot be recovered):")
    print(raw_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
