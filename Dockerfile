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
# psql on PATH.
#
# AND THE MAJOR VERSION MUST MATCH THE SERVER. Debian bookworm ships
# postgresql-client 15; the cluster is 18. pg_dump refuses to dump a
# newer server outright:
#
#   pg_dump: error: aborting because of server version mismatch
#   detail: server version: 18.6; pg_dump version: 15.19
#
# So this pulls client 18 from PGDG, exactly as the Python image did —
# that Dockerfile went to the same trouble and the port initially dropped
# it, which produced a green deploy and a broken backup. gnupg is purged
# after adding the key so it doesn't linger in the runtime image.
#
# ca-certificates for outbound TLS; bash because backup_db.sh is bash,
# not sh.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg bash \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-18 \
 && apt-get purge -y gnupg && apt-get autoremove -y \
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
