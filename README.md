# Sentinel License Service

Validates license keys for self-hosted Sentinel Command Center installs (`AUTH_PROVIDER=local`). This is the mechanism that lets a self-hosted operator pay to unlock **Sentinel AI** — the one feature with a real ongoing LLM cost regardless of who runs the dashboard. Every other self-host feature (cameras, recording, motion, MCP) stays free and unaffected by this service entirely.

A genuinely separate service from Command Center — its own codebase, its own deploy target, its own database. Self-hosted operators' copy of Command Center never contains this service's code.

The database is Postgres in production, on its own dedicated cluster `sentinel-postgres` (migrated from SQLite 2026-09-07). It falls back to SQLite when `DATABASE_URL` is unset, so a local run needs no database to set up — `app/core/database.py` branches on the URL scheme and CI runs the suite against both.

## Run locally

```bash
cp .env.example .env   # defaults work as-is for local dev
uv sync --extra dev
uv run uvicorn app.main:app --reload
```

## Issue a license (v1: manual/CLI only, no self-serve checkout yet)

```bash
uv run python scripts/issue_license.py --email customer@example.com --label "Jane Doe — invoice #123" --renews-days 365
```

Prints the raw key exactly once — it is never stored or logged in plaintext, only its SHA-256 hash.

## Manage an existing license

```bash
uv run python scripts/manage_license.py --key slk_... --show
uv run python scripts/manage_license.py --key slk_... --revoke
uv run python scripts/manage_license.py --id 1 --suspend
uv run python scripts/manage_license.py --id 1 --reactivate
uv run python scripts/manage_license.py --id 1 --enable-sync
uv run python scripts/manage_license.py --id 1 --disable-sync
```

## API

`POST /v1/licenses/check-in` — `Authorization: Bearer slk_<key>`. Always returns HTTP 200 when the service itself is healthy, with `valid: bool` in the body — this is deliberate: it's how a caller tells "the service said no" (revoked/expired/suspended/unknown key — apply immediately) apart from "I couldn't reach it" (a real HTTP error — the caller should apply a grace period instead). See `app/api/licenses.py` for the full contract.

`GET /v1/licenses/entitlements` — `Authorization: Bearer slk_<key>`. Read-only entitlement lookup, deliberately separate from `/check-in`: it doesn't touch `last_seen_at` or write a check-in audit row, so a caller validating on every request (e.g. [Sentinel-Sync-Service](https://github.com/SourceBox-LLC/Sentinel-Sync-Service), checking every push) doesn't pollute that audit trail or fight Command Center's own check-in loop for rate-limit headroom. Returns `sync_enabled` — the cloud data-sync entitlement, a separate opt-in on the same license, independent of Sentinel-AI validity.

`GET /health` — pure liveness. `GET /health/ready` — 503 if a critical dependency (database or disk) is down.

## Tests

```bash
uv run pytest
```

## Deploy

Single-stage `Dockerfile` (no frontend build — this service has no UI). `fly.toml` targets a much smaller VM than Command Center's: 256 MB, no video workload, tiny check-in traffic.

**Deploys from CI.** Every push to `master` runs the tests against both SQLite and Postgres, then `flyctl deploy`. Deploy automation was deferred while this was new infrastructure; that turned out worse than what it avoided, because `fly.toml` became a file that did nothing — a scale-to-zero change merged with CI fully green on 2026-09-09 and never reached Fly, and it *looked* applied because the commit was on master. Config that silently doesn't apply is more dangerous than no config.

Two flags, each for a reason: `--strategy immediate` because this app mounts `sentinel_license_data` and the default rolling strategy errors on the volume's single attachment slot; `--ha=false` because Fly otherwise provisions two machines, which one volume can't serve anyway.

**Scales to zero.** Self-hosted installs check in on a ~15-minute background tick, so this is idle by default. Safe because boot is ~4s — inside the ~8s Fly's proxy waits for an auto-started machine to bind — the caller's timeout is 10s, and a missed check-in is a *designed* path: Command Center treats network/5xx as "unreachable" and applies a 72-hour grace window. This flips if a licence check ever moves onto a user-blocking path; then a 4s cold start becomes a 4s page stall.

## Status

Deployed and live at `https://sentinel-license.fly.dev`. All three phases of the plan are complete: the standalone service (verified via curl/pytest), Command Center-side integration (the background check-in loop and health probe, gated behind `AUTH_PROVIDER=local` + `SENTINEL_LICENSE_KEY`), and the actual Sentinel-AI gate (enforced at every dispatch/API/MCP call site via `sentinel_blocked_by_license()`). Deferred: Stripe checkout automation — v1 issuance is manual/CLI-only (see above).
