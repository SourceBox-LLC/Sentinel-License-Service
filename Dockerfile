# Single-stage build — this service ships no frontend/UI, so there's no
# equivalent of Command Center's Node build stage to run first.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# sqlite3 kept for backup/restore parity with Command Center's own
# operational scripts (PRAGMA integrity_check, online .backup).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--forwarded-allow-ips=*", "--no-access-log"]
