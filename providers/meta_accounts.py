"""Shared ``/me/accounts`` enumeration for the Facebook-hosted Meta providers.

``FacebookProvider`` and ``InstagramProvider`` both read the same Graph edge —
one for the Pages themselves, one for the Instagram Business accounts linked to
them — so the cursor walking, the time budget and the CREATE_CONTENT rule live
here rather than being copied into each.

``InstagramLoginProvider`` drives Instagram's own API and deliberately uses none
of this.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .exceptions import APIError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .base import SocialProvider

logger = logging.getLogger(__name__)

# /me/accounts is paginated. A single response is commonly enough for an
# individual creator, but agencies and Business Manager users can administer
# hundreds of Pages, so we follow cursors rather than silently showing only the
# first response.
META_ACCOUNTS_PAGE_SIZE = 100
META_ACCOUNTS_MAX_PAGES = 100

# This runs inside the OAuth callback the user is waiting on, and each request
# carries the provider's full REQUEST_TIMEOUT. Without a wall-clock budget a
# slow Graph turns "you have a lot of Pages" into a platform request timeout
# with nothing connected and no partial progress saved. Stop early and work
# with what we have instead — well under the 30s most PaaS routers allow.
META_ACCOUNTS_MAX_SECONDS = 20.0


def fetch_me_accounts(
    provider: SocialProvider,
    *,
    access_token: str,
    base_url: str,
    fields: str,
    error_message: str,
    max_seconds: float = META_ACCOUNTS_MAX_SECONDS,
) -> list[dict]:
    """Walk every page of ``/me/accounts`` and return the raw entries.

    Stops early — and says so — when the cursor chain ends, when the page cap is
    reached, or when the time budget runs out. Truncation is always logged: the
    whole point of following cursors is that a caller should never silently see
    a partial list, and a cap that quietly hides the remainder would put that
    bug back at a higher bound.
    """
    raw_pages: list[dict] = []
    after: str | None = None
    seen_cursors: set[str] = set()
    started = time.monotonic()
    truncated_reason: str | None = None

    for attempt in range(META_ACCOUNTS_MAX_PAGES):
        if attempt and time.monotonic() - started > max_seconds:
            truncated_reason = f"time budget of {max_seconds:.0f}s exhausted"
            break

        params: dict = {"fields": fields, "limit": META_ACCOUNTS_PAGE_SIZE}
        if after:
            params["after"] = after
        data = provider._request(
            "GET",
            f"{base_url}/me/accounts",
            access_token=access_token,
            params=params,
        ).json()
        if "error" in data:
            logger.error("%s /me/accounts error: %s", provider.platform_name, data["error"])
            raise APIError(
                f"{error_message}: {data['error'].get('message', 'Unknown error')}",
                platform=provider.platform_name,
                raw_response=data,
            )

        raw_pages.extend(data.get("data", []))
        paging = data.get("paging") or {}
        next_cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        after = next_cursor
    else:
        truncated_reason = f"page cap of {META_ACCOUNTS_MAX_PAGES} reached"

    if truncated_reason:
        logger.warning(
            "%s /me/accounts truncated after %d entries: %s. Some accounts will not be offered.",
            provider.platform_name,
            len(raw_pages),
            truncated_reason,
        )
    else:
        logger.debug("%s /me/accounts returned %d entries", provider.platform_name, len(raw_pages))
    return raw_pages


def page_can_publish(page: dict) -> bool:
    """Whether the grant lets us create content on this Page.

    Meta reports the caller's per-Page permissions as a ``tasks`` list. A Page
    without ``CREATE_CONTENT`` accepts a connection and then fails every publish,
    so it must be rejected at onboarding rather than discovered later.

    An *omitted* ``tasks`` field is a Graph response that predates the field —
    unknown, not empty — and keeps the historical "assume publishable" behavior.
    An explicitly empty list is Meta telling us there are no tasks at all.
    """
    if "tasks" not in page:
        return True
    return "CREATE_CONTENT" in (page.get("tasks") or [])
