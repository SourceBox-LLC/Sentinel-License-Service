"""Lightweight schema sync for SQLite — copied from Sentinel Command
Center's backend/app/core/migrations.py (sync_schema/sync_indexes only;
that repo's one-shot orphan-table/codec-sanitize helpers are specific
to its own historical data problems and don't apply here).

Runs on every boot. Walks Base.metadata and ALTER TABLE ADD COLUMN for
any model field missing from the live SQLite table. Idempotent. Stand-in
for Alembic — the expected schema churn here (add a nullable column) is
exactly the case this handles; see Command Center's
docs/adr/0001-sync-schema-vs-alembic.md for the fuller rationale.

Caveats:
- SQLite can't add a NOT NULL column without a DEFAULT — such a column
  is silently downgraded to nullable with a logged warning.
- No renames, type changes, or drops. Write a real migration for those.
- ADD COLUMN doesn't create indexes; sync_indexes handles that separately.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import Column

logger = logging.getLogger(__name__)


def _compile_column_ddl(column: Column, dialect) -> str:
    col_type = column.type.compile(dialect=dialect)
    parts = [f'"{column.name}"', col_type]

    has_server_default = column.server_default is not None
    if not column.nullable and not has_server_default:
        logger.warning(
            "migrations: column %s.%s is NOT NULL with no server_default; "
            "adding as NULLABLE to keep existing rows valid",
            column.table.name,
            column.name,
        )
    elif not column.nullable:
        parts.append("NOT NULL")

    if has_server_default:
        default = column.server_default.arg
        default_sql = default.text if hasattr(default, "text") else str(default)
        parts.append(f"DEFAULT {default_sql}")

    return " ".join(parts)


def _table_columns(engine: Engine, table_name: str) -> set[str]:
    insp = inspect(engine)
    return {c["name"] for c in insp.get_columns(table_name)}


def _existing_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def ensure_schema(engine: Engine, metadata) -> None:
    """Full boot-time schema convergence: create any missing tables,
    then add any missing columns/indexes to tables that already exist.

    Single entry point for every process that touches this DB file —
    the server and every ops CLI — so the guarantee "schema is current"
    is structural rather than each caller having to remember to paste
    the same three calls in the same order.
    """
    metadata.create_all(bind=engine)
    sync_schema(engine, metadata)
    sync_indexes(engine, metadata)


def sync_schema(engine: Engine, metadata) -> list[str]:
    """Walk every table in `metadata` and add any columns missing from the DB."""
    changes: list[str] = []
    existing = _existing_tables(engine)
    dialect = engine.dialect

    for table in metadata.sorted_tables:
        if table.name not in existing:
            continue  # create_all() will have taken care of this one.

        db_cols = _table_columns(engine, table.name)
        missing: Iterable[Column] = [c for c in table.columns if c.name not in db_cols]
        if not missing:
            continue

        with engine.begin() as conn:
            for column in missing:
                ddl_fragment = _compile_column_ddl(column, dialect)
                stmt = f'ALTER TABLE "{table.name}" ADD COLUMN {ddl_fragment}'
                try:
                    conn.execute(text(stmt))
                    changes.append(f"{table.name}.{column.name}")
                    logger.info("migrations: added column %s.%s", table.name, column.name)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "migrations: failed to add %s.%s (%s): %s",
                        table.name, column.name, stmt, exc,
                    )

    if changes:
        logger.info("migrations: applied %d column additions: %s", len(changes), ", ".join(changes))
    else:
        logger.debug("migrations: schema already in sync")

    return changes


def sync_indexes(engine: Engine, metadata) -> list[str]:
    """Create any model-declared indexes missing from the live DB.

    create_all(checkfirst=True) skips tables that already exist —
    entirely, indexes included — and sync_schema only does ADD COLUMN.
    This walks declared indexes and issues CREATE INDEX IF NOT EXISTS
    for each — idempotent, WAL-friendly, no-op once in sync.
    """
    created: list[str] = []
    existing_tables = _existing_tables(engine)
    inspector = inspect(engine)

    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        try:
            db_indexes = {ix["name"] for ix in inspector.get_indexes(table.name)}
        except Exception:  # noqa: BLE001
            logger.exception("migrations: index inspect failed for %s", table.name)
            continue
        for index in table.indexes:
            if not index.name or index.name in db_indexes:
                continue
            cols = ", ".join(f'"{c.name}"' for c in index.columns)
            unique = "UNIQUE " if index.unique else ""
            stmt = f'CREATE {unique}INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({cols})'
            try:
                with engine.begin() as conn:
                    conn.execute(text(stmt))
                created.append(index.name)
                logger.info("migrations: created index %s on %s", index.name, table.name)
            except Exception:  # noqa: BLE001
                logger.exception("migrations: failed to create index %s", index.name)

    if created:
        logger.info("migrations: created %d missing index(es): %s", len(created), ", ".join(created))
    else:
        logger.debug("migrations: indexes already in sync")
    return created
