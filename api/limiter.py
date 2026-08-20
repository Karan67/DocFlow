"""Shared rate limiter.

Lives in its own module so both `api.main` (which registers the handler) and
`api.routes` (which decorates the endpoint) can import it without a cycle.

Storage is Redis rather than in-process memory, so the limit is a property of
the service rather than of one replica - four API containers with an in-memory
limiter would let through four times the configured rate.
"""

from __future__ import annotations

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from core.config import settings

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """The address to rate limit on.

    `X-Forwarded-For` is appended to by each hop, and the client controls the
    left-hand entries. Trusting the leftmost value - the common mistake - lets
    anyone send `X-Forwarded-For: <random>` and get a fresh quota per request.

    So the header is only consulted when the deployment declares how many
    proxies it sits behind, and then only that many entries back from the
    right, which is the part those proxies actually wrote.
    """
    hops = settings.TRUSTED_PROXY_COUNT
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        entries = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(entries) >= hops:
            return entries[-hops]
        logger.warning(
            "X-Forwarded-For has %s entries but TRUSTED_PROXY_COUNT is %s; "
            "falling back to the socket address",
            len(entries),
            hops,
        )
    return get_remote_address(request)


limiter = Limiter(
    key_func=client_ip,
    storage_uri=settings.REDIS_URL,
    enabled=settings.RATE_LIMIT_ENABLED,
)
