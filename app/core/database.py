"""SQLAlchemy setup — mirrors Sentinel Command Center's
backend/app/core/database.py, including its dialect branching. Nothing
here is specific to either service; it's generic SQLAlchemy boilerplate.

Runs on Postgres when deployed and SQLite when someone runs it locally
with the default DATABASE_URL, so everything below picks a lane. The
SQLite tuning is not merely unnecessary on Postgres — it's invalid:
`PRAGMA` is a syntax error, and `check_same_thread` / `timeout` are
pysqlite connect kwargs psycopg rejects.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

# In-memory SQLite needs StaticPool (a shared single connection) so all
# operations see the same database — used by tests.
_is_memory_db = _is_sqlite and ":memory:" in settings.DATABASE_URL

if _is_sqlite:
    # NullPool: each request gets a fresh connection and releases it
    # immediately — the recommended approach for SQLite under concurrent
    # requests.
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
            **({} if _is_memory_db else {"timeout": 30}),
        },
        poolclass=StaticPool if _is_memory_db else NullPool,
    )
else:
    # Postgres keeps the default QueuePool — NullPool would mean a fresh
    # TCP + TLS + auth round trip per request now that the database is
    # across a network. pool_pre_ping turns a provider-recycled idle
    # connection into a transparent reconnect rather than a failed request.
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)


# Registered conditionally, not early-returning inside the handler: on
# Postgres this must never fire, since PRAGMA would fail every connect.
if _is_sqlite:

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Matches connect_args' timeout=30 above — pysqlite's own busy-wait
        # is set at connect time, but this PRAGMA runs right after and would
        # otherwise silently override it down to a much shorter wait.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
