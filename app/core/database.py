"""SQLite/SQLAlchemy setup — copied near-verbatim from Sentinel Command
Center's backend/app/core/database.py (see that repo's plan doc for
which reusable modules this service borrows). Nothing here is specific
to Command Center; it's generic SQLAlchemy+SQLite boilerplate.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import settings

# NullPool: each request gets a fresh connection and releases it
# immediately — the recommended approach for SQLite under concurrent
# requests. In-memory SQLite needs StaticPool (a shared single
# connection) so all operations see the same database — used by tests.
_is_memory_db = settings.DATABASE_URL == "sqlite:///:memory:" or ":memory:" in settings.DATABASE_URL

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, **({} if _is_memory_db else {"timeout": 30})},
    poolclass=StaticPool if _is_memory_db else NullPool,
)


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
