"""User-facing error messages for provider failures.

Every string a provider failure can put in front of a user is written here, so
"what do we actually show people?" is answerable by reading one file. The
classification is shared and the copy is per-surface: the same failure shapes
need different advice when a health check fails than when a post's first
comment fails.
"""

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from providers.exceptions import (
    APIError,
    OAuthError,
    PublishError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)

RECONNECT_MESSAGE = "Account connection expired. Please reconnect."
RATE_LIMIT_MESSAGE = "Rate limit reached. We'll retry this check shortly."
# A spent daily budget is not the rate limit above, and must not borrow its
# copy: "shortly" can be twenty hours away. YouTube's Data API quota, the one
# that put this here, refills at midnight US/Pacific — so the honest thing is
# to name the hour, which ``QuotaExceededError.resets_at`` already carries.
QUOTA_EXHAUSTED_MESSAGE = "The platform's daily API limit is used up."
PLATFORM_UNAVAILABLE_MESSAGE = "The platform is temporarily unavailable. We'll retry shortly."
GENERIC_MESSAGE = "Connection check failed. Please try reconnecting."

FIRST_COMMENT_RECONNECT_MESSAGE = (
    "The account connection expired, so the first comment wasn't added. "
    "Reconnect the account and add the comment manually."
)
FIRST_COMMENT_TEMPORARY_MESSAGE = "The platform was temporarily unavailable, so the first comment wasn't added."
FIRST_COMMENT_QUOTA_EXHAUSTED_MESSAGE = "The platform's daily API limit is used up, so the first comment wasn't added."
FIRST_COMMENT_REJECTED_MESSAGE = "The platform rejected the first comment. Add it manually on the post."
FIRST_COMMENT_GENERIC_MESSAGE = "The first comment couldn't be added. Add it manually on the post."

PUBLISH_RECONNECT_MESSAGE = "The account connection expired, so the post couldn't be published. Please reconnect it."
PUBLISH_TEMPORARY_MESSAGE = "The platform was temporarily unavailable. We'll retry shortly."
PUBLISH_RATE_LIMIT_MESSAGE = "The platform's rate limit was reached. We'll retry shortly."
PUBLISH_QUOTA_EXHAUSTED_MESSAGE = "The platform's daily API limit is used up, so the post couldn't be published."
PUBLISH_REJECTED_MESSAGE = "The platform rejected this post."
PUBLISH_GENERIC_MESSAGE = "Publishing failed. Please try again."
# The two messages above promise a retry, which is true only while attempts
# remain. Once the budget is spent the post is permanently failed and the
# composer must not keep telling the user to sit tight.
PUBLISH_EXHAUSTED_MESSAGE = (
    "Publishing kept failing, so we stopped retrying. Try again, or reconnect the account if it keeps happening."
)
# The worker was killed (deploy, restart, out-of-memory) between "we started
# publishing" and any outcome being recorded. We genuinely do not know whether
# the platform accepted the post, and re-publishing blind can duplicate a live
# video — so the copy asks the user to look before retrying.
PUBLISH_INTERRUPTED_MESSAGE = (
    "Publishing was interrupted before we could confirm it. "
    "Check the account to see whether the post went out, then publish again if it didn't."
)
# The platform took the upload and then never finished processing it. Distinct
# from a rejection: there is nothing to fix and nothing to see on the account.
PUBLISH_CONFIRM_TIMEOUT_MESSAGE = (
    "The platform accepted this post but never finished processing it. Try publishing it again."
)
# Strictly weaker than the two above, and it must read that way. The platform
# ACCEPTED the upload and then we could not reach it to ask what happened — so
# the post may well be live. Never tell this user to "try again": that is how a
# duplicate ends up on a real account. Ask them to look first.
PUBLISH_UNCONFIRMED_MESSAGE = (
    "This post was uploaded, but we couldn't confirm whether the platform published it. "
    "Check the account before publishing again — it may already be live."
)

# A failed webhook subscription costs real-time delivery, not delivery itself:
# the inbox still polls comments every few minutes. The copy says so, so nobody
# reads a missing subscription as a broken inbox.
WEBHOOK_RECONNECT_MESSAGE = "The account's permissions no longer cover comment delivery."
WEBHOOK_TEMPORARY_MESSAGE = "The platform was temporarily unavailable when we asked it to push updates."
WEBHOOK_REJECTED_MESSAGE = "The platform declined to push updates for this account."
WEBHOOK_GENERIC_MESSAGE = "We couldn't set up real-time updates for this account."
# Not a platform failure at all: this app could not build a client for the
# platform, which in practice means its app credentials are missing.
WEBHOOK_UNAVAILABLE_MESSAGE = "This platform isn't fully configured, so real-time updates couldn't be set up."

_EXPIRED_TOKEN_ERRORS = {
    "ExpiredToken",
    "invalid_token",
    "InvalidToken",
    "invalid_grant",
}

_RECONNECT = "reconnect"
_RATE_LIMITED = "rate_limited"
_QUOTA_EXHAUSTED = "quota_exhausted"
_UNAVAILABLE = "unavailable"
_REJECTED = "rejected"
_UNKNOWN = "unknown"


def _classify(exc: Exception) -> str:
    """Reduce a provider exception to one of the failure shapes above."""
    if isinstance(exc, TokenExpiredError):
        return _RECONNECT

    # Before RateLimitError, which it subclasses. A daily budget and a
    # per-second throttle need opposite advice — wait a moment versus wait
    # until tomorrow — so the broader clause must not answer for both.
    if isinstance(exc, QuotaExceededError):
        return _QUOTA_EXHAUSTED

    if isinstance(exc, RateLimitError):
        return _RATE_LIMITED

    if isinstance(exc, APIError):
        if exc.status_code in (401, 403):
            return _RECONNECT
        # ``raw_response["error"]`` is a bare string on OAuth token endpoints
        # ("invalid_grant") but a *dict* on every Graph API error. Hashing a
        # dict against this set raises TypeError, which would escape whatever
        # ``except`` block called us — guard the type, don't assume the shape.
        error_code = (exc.raw_response or {}).get("error")
        if isinstance(error_code, str) and error_code in _EXPIRED_TOKEN_ERRORS:
            return _RECONNECT
        if exc.status_code is not None and exc.status_code >= 500:
            return _UNAVAILABLE
        return _REJECTED

    if isinstance(exc, OAuthError):
        return _RECONNECT

    return _UNKNOWN


# The unmistakable openings of a serialized JSON object and a Python dict repr.
# A provider message containing either is quoting a response body rather than
# describing the failure.
_PAYLOAD_MARKERS = ('{"', "{'")
# No sentence we write about a publish failure comes close to this.
_MAX_PASSTHROUGH_LENGTH = 300


def _is_user_safe(message: str) -> bool:
    """Whether a provider-authored message can be shown as-is.

    Belt and braces around the pass-through below. Providers are supposed to
    put response bodies in ``raw_response`` and keep the message a sentence, but
    that is a convention a new provider can break silently, and the cost of
    breaking it is the platform's JSON rendered into the composer.
    """
    return (
        bool(message)
        and len(message) <= _MAX_PASSTHROUGH_LENGTH
        and not any(marker in message for marker in _PAYLOAD_MARKERS)
    )


def _quota_reset_phrase(exc: Exception) -> str:
    """ " We'll resume after 08:00 UTC." — or "" when the platform didn't say.

    Rendered in UTC rather than the viewer's zone because that is the only
    clock this code can be sure of, and because a quota window is a property of
    the platform, not of who is looking at it. Support answers "why is this
    stuck?" against the same hour the logs show.

    Anything further out than a day gets the date too: the phrase has to stay
    true if a platform ever hands us a longer window than YouTube's daily one.
    """
    resets_at = getattr(exc, "resets_at", None)
    if resets_at is None:
        return ""
    try:
        resets_utc = resets_at.astimezone(UTC)
        delta = resets_utc - datetime.now(UTC)
    except (AttributeError, TypeError, ValueError):
        return ""

    if delta > timedelta(days=1):
        return f" We'll resume after {resets_utc:%d %b %H:%M} UTC."
    return f" We'll resume after {resets_utc:%H:%M} UTC."


def _friendly(exc: Exception, copy: dict[str, str], fallback: str) -> str:
    """Map ``exc`` to user-facing text, preserving messages we authored.

    Only ``PublishError`` passes through, and only when it still looks like
    prose. Its messages are written by us for a human and are frequently the
    most useful thing we could say — "TikTok only supports VIDEO posts", "Trim
    the video and try again", "DEV.to requires a title" — so rewriting them
    generically would lose real information.

    Everything else is rewritten. ``APIError``/``RateLimitError`` messages are
    built by ``SocialProvider._request`` from ``response.text``, so they carry
    the platform's JSON — OAuthException types, fbtrace ids, internal subcodes.
    ``OAuthError`` interpolates a token-exchange body on some providers, and an
    auth failure maps to better advice than its own wording anyway.
    """
    if type(exc) is PublishError:
        message = str(exc).strip()
        if _is_user_safe(message):
            return message

    kind = _classify(exc)
    text = copy.get(kind, fallback)
    if kind is _QUOTA_EXHAUSTED:
        # Appended rather than baked into each surface's copy so every caller
        # names the same hour, and so a platform that gives us no reset time
        # degrades to the bare sentence instead of an empty promise.
        text += _quota_reset_phrase(exc)
    return text


def friendly_health_check_error(exc: Exception) -> str:
    """Map a provider exception to a short, user-facing message."""
    return _friendly(
        exc,
        {
            _RECONNECT: RECONNECT_MESSAGE,
            _RATE_LIMITED: RATE_LIMIT_MESSAGE,
            _QUOTA_EXHAUSTED: QUOTA_EXHAUSTED_MESSAGE,
            _UNAVAILABLE: PLATFORM_UNAVAILABLE_MESSAGE,
        },
        GENERIC_MESSAGE,
    )


def friendly_first_comment_error(exc: Exception) -> str:
    """Map a failed first-comment attempt to a message safe to show a user.

    Deliberately drops the platform's own wording for API errors. Meta's
    ``error_user_msg`` reads well but says nothing actionable — the payload
    behind this function's existence offered "Your Instagram comment was not
    added" — and the rest of the body is trace ids and internal type names.
    """
    return _friendly(
        exc,
        {
            _QUOTA_EXHAUSTED: FIRST_COMMENT_QUOTA_EXHAUSTED_MESSAGE,
            _RECONNECT: FIRST_COMMENT_RECONNECT_MESSAGE,
            _RATE_LIMITED: FIRST_COMMENT_TEMPORARY_MESSAGE,
            _UNAVAILABLE: FIRST_COMMENT_TEMPORARY_MESSAGE,
            _REJECTED: FIRST_COMMENT_REJECTED_MESSAGE,
        },
        FIRST_COMMENT_GENERIC_MESSAGE,
    )


class WebhookFailure(NamedTuple):
    """What to tell the user about a failed subscription, and what to offer them.

    One value rather than two functions so the message and the button can never
    describe different failures: both come from a single ``_classify`` call.
    """

    message: str
    needs_reconnect: bool


def classify_webhook_failure(exc: Exception) -> WebhookFailure:
    """Map a failed webhook subscription to user-facing text and a next step.

    Deliberately does not use ``_friendly``: its ``PublishError`` pass-through
    would emit a provider-authored message while ``needs_reconnect`` still came
    from ``_classify``, which is exactly the divergence this type exists to
    prevent. Nothing raised by ``subscribe_webhooks`` is safe to show raw —
    ``SocialProvider._request`` builds ``APIError`` messages from
    ``response.text[:500]``, so a rejection arrives as a truncated Graph payload
    ("(#100) Param subscribed_fields[0] must be one of {feed, mention, ...}",
    cut off mid-token). The full body is kept by the caller's
    ``logger.exception`` and in ``webhook_error_detail``.

    ``needs_reconnect`` marks the one class a retry can never fix: an auth
    failure means the grant is missing what the subscription needs.
    """
    kind = _classify(exc)
    message = {
        _RECONNECT: WEBHOOK_RECONNECT_MESSAGE,
        _RATE_LIMITED: WEBHOOK_TEMPORARY_MESSAGE,
        # A spent budget reads as temporary here on purpose: unlike the surfaces
        # above there is no retry to promise a time for, and the subscription is
        # worth re-trying once the window rolls over.
        _QUOTA_EXHAUSTED: WEBHOOK_TEMPORARY_MESSAGE,
        _UNAVAILABLE: WEBHOOK_TEMPORARY_MESSAGE,
        _REJECTED: WEBHOOK_REJECTED_MESSAGE,
    }.get(kind, WEBHOOK_GENERIC_MESSAGE)
    return WebhookFailure(message=message, needs_reconnect=kind == _RECONNECT)


def friendly_publish_error(exc: Exception) -> str:
    """Map a failed publish to a message safe to show a user.

    The raw text is not lost: every publish failure writes a PublishLog row
    carrying ``error_message`` before the post is marked failed.
    """
    return _friendly(
        exc,
        {
            _RECONNECT: PUBLISH_RECONNECT_MESSAGE,
            _RATE_LIMITED: PUBLISH_RATE_LIMIT_MESSAGE,
            _QUOTA_EXHAUSTED: PUBLISH_QUOTA_EXHAUSTED_MESSAGE,
            _UNAVAILABLE: PUBLISH_TEMPORARY_MESSAGE,
            _REJECTED: PUBLISH_REJECTED_MESSAGE,
        },
        PUBLISH_GENERIC_MESSAGE,
    )


CONNECT_QUOTA_EXHAUSTED_MESSAGE = "{platform}'s daily API limit is used up, so the account couldn't be connected."


def quota_connect_error(exc: Exception) -> str:
    """What the connect flow says when the platform's daily budget is spent.

    Its own message because the connect flow is the one surface with no retry
    of its own to describe: the user is standing at a redirect waiting to be
    told what to do. "Please try again" — what the callback said before — is
    actively wrong here, since every attempt until the window rolls over fails
    the same way. Naming the hour turns a dead end into a wait.
    """
    platform = getattr(exc, "platform", "") or "The platform"
    return CONNECT_QUOTA_EXHAUSTED_MESSAGE.format(platform=platform) + _quota_reset_phrase(exc).replace(
        " We'll resume after", " Try again after"
    )
