# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------
FROM rust:1.98-slim-bookworm AS builder

WORKDIR /build

# Dependency layer first: Cargo.toml/lock change far less often than
# src/, so a source-only edit reuses the compiled dependency graph.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs \
 && cargo build --release --locked \
 && rm -rf src

COPY src ./src
COPY migrations ./migrations
# Cargo caches on mtime; the stub main.rs above means the real one can
# look "already built" without this.
RUN touch src/main.rs && cargo build --release --locked

# ---------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------
FROM debian:bookworm-slim

# postgresql-client is NOT optional here, unlike the sync service.
# .github/workflows/backup.yml runs `flyctl ssh console -C "bash
# /app/scripts/backup_db.sh"` nightly, and that script needs pg_dump and
# psql on PATH. Dropping them would leave a green deploy and a backup job
# that fails at 09:47 UTC — the failure mode this repo has already had
# once, when scale-to-zero broke the same job.
#
# ca-certificates for outbound TLS; bash because backup_db.sh is bash,
# not sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      postgresql-client \
      bash \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged. The Python image ran as root; nothing here needs it.
# Named `licensesvc` rather than `license` for symmetry with the DB role.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin licensesvc

WORKDIR /app

COPY --from=builder /build/target/release/sentinel-license-service /usr/local/bin/sentinel-license-service
# Kept at /app/scripts because backup.yml hard-codes that path.
COPY scripts ./scripts
RUN chmod +x ./scripts/*.sh

# /data is the mounted volume backup_db.sh writes dumps into.
RUN mkdir -p /data && chown licensesvc:licensesvc /data /app
USER licensesvc

EXPOSE 8000
CMD ["/usr/local/bin/sentinel-license-service"]
