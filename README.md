# Sentinel License Service

Validates license keys for self-hosted Sentinel Command Center installs (`AUTH_PROVIDER=local`). This is the mechanism that lets a self-hosted operator pay to unlock **Sentinel AI** — the one feature with a real ongoing LLM cost regardless of who runs the dashboard. Every other self-host feature (cameras, recording, motion, MCP) stays free and unaffected by this service entirely.

A genuinely separate service from Command Center — its own codebase, its own deploy target, its own SQLite database. Self-hosted operators' copy of Command Center never contains this service's code.

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
```

## API

`POST /v1/licenses/check-in` — `Authorization: Bearer slk_<key>`. Always returns HTTP 200 when the service itself is healthy, with `valid: bool` in the body — this is deliberate: it's how a caller tells "the service said no" (revoked/expired/suspended/unknown key — apply immediately) apart from "I couldn't reach it" (a real HTTP error — the caller should apply a grace period instead). See `app/api/licenses.py` for the full contract.

`GET /health` — pure liveness. `GET /health/ready` — 503 if a critical dependency (database or disk) is down.

## Tests

```bash
uv run pytest
```

## Deploy

Single-stage `Dockerfile` (no frontend build — this service has no UI), `fly.toml` targets a much smaller VM than Command Center's (no video workload, tiny check-in traffic). First deploy is manual (`fly deploy`) by design — see `.github/workflows/test.yml`'s comment for why deploy automation is deferred.

## Status

Deployed and live at `https://sentinel-license.fly.dev`. All three phases of the plan are complete: the standalone service (verified via curl/pytest), Command Center-side integration (the background check-in loop and health probe, gated behind `AUTH_PROVIDER=local` + `SENTINEL_LICENSE_KEY`), and the actual Sentinel-AI gate (enforced at every dispatch/API/MCP call site via `sentinel_blocked_by_license()`). Deferred: Stripe checkout automation — v1 issuance is manual/CLI-only (see above).
