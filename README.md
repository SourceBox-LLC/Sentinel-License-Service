# Sentinel License Service

Validates license keys for self-hosted Sentinel Command Center installs (`AUTH_PROVIDER=local`). This is the mechanism that lets a self-hosted operator pay to unlock **Sentinel AI** — the one feature with a real ongoing LLM cost regardless of who runs the dashboard. Every other self-host feature (cameras, recording, motion, MCP) stays free and unaffected by this service entirely.

A genuinely separate service from Command Center — its own codebase, its own deploy target, its own SQLite database. Self-hosted operators' copy of Command Center never contains this service's code.

## Run locally

```bash
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

`GET /health` — pure liveness. `GET /health/ready` — 503 if a critical dependency (the database) is down.

## Tests

```bash
uv run pytest
```

## Deploy

Single-stage `Dockerfile` (no frontend build — this service has no UI), `fly.toml` targets a much smaller VM than Command Center's (no video workload, tiny check-in traffic). First deploy is manual (`fly deploy`) by design — see `.github/workflows/test.yml`'s comment for why deploy automation is deferred.

## Status

Phase 1 of the plan (standalone service, verified via curl/pytest) is complete. Command Center-side integration (the background check-in loop and the actual Sentinel-AI gate) is tracked separately in the Sentinel Command repo.
