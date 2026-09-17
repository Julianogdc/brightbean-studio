"""Notification engine - the single entry point all features call.

Usage:
    from apps.notifications.engine import notify

    notify(
        user=some_user,
        event_type="post_approved",
        title="Post approved",
        body="Your post 'New product launch' was approved by Jane.",
        data={"post_id": str(post.id), "workspace_id": str(ws.id)},
    )
"""

import hashlib
import hmac
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone

from .models import (
    Channel,
    DeliveryStatus,
    EventType,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    QuietHours,
)

logger = logging.getLogger(__name__)

MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_MINUTES = [1, 5, 30]
# Cap how many deliveries a single retry sweep processes, so a PENDING backlog
# that accumulated before the periodic retry was scheduled drains gradually
# across runs instead of bursting all at once.
RETRY_BATCH_LIMIT = 200

# --- Email batching -------------------------------------------------------
#
# Some email is worth sending the instant it fires (a failed post, a disconnected
# account, an invite someone is waiting on). The rest is high-volume and low
# urgency, and one email per event turns into a flood. Those event types are
# queued on the delivery row (``batch_queued_at``) instead of dispatched inline,
# and ``send_batched_email_digests`` collapses each user's queue into one email.
#
# The same queue also carries users who switched on ``QuietHours.digest_mode``:
# for them EVERY event type is queued and delivered once a day at
# DAILY_DIGEST_HOUR in their own timezone.
#
# The two populations are swept SEPARATELY (see _flush_daily_digests and
# _flush_rolling_batches). They must never share a candidate window: daily rows
# sit queued for hours and rolling rows for minutes, so on a single oldest-first
# query every daily row sorts ahead of every rolling row and would starve it.
#
# Kept deliberately small: anything someone is actively waiting on (an approval
# request, a failed post, an invite) stays immediate. POST_SUBMITTED in
# particular is a reviewer waiting to act, so it is NOT batched.
BATCHED_EMAIL_EVENTS = {
    EventType.REPORT_GENERATED,
    EventType.ENGAGEMENT_ALERT,
    EventType.COMMENT_MENTION,
    EventType.APPROVAL_REMINDER,
}

# How long a group of queued emails waits before it is flushed. This is the
# fallback: the effective delay comes from ``org.email_batching_delay_minutes``
# via the settings cascade (workspace -> org -> app default), and is only used
# directly for notifications that carry no workspace. See _rolling_window_minutes.
BATCH_WINDOW_MINUTES = 5
EMAIL_BATCHING_DELAY_SETTING = "org.email_batching_delay_minutes"
# ...unless it reaches this many queued emails first, which flushes it early.
# Deliberately NOT applied to digest_mode users: a "daily digest" that arrives
# at lunchtime because the tenth notification landed is not a daily digest.
BATCH_SIZE_TRIGGER = 10
# Local hour at which digest_mode users receive their digest, in the timezone on
# their QuietHours row. Anchored to the clock rather than measured from the
# oldest queued row, so the digest lands at the same time every day instead of
# drifting forward by however long the user was idle.
DAILY_DIGEST_HOUR = 8
# A sweep that dies mid-send leaves rows claimed. Reclaim them after this long.
BATCH_CLAIM_TIMEOUT_MINUTES = 15
# Cap on groups flushed per sweep, mirroring RETRY_BATCH_LIMIT's reasoning.
# Applied to DUE groups only — capping before the due-check lets groups that are
# still inside their window consume the whole budget and starve the rest.
BATCH_GROUP_LIMIT = 100
# Cap on queued rows read per sweep, per population. Oldest-queued first, so a
# backlog drains in the order it built up; the rest waits for the next run.
BATCH_CANDIDATE_LIMIT = 2000
# Cap on notifications in a single digest email. A backlog over this goes out as
# consecutive emails rather than one unreadable (and memory-hungry) message.
MAX_DIGEST_NOTIFICATIONS = 50

# Default channel enablement per event type.
# Key: event_type, Value: dict of channel → default enabled.
DEFAULT_CHANNELS: dict[str, dict[str, bool]] = {
    EventType.POST_SUBMITTED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.POST_APPROVED: {Channel.IN_APP: True, Channel.EMAIL: False},
    EventType.POST_CHANGES_REQUESTED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.POST_REJECTED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.POST_PUBLISHED: {Channel.IN_APP: True, Channel.EMAIL: False},
    EventType.POST_FAILED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.NEW_INBOX_MESSAGE: {Channel.IN_APP: True, Channel.EMAIL: False},
    EventType.INBOX_SLA_OVERDUE: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.CLIENT_APPROVAL_REQUESTED: {Channel.IN_APP: False, Channel.EMAIL: True},
    EventType.TEAM_MEMBER_INVITED: {Channel.IN_APP: False, Channel.EMAIL: True},
    EventType.SOCIAL_ACCOUNT_DISCONNECTED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.REPORT_GENERATED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.ENGAGEMENT_ALERT: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.COMMENT_MENTION: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.APPROVAL_REMINDER: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.APPROVAL_STALLED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.APPROVAL_HOLD_REQUESTED: {Channel.IN_APP: True, Channel.EMAIL: True},
    EventType.CLIENT_CONNECTED_ACCOUNTS: {Channel.IN_APP: True, Channel.EMAIL: True},
}

# Event types considered non-critical (suppressed during quiet hours).
NON_CRITICAL_EVENTS = {
    EventType.POST_PUBLISHED,
    EventType.REPORT_GENERATED,
    EventType.ENGAGEMENT_ALERT,
}


def notify(
    user,
    event_type: str,
    title: str,
    body: str = "",
    data: dict | None = None,
) -> Notification | None:
    """Create a notification and dispatch to enabled channels.

    This is the single entry point that all features call. The function:
    1. Creates the Notification record (always).
    2. Checks the user's per-event/per-channel preferences.
    3. Respects quiet hours (suppresses non-critical events).
    4. Creates NotificationDelivery records for each enabled channel.
    5. Dispatches immediately (in-app is a DB write, email/webhook are async-safe),
       except for email that belongs in a digest, which is queued instead —
       see BATCHED_EMAIL_EVENTS and ``send_batched_email_digests``.

    Returns the created Notification, or None if the user or event_type is invalid.
    """
    if not user or not user.is_active:
        return None

    if event_type not in EventType.values:
        logger.warning("Unknown event_type: %s", event_type)
        return None

    notification = Notification.objects.create(
        user=user,
        event_type=event_type,
        title=title,
        body=body,
        data=data or {},
    )

    channels_to_dispatch = _resolve_channels(user, event_type)
    digest_mode = _is_digest_mode(user)

    if _is_in_quiet_hours(user) and event_type in NON_CRITICAL_EVENTS:
        # During quiet hours, only deliver in-app (silent). Skip email/webhook.
        #
        # EMAIL is exempt for digest_mode users, and only for them: dropping it
        # here would LOSE the notification rather than silence it, because their
        # email is queued and the digest is the only thing that will ever send
        # it. The digest's own send hour is what keeps them undisturbed. The
        # webhook stays suppressed either way — it fires immediately, so
        # exempting it would defeat quiet hours outright.
        quiet_channels = {Channel.IN_APP, Channel.EMAIL} if digest_mode else {Channel.IN_APP}
        channels_to_dispatch = [c for c in channels_to_dispatch if c in quiet_channels]

    batch_email = Channel.EMAIL in channels_to_dispatch and (digest_mode or event_type in BATCHED_EMAIL_EVENTS)

    for channel in channels_to_dispatch:
        queued_at = timezone.now() if (channel == Channel.EMAIL and batch_email) else None
        delivery = NotificationDelivery.objects.create(
            notification=notification,
            channel=channel,
            status=DeliveryStatus.PENDING,
            batch_queued_at=queued_at,
        )
        if queued_at is None:
            _dispatch(delivery)
        # Queued rows stay PENDING with no next_retry_at, so the retry sweep
        # ignores them and only send_batched_email_digests picks them up.

    return notification


def _is_digest_mode(user) -> bool:
    """Whether the user asked for their notification email as a daily digest."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return bool(user.quiet_hours.digest_mode)
    except (AttributeError, ObjectDoesNotExist):
        return False


def _resolve_channels(user, event_type: str, pref_cache: dict | None = None) -> list[str]:
    """Determine which channels are enabled for this user + event_type.

    Checks user preferences first; falls back to DEFAULT_CHANNELS.
    Accepts an optional pref_cache dict to avoid repeated queries in batch operations.
    """
    if pref_cache is not None and event_type in pref_cache:
        pref_map = pref_cache[event_type]
    else:
        prefs = NotificationPreference.objects.filter(user=user, event_type=event_type).values_list(
            "channel", "is_enabled"
        )
        pref_map = dict(prefs)
        if pref_cache is not None:
            pref_cache[event_type] = pref_map

    defaults = DEFAULT_CHANNELS.get(event_type, {})
    channels: list[str] = []

    for channel_value in [Channel.IN_APP, Channel.EMAIL, Channel.WEBHOOK]:
        if channel_value in pref_map:
            if pref_map[channel_value]:
                channels.append(str(channel_value))
        elif defaults.get(channel_value, False):
            channels.append(str(channel_value))

    return channels


def _is_in_quiet_hours(user) -> bool:
    """Check if the user is currently in their quiet hours window."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        qh = user.quiet_hours
    except (AttributeError, ObjectDoesNotExist):
        return False

    if not qh.is_enabled or not qh.start_time or not qh.end_time:
        return False

    now_local = timezone.now().astimezone(_resolve_timezone(qh.timezone)).time()

    # Coerce to time objects - fields may be raw strings if the in-memory
    # QuietHours instance was populated from POST data and not yet refreshed.
    from datetime import time as dt_time

    start = qh.start_time
    end = qh.end_time
    if isinstance(start, str):
        try:
            parts = start.split(":")
            start = dt_time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return False
    if isinstance(end, str):
        try:
            parts = end.split(":")
            end = dt_time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return False

    if start <= end:
        return start <= now_local <= end
    else:
        # Overnight range (e.g., 22:00 - 07:00)
        return now_local >= start or now_local <= end


def _resolve_timezone(tz_name: str):
    """Resolve an IANA timezone name, falling back to UTC on anything unusable."""
    import zoneinfo

    try:
        return zoneinfo.ZoneInfo(tz_name)
    except (KeyError, ValueError, zoneinfo.ZoneInfoNotFoundError):
        return zoneinfo.ZoneInfo("UTC")


def _app_url() -> str:
    return getattr(settings, "APP_URL", "http://localhost:8000")


def _send_notification_email(*, to_email: str, subject: str, context: dict, template_stem: str) -> None:
    """Render and send one notification email.

    Shared by the immediate path (``_dispatch_email``) and the digest path
    (``_send_digest_email``) so the message is addressed and built one way.
    """
    text_content = render_to_string(f"notifications/email/{template_stem}.txt", context)
    html_content = render_to_string(f"notifications/email/{template_stem}.html", context)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        to=[to_email],
    )
    msg.attach_alternative(html_content, "text/html")
    msg.send(fail_silently=False)


def _dispatch(delivery: NotificationDelivery) -> None:
    """Dispatch a single delivery to its channel."""
    delivery.attempts += 1
    delivery.save(update_fields=["attempts"])

    try:
        if delivery.channel == Channel.IN_APP:
            _dispatch_in_app(delivery)
        elif delivery.channel == Channel.EMAIL:
            _dispatch_email(delivery)
        elif delivery.channel == Channel.WEBHOOK:
            _dispatch_webhook(delivery)
        else:
            logger.warning("Unknown channel: %s", delivery.channel)
            return

        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = timezone.now()
        delivery.save(update_fields=["status", "delivered_at"])

    except Exception as exc:
        logger.exception("Delivery failed: %s", delivery.id)
        delivery.error_message = str(exc)[:500]

        if delivery.attempts >= MAX_RETRY_ATTEMPTS:
            delivery.status = DeliveryStatus.FAILED
        else:
            delivery.status = DeliveryStatus.PENDING
            backoff_idx = min(delivery.attempts - 1, len(RETRY_BACKOFF_MINUTES) - 1)
            delivery.next_retry_at = timezone.now() + timedelta(minutes=RETRY_BACKOFF_MINUTES[backoff_idx])

        delivery.save(update_fields=["status", "error_message", "next_retry_at"])


def _dispatch_in_app(delivery: NotificationDelivery) -> None:
    """In-app delivery is just the DB record - already created."""
    pass


def _dispatch_email(delivery: NotificationDelivery) -> None:
    """Send notification email using Django's email backend."""
    notification = delivery.notification
    user = notification.user

    _send_notification_email(
        to_email=user.email,
        subject=notification.title,
        context={"notification": notification, "user": user, "app_url": _app_url()},
        template_stem="notification",
    )


def _dispatch_webhook(delivery: NotificationDelivery) -> None:
    """Send notification via webhook (HTTP POST with HMAC-SHA256 signature).

    The webhook URL is re-validated with is_safe_url at dispatch time (not just
    when stored), and redirects are not followed. This narrows the DNS-rebind
    window between validation and connection. We still rely on the OS-level DNS
    cache to resolve consistently within a single dispatch; deployments with
    aggressive DNS-rebind threat models should additionally enforce egress
    firewall rules.
    """
    import httpx

    from apps.common.validators import is_safe_url

    notification = delivery.notification

    webhook_url = notification.data.get("webhook_url")
    if not webhook_url:
        logger.info("No webhook_url in notification data, skipping webhook delivery")
        return

    # Re-validate immediately before the request. The single-pass DNS resolve
    # used by is_safe_url is reused by httpx via the OS resolver cache; this
    # is the simplest defence that doesn't add an httpx-transport dependency.
    if not is_safe_url(webhook_url):
        raise RuntimeError("Webhook URL rejected: must be a public http(s) endpoint")

    payload = json.dumps(
        {
            "event_type": notification.event_type,
            "title": notification.title,
            "body": notification.body,
            "data": notification.data,
            "created_at": notification.created_at.isoformat(),
            "user_id": str(notification.user_id),
        },
        default=str,
    ).encode("utf-8")

    webhook_secret = getattr(settings, "WEBHOOK_SECRET", settings.SECRET_KEY)
    signature = hmac.new(
        webhook_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature-256": f"sha256={signature}",
        "X-Event-Type": notification.event_type,
    }

    # follow_redirects=False prevents a 302→private-IP bait-and-switch from a
    # legitimate-looking endpoint. Any redirect is surfaced as a delivery
    # failure, not silently followed.
    response = httpx.post(webhook_url, content=payload, headers=headers, timeout=10.0, follow_redirects=False)
    if 300 <= response.status_code < 400:
        raise RuntimeError(f"Webhook URL replied with redirect {response.status_code} — refusing to follow.")
    if response.status_code >= 400:
        raise RuntimeError(f"Webhook returned HTTP {response.status_code}")


def retry_failed_deliveries() -> int:
    """Retry deliveries that are pending and past their next_retry_at.

    Called by a background task on a periodic schedule.
    Returns the count of retried deliveries.
    """
    now = timezone.now()
    pending = (
        NotificationDelivery.objects.filter(
            status=DeliveryStatus.PENDING,
            next_retry_at__isnull=False,
            next_retry_at__lte=now,
            attempts__lt=MAX_RETRY_ATTEMPTS,
        )
        .select_related("notification", "notification__user")
        .order_by("next_retry_at")[:RETRY_BATCH_LIMIT]
    )

    count = 0
    for delivery in pending:
        _dispatch(delivery)
        count += 1

    return count


def send_batched_email_digests() -> int:
    """Flush queued digest email, one message per due group.

    Called by a background task on a periodic schedule. Returns the number of
    emails sent.

    The two populations are swept independently, each with its own candidate
    window and group budget: digest_mode users (one daily email covering every
    event type) and everyone else (one email per event type per short window).
    Sharing a window between them would let the long-lived daily rows, which are
    always the oldest, crowd the short-lived rolling rows out of every sweep.

    Concurrency: rows are claimed with a conditional UPDATE that only matches
    unclaimed (or stale-claimed) rows, so a second sweep running at the same
    time finds nothing left to take. The sweep's own ``now`` doubles as the
    claim token when reading back what it won.
    """
    now = timezone.now()
    stale_claim_cutoff = now - timedelta(minutes=BATCH_CLAIM_TIMEOUT_MINUTES)

    retired = _retire_queued_for_inactive_users()
    if retired:
        logger.info("Retired %d queued digest deliveries for deactivated recipients", retired)

    exhausted = _retire_exhausted_queued_deliveries()
    if exhausted:
        logger.warning("Gave up on %d queued digest deliveries that used up their attempts", exhausted)

    return _flush_daily_digests(now, stale_claim_cutoff) + _flush_rolling_batches(now, stale_claim_cutoff)


def _queued_deliveries(stale_claim_cutoff):
    """Base queryset of email deliveries waiting in the digest queue."""
    return (
        NotificationDelivery.objects.filter(
            status=DeliveryStatus.PENDING,
            channel=Channel.EMAIL,
            batch_queued_at__isnull=False,
            attempts__lt=MAX_RETRY_ATTEMPTS,
            notification__user__is_active=True,
        )
        .filter(Q(batch_claimed_at__isnull=True) | Q(batch_claimed_at__lt=stale_claim_cutoff))
        .select_related("notification", "notification__user")
        .order_by("batch_queued_at")
    )


def _retire_queued_for_inactive_users() -> int:
    """Mark queued email for deactivated recipients as FAILED.

    Without this they are skipped on every sweep but never reach a terminal
    state, so they stay PENDING forever — and because they are the oldest rows
    in the queue they would sit permanently at the front of the oldest-first
    candidate window, shrinking the usable budget of every later sweep.
    """
    return NotificationDelivery.objects.filter(
        status=DeliveryStatus.PENDING,
        channel=Channel.EMAIL,
        batch_queued_at__isnull=False,
        notification__user__is_active=False,
    ).update(
        status=DeliveryStatus.FAILED,
        error_message="Recipient was deactivated before the digest was sent.",
        batch_claimed_at=None,
    )


def _retire_exhausted_queued_deliveries() -> int:
    """Mark queued email that used up its attempts without a confirmed send.

    _fail_batch already retires rows whose send raised. This catches the other
    path: a worker that died mid-send leaves rows PENDING and claimed, and each
    reclaim spends another attempt. Once they are out of attempts the candidate
    query stops seeing them, so without this they would sit PENDING forever.
    """
    exhausted = NotificationDelivery.objects.filter(
        status=DeliveryStatus.PENDING,
        channel=Channel.EMAIL,
        batch_queued_at__isnull=False,
        attempts__gte=MAX_RETRY_ATTEMPTS,
    )

    # Don't overwrite a real SMTP error with the generic message.
    exhausted.filter(error_message="").update(
        error_message="Digest send did not complete within the retry budget.",
    )
    return exhausted.update(status=DeliveryStatus.FAILED, batch_claimed_at=None)


def _flush_daily_digests(now, stale_claim_cutoff) -> int:
    """Send one email per digest_mode user whose local send hour has arrived."""
    rows = list(
        _queued_deliveries(stale_claim_cutoff).filter(notification__user__quiet_hours__digest_mode=True)[
            :BATCH_CANDIDATE_LIMIT
        ]
    )
    if not rows:
        return 0

    groups: dict[UUID, list[NotificationDelivery]] = defaultdict(list)
    for row in rows:
        groups[row.notification.user_id].append(row)

    # Timezones for exactly the users in this batch, not every digest user.
    timezones = dict(QuietHours.objects.filter(user_id__in=list(groups)).values_list("user_id", "timezone"))

    due = [
        deliveries
        for user_id, deliveries in groups.items()
        if _daily_digest_is_due(deliveries, now, timezones.get(user_id) or "UTC")
    ]

    sent = 0
    for deliveries in due[:BATCH_GROUP_LIMIT]:
        if _flush_group(deliveries, now, stale_claim_cutoff, daily=True, event_type=None):
            sent += 1
    return sent


def _flush_rolling_batches(now, stale_claim_cutoff) -> int:
    """Send one email per (user, event_type) group whose short window is up."""
    rows = list(
        _queued_deliveries(stale_claim_cutoff).exclude(notification__user__quiet_hours__digest_mode=True)[
            :BATCH_CANDIDATE_LIMIT
        ]
    )
    if not rows:
        return 0

    groups: dict[tuple, list[NotificationDelivery]] = defaultdict(list)
    for row in rows:
        groups[(row.notification.user_id, row.notification.event_type)].append(row)

    window_cache: dict = {}
    due = [
        (key, deliveries)
        for key, deliveries in groups.items()
        if _rolling_batch_is_due(deliveries, now, _rolling_window_minutes(deliveries, window_cache))
    ]

    sent = 0
    for (_user_id, event_type), deliveries in due[:BATCH_GROUP_LIMIT]:
        if _flush_group(deliveries, now, stale_claim_cutoff, daily=False, event_type=event_type):
            sent += 1
    return sent


def _flush_group(deliveries, now, stale_claim_cutoff, *, daily: bool, event_type: str | None) -> bool:
    """Claim a group (capped to one email's worth) and send it."""
    # Anything beyond the cap stays queued and goes out on a following sweep,
    # rather than rendering one enormous message.
    chunk = deliveries[:MAX_DIGEST_NOTIFICATIONS]

    claimed = _claim_batch(chunk, now, stale_claim_cutoff)
    if not claimed:
        # Another sweep took this group between the read and the claim.
        return False

    return _send_digest_email(claimed, daily=daily, event_type=event_type)


def _oldest_queued_at(deliveries: list[NotificationDelivery]) -> datetime | None:
    """Earliest queue time in a group, or None if somehow nothing is queued.

    ``batch_queued_at`` is nullable on the model, and the candidate query
    already filters the nulls out, so in practice this always returns a value.
    Narrowing it here keeps that assumption in one place and makes a group that
    breaks it simply never come due, rather than crashing the sweep for every
    other user in it.
    """
    queued = [d.batch_queued_at for d in deliveries if d.batch_queued_at is not None]
    return min(queued) if queued else None


def _daily_digest_is_due(deliveries: list[NotificationDelivery], now, tz_name: str) -> bool:
    """Whether this user's local send hour has passed for a queue that predates it.

    Anchored to the wall clock rather than to an elapsed window, so the digest
    lands at DAILY_DIGEST_HOUR every day. Anything queued after today's send
    time waits for tomorrow instead of dragging the send time forward.
    """
    user_tz = _resolve_timezone(tz_name)
    now_local = now.astimezone(user_tz)
    send_time_today = now_local.replace(hour=DAILY_DIGEST_HOUR, minute=0, second=0, microsecond=0)

    if now_local < send_time_today:
        return False

    oldest = _oldest_queued_at(deliveries)
    if oldest is None:
        return False

    return oldest.astimezone(user_tz) < send_time_today


def _batching_delay_for_workspace(workspace_id) -> int:
    """Resolve ``org.email_batching_delay_minutes`` for one workspace."""
    from apps.settings_manager.helpers import get_setting

    try:
        minutes = int(get_setting(workspace_id, EMAIL_BATCHING_DELAY_SETTING))
    except Exception:
        # A malformed workspace_id in notification.data, or a non-numeric
        # override, must not take the sweep down with it.
        logger.warning("Unusable email batching delay for workspace %s; using the default", workspace_id)
        return BATCH_WINDOW_MINUTES

    return minutes if minutes >= 0 else BATCH_WINDOW_MINUTES


def _rolling_window_minutes(deliveries: list[NotificationDelivery], cache: dict) -> int:
    """The batching delay that applies to a rolling group.

    A group is keyed on (user, event_type) and can therefore span workspaces, so
    the shortest configured delay wins: a workspace that asked to be notified
    promptly should not be made to wait on a slower one. ``cache`` is per sweep,
    so each workspace's cascade is resolved once however many groups touch it.
    """
    windows = []

    for delivery in deliveries:
        workspace_id = (delivery.notification.data or {}).get("workspace_id")
        if not workspace_id:
            continue
        if workspace_id not in cache:
            cache[workspace_id] = _batching_delay_for_workspace(workspace_id)
        windows.append(cache[workspace_id])

    return min(windows) if windows else BATCH_WINDOW_MINUTES


def _rolling_batch_is_due(deliveries: list[NotificationDelivery], now, window_minutes: int) -> bool:
    """Whether a short-window group has waited long enough, or grown big enough."""
    oldest = _oldest_queued_at(deliveries)
    if oldest is not None and oldest <= now - timedelta(minutes=window_minutes):
        return True

    # The size trigger flushes a busy batch early so it doesn't sit growing for
    # the full window. It has no daily equivalent on purpose: a busy morning
    # must not ship "today's digest" hours before the day is up.
    return len(deliveries) >= BATCH_SIZE_TRIGGER


def _claim_batch(deliveries: list[NotificationDelivery], now, stale_claim_cutoff) -> list[NotificationDelivery]:
    """Take ownership of a group, returning only the rows this sweep won.

    The UPDATE is conditional on the row still being unclaimed (or its claim
    having gone stale), so concurrent sweeps can't both send the same batch.

    The attempt is charged HERE rather than after a successful send. If the
    worker dies between SMTP accepting the message and the delivered-bookkeeping
    UPDATE, the rows stay PENDING and are reclaimed once the claim goes stale —
    with the attempt already spent, so a crash loop resends a bounded number of
    times instead of forever.
    """
    ids = [d.pk for d in deliveries]

    claimed = (
        NotificationDelivery.objects.filter(pk__in=ids)
        .filter(Q(batch_claimed_at__isnull=True) | Q(batch_claimed_at__lt=stale_claim_cutoff))
        .update(batch_claimed_at=now, attempts=F("attempts") + 1)
    )
    if not claimed:
        return []

    return list(
        NotificationDelivery.objects.filter(pk__in=ids, batch_claimed_at=now)
        .select_related("notification", "notification__user")
        .order_by("batch_queued_at")
    )


def _send_digest_email(deliveries: list[NotificationDelivery], *, daily: bool, event_type: str | None) -> bool:
    """Send one email covering ``deliveries`` and mark them delivered."""
    user = deliveries[0].notification.user
    notifications = [d.notification for d in deliveries]
    count = len(notifications)
    plural = "" if count == 1 else "s"

    if daily:
        subject = f"Daily Digest - {count} notification{plural}"
    elif event_type in EventType.values:
        subject = f"{EventType(event_type).label} - {count} update{plural}"
    else:
        subject = f"{count} notification{plural}"

    context = {
        "notifications": notifications,
        "user": user,
        "date": timezone.now(),
        "is_daily": daily,
        "app_url": _app_url(),
    }

    try:
        _send_notification_email(
            to_email=user.email,
            subject=subject,
            context=context,
            template_stem="digest",
        )
    except Exception as exc:
        logger.exception("Batched digest email failed for %s", user.email)
        _fail_batch(deliveries, exc)
        return False

    # The attempt was already charged at claim time.
    NotificationDelivery.objects.filter(pk__in=[d.pk for d in deliveries]).update(
        status=DeliveryStatus.DELIVERED,
        delivered_at=timezone.now(),
    )
    logger.info("Sent %s digest to %s (%d notification%s)", "daily" if daily else "batched", user.email, count, plural)
    return True


def _fail_batch(deliveries: list[NotificationDelivery], exc: Exception) -> None:
    """Record a failed digest send and release the claim for a later sweep.

    The attempt was already charged by _claim_batch, so a digest that keeps
    failing gives up at MAX_RETRY_ATTEMPTS like any other delivery.
    ``next_retry_at`` stays None on purpose: these rows belong to the digest
    sweep, and setting it would hand them to retry_failed_deliveries, which
    sends one email each — exactly the flood the queue exists to prevent.
    """
    ids = [d.pk for d in deliveries]

    NotificationDelivery.objects.filter(pk__in=ids).update(
        error_message=str(exc)[:500],
        batch_claimed_at=None,
    )
    NotificationDelivery.objects.filter(pk__in=ids, attempts__gte=MAX_RETRY_ATTEMPTS).update(
        status=DeliveryStatus.FAILED,
    )
