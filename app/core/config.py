import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./licenses.db")

    # Sentry error tracking. Blank in local dev/tests — init_sentry()
    # no-ops gracefully. Same module as Command Center's, copied
    # verbatim (app/core/sentry.py).
    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")
    SENTRY_TRACES_SAMPLE_RATE: float = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))

    # Rate-limiter shared storage. Without it, slowapi falls back to
    # in-memory counters — fine for a single-machine deploy (this
    # service's whole topology for the foreseeable future), broken
    # across multiple machines. Set REDIS_URL if that ever changes.
    REDIS_URL: str = os.getenv("REDIS_URL", "")


settings = Config()
