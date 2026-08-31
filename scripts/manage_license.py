#!/usr/bin/env python3
"""Change status on an existing license (revoke / suspend / reactivate),
and look up a license by its raw key or id — for support/ops use and
for testing the fail-closed path end to end (see the Sentinel Command
plan doc's Phase 3 verification: "revoke the test key -> access pulled
within one 15-minute tick").

Usage:
    uv run python scripts/manage_license.py --key slk_... --show
    uv run python scripts/manage_license.py --key slk_... --revoke
    uv run python scripts/manage_license.py --id 1 --suspend
    uv run python scripts/manage_license.py --id 1 --reactivate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal  # noqa: E402
from app.core.keys import hash_key  # noqa: E402
from app.models.models import License  # noqa: E402


def _find(db, args) -> License | None:
    if args.key:
        return db.query(License).filter_by(key_hash=hash_key(args.key)).first()
    if args.id is not None:
        return db.query(License).filter_by(id=args.id).first()
    return None


def _print(license_row: License) -> None:
    print(
        f"id={license_row.id} tier={license_row.tier} status={license_row.status} "
        f"cap={license_row.monthly_run_cap}/mo key=...{license_row.key_last4} "
        f"customer={license_row.customer_label or license_row.customer_email or '(none)'} "
        f"issued_at={license_row.issued_at} renews_at={license_row.renews_at or 'never'} "
        f"last_seen_at={license_row.last_seen_at or 'never'} "
        f"last_seen_ip={license_row.last_seen_ip or '(none)'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--key", help="raw license key")
    target.add_argument("--id", type=int, help="license row id")

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--show", action="store_true")
    action.add_argument("--revoke", action="store_true")
    action.add_argument("--suspend", action="store_true")
    action.add_argument("--reactivate", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        license_row = _find(db, args)
        if license_row is None:
            print("No matching license found.", file=sys.stderr)
            return 1

        if args.revoke:
            license_row.status = "revoked"
        elif args.suspend:
            license_row.status = "suspended"
        elif args.reactivate:
            license_row.status = "active"
        # --show or no action: just print current state.

        if args.revoke or args.suspend or args.reactivate:
            db.add(license_row)
            db.commit()
            db.refresh(license_row)

        _print(license_row)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
