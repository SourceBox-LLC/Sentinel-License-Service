# Single-stage build — this service ships no frontend/UI, so there's no
# equivalent of Command Center's Node build stage to run first.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# postgresql-client for backup/restore parity with Command Center's own
# operational scripts (pg_dump / pg_restore / psql).
#
# Version 18 from PGDG, not Debian's 15: pg_dump refuses to dump a server
# with a newer major version than itself, and this service's database
# lives on the same 18.x cluster as Command Center's. See that repo's
# Dockerfile for the full note. Bump when the cluster major moves.
#
# (sqlite3 was here until 2026-09, when this service moved off SQLite.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
         postgresql-client-18 \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--forwarded-allow-ips=*", "--no-access-log"]
