"""Exception hierarchy for social platform providers."""


class ProviderError(Exception):
    """Base exception for all provider errors.

    ``retryable=False`` marks the error as permanent: the publish engine
    fails the post immediately instead of scheduling backoff retries.
    """

    def __init__(
        self,
        message: str,
        platform: str = "",
        raw_response: dict | None = None,
        retryable: bool = True,
    ):
        self.platform = platform
        self.raw_response = raw_response or {}
        self.retryable = retryable
        super().__init__(message)


class OAuthError(ProviderError):
    """OAuth flow failure (invalid code, denied access, etc.)."""


class TokenExpiredError(ProviderError):
    """Access token has expired and refresh failed or is unavailable.

    ``status_code`` carries the HTTP status the platform answered with, and is
    load-bearing rather than decorative: ``apps.publisher.engine``'s
    ``_is_ambiguous_submission_failure`` and ``_is_retryable_first_comment_failure``
    both read it through ``getattr(exc, "status_code", None)`` and treat a
    missing code as "outcome unknown, do not retry". A 401 that used to arrive
    as ``APIError(status_code=401)`` must keep answering 401 here or a
    first comment silently flips from retryable to untouchable.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        **kwargs,
    ):
        self.status_code = status_code
        super().__init__(message, **kwargs)


class RateLimitError(ProviderError):
    """Platform rate limit exceeded."""

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
        **kwargs,
    ):
        self.retry_after = retry_after
        super().__init__(message, **kwargs)


class QuotaExceededError(RateLimitError):
    """A hard — usually daily — API quota is spent, not a per-second throttle.

    Subclasses :class:`RateLimitError` so every existing consumer keeps working
    unchanged: ``apps.social_accounts.error_messages._classify`` reports it as
    rate-limited rather than sending the user to reconnect a perfectly healthy
    account, and the publish engine's two retry gates short-circuit on their
    ``isinstance(exc, RateLimitError)`` checks before they reach ``status_code``.

    ``resets_at`` is when the window rolls over, when the platform's quota has a
    knowable boundary (YouTube's resets at midnight US/Pacific). ``quota_scope``
    names *which* pool ran dry for platforms that meter more than one — YouTube
    charges the Data API and the Analytics API against separate budgets, and
    conflating them would stop the cheap call because the expensive one failed.
    """

    def __init__(
        self,
        message: str,
        *,
        resets_at=None,
        status_code: int | None = None,
        quota_scope: str = "",
        **kwargs,
    ):
        self.resets_at = resets_at
        self.status_code = status_code
        self.quota_scope = quota_scope
        super().__init__(message, **kwargs)


class PublishError(ProviderError):
    """Post publishing failed."""


class APIError(ProviderError):
    """Generic API error from the platform."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        **kwargs,
    ):
        self.status_code = status_code
        super().__init__(message, **kwargs)
