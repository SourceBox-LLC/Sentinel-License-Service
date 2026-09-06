"""Shared test fixtures. In-memory SQLite + a FastAPI test client,
mirroring Sentinel Command Center's own tests/conftest.py pattern.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

# Must set env vars BEFORE importing app modules so config.py picks them up.
# Dialect-parametrised: the deployed service runs Postgres, local runs
# SQLite. Both must stay green. Set TEST_DATABASE_URL to point the suite
# at Postgres; the SQLite default keeps a bare `uv run pytest` working
# with no database to set up.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "sqlite:///:memory:"

from app.core.database import Base, engine, get_db
from app.core.limiter import limiter
from app.main import app

TestSession = sessionmaker(bind=engine)


def _override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate all tables between tests — StaticPool means every test
    shares the same in-memory DB connection. Also reset the rate
    limiter's in-memory storage — it's a module-level singleton keyed
    by client IP, and TestClient uses a fixed synthetic host
    ("testclient") for every test, so without a reset one test's
    requests count against the next test's rate-limit budget."""
    limiter.reset()
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
