"""Rate limiter — per source IP. No tenant/org concept here (unlike
Command Center's limiter.py, which buckets by org/node) since a
check-in caller is identified only by the license key it presents, and
bucketing by IP is what actually blunts hash-lookup brute-forcing.
"""

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

logger = logging.getLogger(__name__)


def _build_limiter() -> Limiter:
    kwargs: dict = {"key_func": get_remote_address}
    if settings.REDIS_URL:
        kwargs["storage_uri"] = settings.REDIS_URL
        logger.info("[Limiter] Using Redis storage for rate limits")
    else:
        logger.warning(
            "[Limiter] REDIS_URL not set — in-memory rate limiting only. "
            "Fine for this service's single-machine deploy."
        )
    return Limiter(**kwargs)


limiter = _build_limiter()
