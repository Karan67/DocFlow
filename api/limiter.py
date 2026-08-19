"""Shared rate limiter.

Lives in its own module so both `api.main` (which registers the handler) and
`api.routes` (which decorates the endpoint) can import it without a cycle.

Storage is Redis rather than in-process memory, so the limit is a property of
the service rather than of one replica - four API containers with an in-memory
limiter would let through four times the configured rate.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from core.config import settings

limiter = Limiter(
    # Behind a load balancer this sees the proxy's address, not the client's.
    # Phase 6 needs a trusted-proxy config before this means anything in
    # production - honouring X-Forwarded-For without one is trivially spoofed.
    key_func=get_remote_address,
    storage_uri=settings.REDIS_URL,
    enabled=settings.RATE_LIMIT_ENABLED,
)
