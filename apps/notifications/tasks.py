"""Background tasks for the notification system.

These are meant to be called by django-background-tasks or a cron schedule.
"""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often the recurring delivery-retry sweep runs; registered on a repeating
# schedule by apps.notifications.apps.NotificationsConfig.
NOTIFICATION_RETRY_INTERVAL_SECONDS = 60  # every minute

# How often queued digest email is flushed. The sweep decides per group whether
# the window has elapsed, so this only needs to be fine-grained enough for the
# shortest window (engine.BATCH_WINDOW_MINUTES); daily digests come due on the
# same sweep.
NOTIFICATION_BATCH_INTERVAL_SECONDS = 60  # every minute


@background(schedule=0)
def retry_failed_deliveries():
    """Retry pending notification deliveries that are past their backoff window.

    Registered on a 1-minute repeating schedule. ``notify()`` dispatches the
    first attempt inline; transient email/webhook failures leave the delivery
    PENDING with a ``next_retry_at`` that only this sweep acts on.

    Deliveries queued for a digest are deliberately out of scope here: they carry
    ``batch_queued_at`` and no ``next_retry_at``, so only the digest sweep below
    sends them.
    """
    from .engine import retry_failed_deliveries as _retry

    count = _retry()
    if count > 0:
        logger.info("Retried %d failed notification deliveries", count)


@background(schedule=0)
def send_batched_email_digests():
    """Flush queued notification email into one message per user per window.

    Registered on a 1-minute repeating schedule. Covers both batched event types
    and users who turned on ``QuietHours.digest_mode`` — the latter get every
    event type collapsed into a single daily email. This is the only digest
    implementation; there is no separate daily task.
    """
    from .engine import send_batched_email_digests as _send

    count = _send()
    if count > 0:
        logger.info("Sent %d batched notification digest emails", count)
