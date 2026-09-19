"""Inbox sync engine - polls connected accounts for new messages."""

import logging
import re
from datetime import timedelta
from typing import Any

from background_task import background
from django.conf import settings
from django.utils import timezone

from apps.common import quota
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from providers import get_provider
from providers.exceptions import ProviderError, QuotaExceededError, TokenExpiredError
from providers.google_errors import google_error_reasons

from .models import InboxMessage, InboxSLAConfig
from .sentiment import analyze_sentiment

logger = logging.getLogger(__name__)

# On an account's first-ever sync we may pull a historical backlog; notifications
# are suppressed for it, EXCEPT messages newer than this window — so a long-quiet
# account's genuinely-new first message still alerts instead of being swallowed.
INBOX_BACKLOG_NOTIFY_WINDOW = timedelta(hours=1)
_YOUTUBE_INBOX_REFRESH_WINDOW = timedelta(minutes=10)

# Which of a platform's budgets the inbox draws on. YouTube meters its Data API
# and its Analytics API separately, and comments come from the Data API — so
# blocking the poll because the *analytics* budget ran dry would stop the one
# call that was never the problem. Platforms that meter a single pool ignore the
# scope entirely; it is simply "" for them.
_QUOTA_SCOPE = "data"

# Used only when a platform refuses on quota without saying when it will stop.
# Long enough not to re-ask every cycle, short enough that a platform whose
# window we cannot read still recovers the same day.
_QUOTA_FALLBACK_BACKOFF = timedelta(hours=1)

# Platforms whose provider implements the ``deep`` walk. Only YouTube needs it:
# it is the only one whose routine poll stops early to protect a shared budget,
# and so the only one with a gap for a sweep to close.
_DEEP_SWEEP_PLATFORMS = ("youtube",)


def _provider_failure_details(exc: Exception) -> tuple[int | None, str]:
    """Return safe diagnostics without logging provider payloads or credentials."""
    status = getattr(exc, "status_code", None)
    raw_response = getattr(exc, "raw_response", None)
    reasons = google_error_reasons(raw_response) if isinstance(raw_response, dict) else set()
    if isinstance(raw_response, dict) and raw_response.get("error") == "invalid_grant":
        reasons.add("invalid_grant")
    safe_reasons = sorted(reason for reason in reasons if re.fullmatch(r"[a-z0-9_.-]{1,80}", reason))
    return status, ",".join(safe_reasons) or "unknown"


def _refresh_grant_rejected(exc: Exception) -> bool:
    """Only a permanent refresh refusal should prompt a reconnect check."""
    from apps.social_accounts.error_messages import _RECONNECT, _classify

    try:
        return _classify(exc) == _RECONNECT
    except Exception:
        return False


def _queue_health_check(account: SocialAccount) -> None:
    from apps.social_accounts.tasks import check_social_account_health

    try:
        check_social_account_health(str(account.id), remove_existing_tasks=True)
    except Exception as exc:
        logger.error("Could not queue inbox health check for account %s: error_type=%s", account.id, type(exc).__name__)


def _is_recent(ts):
    """True if a provider message timestamp falls within the backlog-notify window."""
    if ts is None:
        return False
    if timezone.is_naive(ts):
        ts = timezone.make_aware(ts, timezone.get_default_timezone())
    return ts >= timezone.now() - INBOX_BACKLOG_NOTIFY_WINDOW


def _related_post_key(extra: dict | None) -> str:
    """The post id a message hangs off, as PlatformPost stores it.

    Providers report the platform's own id (Facebook's is ``PAGEID_POSTID``);
    ``stored_post_id`` is the stripped form that matches
    ``PlatformPost.platform_post_id``. Fall back to the raw id for providers
    that don't strip.
    """
    return str((extra or {}).get("stored_post_id") or (extra or {}).get("post_id") or "")


def resolve_related_posts(account, messages) -> dict[str, Any]:
    """Map post ids in a batch of messages to this account's PlatformPost pks.

    One query for the whole batch rather than one per message — an active Page
    polls dozens of comments spread over a handful of posts.
    """
    from apps.composer.models import PlatformPost

    post_ids = {_related_post_key(getattr(msg, "extra", None)) for msg in messages}
    post_ids.discard("")
    if not post_ids:
        return {}

    return {
        platform_post_id: pk
        for platform_post_id, pk in PlatformPost.objects.filter(
            social_account=account,
            platform_post_id__in=post_ids,
        ).values_list("platform_post_id", "id")
    }


class InboxSyncEngine:
    """Syncs inbox messages from all connected social accounts."""

    def run_cycle(self):
        """Run one full inbox cycle: poll every connected account, then check SLAs.

        Single shared entry point for the ``run_inbox_sync`` management command
        and the recurring ``run_inbox_sync_cycle`` background task, so the two
        never diverge.
        """
        self.sync_all()
        self.check_sla()

    def sync_all(self):
        """Poll each connected account for new messages."""
        accounts = SocialAccount.objects.filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).select_related("workspace")

        # One dict for the whole pass. Accounts overwhelmingly share a single
        # OAuth client, so without it the breaker costs a query per account to
        # answer the same question — see apps.common.quota.quota_blocked_until.
        quota_cache: dict = {}

        for account in accounts:
            try:
                self._sync_account(account, quota_cache=quota_cache)
            except Exception:
                logger.exception("Inbox sync failed for account %s", account.id)

    def _poll_is_too_soon(self, account) -> bool:
        """Whether this account's platform floor says to leave it alone this cycle.

        The cycle runs every 5 minutes for everyone; this is what lets a
        platform with a shared daily budget opt out of most of those passes
        without slowing the platforms that have no such limit.
        """
        minimum = settings.INBOX_PLATFORM_MIN_POLL_SECONDS.get(account.platform)
        if not minimum or account.inbox_last_polled_at is None:
            return False
        return account.inbox_last_polled_at > timezone.now() - timedelta(seconds=minimum)

    def _deep_sweep_due(self, account) -> bool:
        """Whether this account is due a full walk of its comment history.

        A null stamp is *not* due: it means this account has never completed a
        poll, and the first one already imports the backlog. Sweeping then would
        pay for a second full walk of history we just read.
        """
        if account.platform not in _DEEP_SWEEP_PLATFORMS or account.inbox_last_deep_sweep_at is None:
            return False
        return account.inbox_last_deep_sweep_at <= timezone.now() - timedelta(seconds=settings.INBOX_DEEP_SWEEP_SECONDS)

    def _mark_polled(self, account, *, deep: bool = False) -> None:
        """Record that this account has been polled, so the floor can hold it off.

        Stamped after a failed provider call as well as a successful one: a
        platform that just refused us is not one to come back to in five
        minutes. The one exception is a quota refusal, where the breaker — not
        this clock — is what holds the account back, and re-stamping would
        outlast it.
        """
        now = timezone.now()
        fields = ["inbox_last_polled_at"]
        account.inbox_last_polled_at = now
        if deep or account.inbox_last_deep_sweep_at is None:
            account.inbox_last_deep_sweep_at = now
            fields.append("inbox_last_deep_sweep_at")
        account.save(update_fields=[*fields, "updated_at"])

    def _sync_account(self, account, *, quota_cache=None):
        """Sync messages for a single social account."""
        from apps.publisher.engine import _resolve_publish_credentials

        if self._poll_is_too_soon(account):
            return

        try:
            provider = get_provider(account.platform, _resolve_publish_credentials(account))
        except ValueError:
            logger.warning("No provider for platform %s", account.platform)
            return

        # The platform has already said "not until later". Polling anyway spends
        # a request to be refused and — on YouTube, where the budget is shared
        # across every account on this deployment — takes the refusal out of the
        # same pool that publishing and reconnecting need.
        credential = quota.credential_key(getattr(provider, "credentials", None))
        blocked_until = quota.quota_blocked_until(account.platform, credential, _QUOTA_SCOPE, cache=quota_cache)
        if blocked_until:
            logger.info(
                "Inbox poll skipped for account %s: %s quota blocked until %s",
                account.id,
                account.platform,
                blocked_until.isoformat(),
            )
            return

        last_msg = (
            InboxMessage.objects.filter(social_account=account)
            .order_by("-received_at")
            .values_list("received_at", flat=True)
            .first()
        )
        # First-sync suppression is per message *type*, not per account. An
        # account that has been polling DMs for months is not "first sync", but
        # the day comment polling starts working its whole comment backlog
        # arrives at once — and would notify every owner and manager for each
        # one. A type we have never seen before is a backlog by definition.
        seen_types = set(
            InboxMessage.objects.filter(social_account=account).values_list("message_type", flat=True).distinct()
        )

        deep = self._deep_sweep_due(account)

        try:
            if account.platform == "youtube":
                messages = self._get_youtube_messages(account, provider, last_msg, deep=deep)
            else:
                messages = provider.get_messages(access_token=account.oauth_access_token, since=last_msg)
        except NotImplementedError:
            return
        except QuotaExceededError as exc:
            # Must precede ProviderError: QuotaExceededError subclasses
            # RateLimitError, so the broader clause would swallow it and the
            # next poll five minutes later would spend another doomed request.
            self._trip_quota_block(account, credential, exc, cache=quota_cache)
            return
        except ProviderError as exc:
            status, reason = _provider_failure_details(exc)
            logger.warning(
                "get_messages() failed for account %s (%s): status=%s reason=%s",
                account.id,
                account.platform,
                status,
                reason,
            )
            self._mark_polled(account)
            return
        except Exception:
            logger.exception(
                "get_messages() failed for account %s (%s)",
                account.id,
                account.platform,
            )
            self._mark_polled(account)
            return

        if messages is None:
            self._mark_polled(account)
            return
        if account.platform == "youtube":
            logger.info("YouTube inbox poll completed for account %s: %s messages", account.id, len(messages))

        related_posts = resolve_related_posts(account, messages)

        for msg in messages:
            # Suppress notifications for the historical backlog pulled the first
            # time we see a message type, but still alert for genuinely recent
            # messages: a long-quiet account's first real message also looks like
            # a backlog, so a blanket mute would silently swallow it.
            # backfill_inbox seeds explicit history silently (notify=False).
            is_backlog = msg.message_type not in seen_types
            notify_new = not is_backlog or _is_recent(msg.timestamp)
            self._upsert_message(
                account,
                msg,
                notify=notify_new,
                related_post_id=related_posts.get(_related_post_key(msg.extra)),
            )

        self._mark_polled(account, deep=deep)

    def _trip_quota_block(self, account, credential, exc, *, cache=None) -> None:
        """Record the refusal so the rest of this pass — and the next — stand down.

        Mirrors ``apps.analytics.tasks._handle_quota_exhaustion``: the provider
        already knows when the window rolls over (midnight US/Pacific for
        YouTube's Data API) and which budget ran dry, so prefer its answer and
        fall back to a short cooldown only when it gives none.
        """
        until = getattr(exc, "resets_at", None) or (timezone.now() + _QUOTA_FALLBACK_BACKOFF)
        scope = getattr(exc, "quota_scope", "") or _QUOTA_SCOPE
        quota.trip_quota_block(
            account.platform,
            credential,
            scope,
            until=until,
            reason=str(exc),
            cache=cache,
        )

    def _get_youtube_messages(self, account, provider, since, *, deep: bool = False):
        """Refresh a stale YouTube token, with one auth retry and no backfill fan-out.

        ``deep`` is passed straight through to the provider, which is where it
        decides how far to page; nothing about the token dance changes with it.
        """
        account.refresh_from_db(
            fields=["oauth_access_token", "oauth_refresh_token", "token_expires_at", "connection_status"]
        )
        if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
            return None
        access_token = account.oauth_access_token
        refresh_attempted = False
        refresh_failed = None
        if account.oauth_refresh_token and account.token_expires_within(_YOUTUBE_INBOX_REFRESH_WINDOW):
            refresh_attempted = True
            try:
                access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
            except Exception as exc:
                refresh_failed = exc
                status, reason = _provider_failure_details(exc)
                logger.warning(
                    "YouTube inbox preflight refresh failed for account %s: status=%s reason=%s",
                    account.id,
                    status,
                    reason,
                )
                if _refresh_grant_rejected(exc):
                    _queue_health_check(account)

        try:
            return provider.get_messages(access_token=access_token, since=since, deep=deep)
        except TokenExpiredError as exc:
            status, reason = _provider_failure_details(exc)
            logger.warning(
                "YouTube inbox token rejected for account %s: status=%s reason=%s",
                account.id,
                status,
                reason,
            )

        # Another worker may have rotated the token while this account was being
        # polled. Prefer its persisted token before spending a second refresh.
        account.refresh_from_db(
            fields=["oauth_access_token", "oauth_refresh_token", "token_expires_at", "connection_status"]
        )
        if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
            return None
        if account.oauth_access_token != access_token:
            access_token = account.oauth_access_token
        elif not refresh_attempted and account.oauth_refresh_token:
            try:
                access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
            except Exception as exc:
                status, reason = _provider_failure_details(exc)
                logger.warning(
                    "YouTube inbox auth refresh failed for account %s: status=%s reason=%s",
                    account.id,
                    status,
                    reason,
                )
                if _refresh_grant_rejected(exc):
                    _queue_health_check(account)
                return None
        else:
            if refresh_failed is None:
                _queue_health_check(account)
            return None

        try:
            messages = provider.get_messages(access_token=access_token, since=since, deep=deep)
        except TokenExpiredError as exc:
            status, reason = _provider_failure_details(exc)
            logger.warning(
                "YouTube inbox token still rejected after retry for account %s: status=%s reason=%s",
                account.id,
                status,
                reason,
            )
            _queue_health_check(account)
            return None
        logger.info("YouTube inbox poll recovered for account %s", account.id)
        return messages

    def _upsert_message(self, account, msg, notify=True, related_post_id=None):
        """Create or update an inbox message, deduplicating by platform_message_id."""
        defaults = {
            "workspace": account.workspace,
            "sender_name": msg.sender_name,
            "sender_handle": msg.extra.get("sender_handle", msg.sender_id),
            "sender_avatar_url": msg.extra.get("sender_avatar_url", ""),
            "body": msg.text,
            "message_type": msg.message_type,
            "received_at": msg.timestamp,
            "extra": msg.extra,
        }
        if related_post_id:
            defaults["related_post_id"] = related_post_id

        obj, created = InboxMessage.objects.update_or_create(
            social_account=account,
            platform_message_id=msg.platform_message_id,
            defaults=defaults,
        )
        if created:
            obj.sentiment = analyze_sentiment(obj.body)
            obj.save(update_fields=["sentiment"])
            if notify:
                self._notify_new_message(obj)

    def _notify_new_message(self, message):
        """Send notification for a new inbox message."""
        if message.assigned_to:
            users = [message.assigned_to]
        else:
            memberships = WorkspaceMembership.objects.filter(
                workspace=message.workspace,
                workspace_role__in=["owner", "manager"],
            ).select_related("user")
            users = [m.user for m in memberships]

        for user in users:
            notify(
                user=user,
                event_type=EventType.NEW_INBOX_MESSAGE,
                title=f"New {message.get_message_type_display()} from {message.sender_name}",
                body=message.body[:200],
                data={
                    "message_id": str(message.id),
                    "workspace_id": str(message.workspace_id),
                },
            )

    def check_sla(self):
        """Check for SLA-overdue messages and send notifications."""
        from datetime import timedelta

        configs = InboxSLAConfig.objects.filter(is_active=True).select_related("workspace")

        for config in configs:
            threshold = timezone.now() - timedelta(minutes=config.target_response_minutes)
            overdue_messages = InboxMessage.objects.filter(
                workspace=config.workspace,
                status__in=[InboxMessage.Status.UNREAD, InboxMessage.Status.OPEN],
                received_at__lte=threshold,
            ).exclude(extra__has_key="sla_notified")

            for message in overdue_messages:
                self._notify_sla_overdue(message, config)
                message.extra["sla_notified"] = True
                message.save(update_fields=["extra"])

    def _notify_sla_overdue(self, message, config):
        """Notify about an SLA-overdue message."""
        if message.assigned_to:
            users = [message.assigned_to]
        else:
            memberships = WorkspaceMembership.objects.filter(
                workspace=message.workspace,
                workspace_role__in=["owner", "manager"],
            ).select_related("user")
            users = [m.user for m in memberships]

        for user in users:
            notify(
                user=user,
                event_type=EventType.INBOX_SLA_OVERDUE,
                title=f"SLA overdue: {message.get_message_type_display()} from {message.sender_name}",
                body=f"Response target of {config.target_response_minutes} minutes exceeded.",
                data={
                    "message_id": str(message.id),
                    "workspace_id": str(message.workspace_id),
                },
            )


# How often the recurring inbox-sync cycle runs; registered on a repeating
# schedule by apps.inbox.apps.InboxConfig. Polling is the always-on baseline
# documented in architecture.md (webhooks, where configured, supplement it).
INBOX_SYNC_INTERVAL_SECONDS = 5 * 60  # every 5 minutes


@background(schedule=0)
def run_inbox_sync_cycle():
    """Run one inbox cycle on the shared ``process_tasks`` worker (every deploy target).

    Delegates to ``InboxSyncEngine.run_cycle`` — the same entry point the
    ``run_inbox_sync`` management command uses — so the two never diverge.
    """
    InboxSyncEngine().run_cycle()
