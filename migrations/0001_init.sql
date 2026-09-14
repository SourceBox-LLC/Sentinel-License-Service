-- Adopts the schema SQLAlchemy's create_all() already built in
-- production. Column types, lengths, nullability, defaults and index
-- names are carried over unchanged, and everything is IF NOT EXISTS so
-- the first Rust deploy is a no-op against the live database rather than
-- a failure or a rewrite.
CREATE TABLE IF NOT EXISTS licenses (
    id               SERIAL PRIMARY KEY,
    key_hash         VARCHAR(64)  NOT NULL UNIQUE,
    key_last4        VARCHAR(4)   NOT NULL,
    tier             VARCHAR(40)  NOT NULL DEFAULT 'self_host_standard',
    monthly_run_cap  INTEGER      NOT NULL DEFAULT 500,
    sync_enabled     BOOLEAN      NOT NULL DEFAULT false,
    status           VARCHAR(20)  NOT NULL DEFAULT 'active',
    customer_email   VARCHAR(255),
    customer_label   VARCHAR(255),
    issued_at        TIMESTAMP    NOT NULL,
    renews_at        TIMESTAMP,
    issued_by        VARCHAR(60)  NOT NULL DEFAULT 'manual',
    notes            TEXT,
    last_seen_at     TIMESTAMP,
    last_seen_ip     VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS ix_licenses_key_hash ON licenses (key_hash);
CREATE INDEX IF NOT EXISTS ix_licenses_status   ON licenses (status);

CREATE TABLE IF NOT EXISTS license_checkins (
    id            SERIAL PRIMARY KEY,
    key_hash      VARCHAR(64) NOT NULL,
    license_id    INTEGER REFERENCES licenses(id),
    checked_in_at TIMESTAMP   NOT NULL,
    source_ip     VARCHAR(45),
    install_id    VARCHAR(64),
    result        VARCHAR(20) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_license_checkins_key_hash      ON license_checkins (key_hash);
CREATE INDEX IF NOT EXISTS ix_license_checkins_license_id    ON license_checkins (license_id);
CREATE INDEX IF NOT EXISTS ix_license_checkins_checked_in_at ON license_checkins (checked_in_at);
CREATE INDEX IF NOT EXISTS ix_license_checkins_license_checked_at
    ON license_checkins (license_id, checked_in_at);
