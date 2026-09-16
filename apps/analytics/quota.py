"""The analytics sync's quota circuit breaker.

When a platform says "you have spent your budget", the only useful response is
to stop calling until the budget returns. Without that, every subsequent call in
the run — and every call in the next hourly run — is a guaranteed failure that
still costs a request, still logs a warning, and still leaves the post looking
un-synced. The breaker turns one platform answer into a decision the whole sync
respects.

The state lives in the database rather than the cache on purpose: ``REDIS_URL``
is optional (``config/settings/base.py``), so the fallback cache is per-process
LocMemCache, which is emptied by every deploy and every dyno restart — precisely
the moments a block most needs to survive.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)

# Stand-in key for a platform whose credentials we could not resolve. Grouping
# those together is right: they all failed the same way, and none of them can be
# told apart by project.
_UNKNOWN_CREDENTIAL = "unknown"


def credential_key(credentials: dict | None) -> str:
    """Stable, non-secret identifier for the app credentials behind a call.

    Two accounts that resolve to the same OAuth client are drawing on the same
    upstream quota pool, so they must share a breaker row. Hashing gives that
    equivalence without putting a client_id in the database or the logs.
    """
    client_id = (credentials or {}).get("client_id") or ""
    if not client_id:
        return _UNKNOWN_CREDENTIAL
    return hashlib.sha256(str(client_id).encode()).hexdigest()[:16]


def quota_blocked_until(
    platform: str,
    key: str,
    scope: str = "",
    *,
    cache: dict | None = None,
) -> datetime | None:
    """When this credential's quota comes back, or ``None`` if it is not blocked.

    ``cache`` is an optional per-run dict. A sync pass asks this once per
    account, and accounts overwhelmingly share one credential, so without it the
    breaker would cost a query per account to answer the same question.
    """
    cache_key = (platform, key, scope)
    if cache is not None and cache_key in cache:
        blocked_until = cache[cache_key]
    else:
        from .models import ProviderQuotaBlock

        blocked_until = (
            ProviderQuotaBlock.objects.filter(platform=platform, credential_key=key, quota_scope=scope)
            .values_list("blocked_until", flat=True)
            .first()
        )
        if cache is not None:
            cache[cache_key] = blocked_until

    if blocked_until is None or blocked_until <= timezone.now():
        return None
    return blocked_until


def trip_quota_block(
    platform: str,
    key: str,
    scope: str = "",
    *,
    until: datetime,
    reason: str = "",
    cache: dict | None = None,
) -> None:
    """Record that this credential's quota is spent until ``until``.

    Idempotent by ``(platform, credential_key, quota_scope)``: a later failure
    simply moves the expiry, so a throttle that arrives after a daily
    exhaustion cannot shorten the longer block.
    """
    from .models import ProviderQuotaBlock

    existing = (
        ProviderQuotaBlock.objects.filter(platform=platform, credential_key=key, quota_scope=scope)
        .values_list("blocked_until", flat=True)
        .first()
    )
    if existing is not None and existing > until:
        until = existing

    ProviderQuotaBlock.objects.update_or_create(
        platform=platform,
        credential_key=key,
        quota_scope=scope,
        defaults={"blocked_until": until, "reason": reason[:500]},
    )
    if cache is not None:
        cache[(platform, key, scope)] = until
    logger.error(
        "analytics: %s quota block tripped for credential %s (scope=%r) until %s — %s",
        platform,
        key,
        scope,
        until.isoformat(),
        reason[:200],
    )
