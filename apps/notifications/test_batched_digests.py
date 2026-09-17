"""digest_mode routes a user's notification email through the batch queue.

Regression: ``QuietHours.digest_mode`` was a user-facing toggle that nothing
read. Its only consumer, ``tasks.send_daily_digests``, was never registered on a
schedule — so switching it on did nothing at all, and had that task ever been
scheduled it would have sent a digest *on top of* the immediate emails the user
was already getting, because the two paths shared no bookkeeping.

digest_mode now queues the email instead of sending it inline, on the same queue
the short rolling batch uses, delivered once a day at DAILY_DIGEST_HOUR in the
user's own timezone.
"""

import contextlib
import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from apps.accounts.models import User
from apps.notifications.engine import (
    BATCH_CLAIM_TIMEOUT_MINUTES,
    BATCH_SIZE_TRIGGER,
    BATCH_WINDOW_MINUTES,
    DAILY_DIGEST_HOUR,
    EMAIL_BATCHING_DELAY_SETTING,
    MAX_DIGEST_NOTIFICATIONS,
    MAX_RETRY_ATTEMPTS,
    notify,
    retry_failed_deliveries,
    send_batched_email_digests,
)
from apps.notifications.models import (
    Channel,
    DeliveryStatus,
    EventType,
    NotificationDelivery,
    NotificationPreference,
    QuietHours,
)

# Event types with email on by default that are NOT in BATCHED_EMAIL_EVENTS, so
# a non-digest user gets them immediately. Using these proves digest_mode
# captures email that would otherwise have gone out the moment it fired.
IMMEDIATE_EMAIL_EVENT = EventType.POST_FAILED
OTHER_IMMEDIATE_EMAIL_EVENT = EventType.SOCIAL_ACCOUNT_DISCONNECTED
# In BATCHED_EMAIL_EVENTS, so it queues for everyone on the short window.
ROLLING_BATCH_EVENT = EventType.COMMENT_MENTION
# In BATCHED_EMAIL_EVENTS *and* NON_CRITICAL_EVENTS, so it is the event that
# quiet hours would otherwise drop before it could reach a digest.
QUIET_HOURS_DROPPABLE_EVENT = EventType.REPORT_GENERATED


def at(hour, minute=0, day=17):
    """A fixed UTC moment in September 2026, for clock-anchored assertions."""
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=datetime.UTC)


@contextmanager
def frozen_now(moment):
    """Pin timezone.now() so the daily send hour is deterministic."""
    with patch("django.utils.timezone.now", return_value=moment):
        yield


def email_deliveries(user):
    return NotificationDelivery.objects.filter(notification__user=user, channel=Channel.EMAIL)


def queued_at(user, moment):
    """Pin a user's queued rows to an absolute time."""
    email_deliveries(user).filter(batch_queued_at__isnull=False).update(batch_queued_at=moment)


def age_queue(user, *, minutes):
    """Backdate a user's queued rows so a relative window looks elapsed."""
    email_deliveries(user).filter(batch_queued_at__isnull=False).update(
        batch_queued_at=timezone.now() - datetime.timedelta(minutes=minutes)
    )


def make_user(suffix, *, digest_mode=False, tz="UTC"):
    user = User.objects.create_user(
        email=f"{suffix}@example.com", password="testpass123", name=suffix, tos_accepted_at=timezone.now()
    )
    if digest_mode:
        QuietHours.objects.create(user=user, digest_mode=True, timezone=tz)
    return user


@pytest.mark.django_db
class TestDigestModeUser:
    def test_gets_one_daily_email_and_no_immediate_ones(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=True)

        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        notify(user, OTHER_IMMEDIATE_EMAIL_EVENT, "Account disconnected")
        notify(user, EventType.TEAM_MEMBER_INVITED, "You were invited")

        # Nothing inline: the queued row IS the delivery, not an extra copy.
        assert mailoutbox == []
        queued = email_deliveries(user)
        assert queued.count() == 3
        assert all(d.batch_queued_at is not None for d in queued)
        assert all(d.status == DeliveryStatus.PENDING for d in queued)

        queued_at(user, at(6))

        # Before the send hour, nothing goes out.
        with frozen_now(at(DAILY_DIGEST_HOUR - 1)):
            assert send_batched_email_digests() == 0
        assert mailoutbox == []

        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 1

        assert len(mailoutbox) == 1

        # One email, covering every event type rather than one per type.
        sent = mailoutbox[0]
        assert sent.to == [user.email]
        assert "Daily Digest" in sent.subject
        assert "Post failed" in sent.body
        assert "Account disconnected" in sent.body
        assert "You were invited" in sent.body
        # Re-query: `queued` cached its rows back when they were still PENDING.
        assert set(email_deliveries(user).values_list("status", flat=True)) == {DeliveryStatus.DELIVERED}

        # A second sweep must not re-send what it already delivered.
        with frozen_now(at(DAILY_DIGEST_HOUR + 2)):
            assert send_batched_email_digests() == 0
        assert len(mailoutbox) == 1

    def test_send_time_is_anchored_to_the_clock_not_to_the_queue(self, user, mailoutbox):
        """Queueing at noon must not drag the send time to noon tomorrow."""
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Queued after today's send hour")
        queued_at(user, at(DAILY_DIGEST_HOUR + 4))

        # Later the same day: today's send hour has already passed, so this
        # waits rather than going out a bare hour after it was queued.
        with frozen_now(at(DAILY_DIGEST_HOUR + 5)):
            assert send_batched_email_digests() == 0

        # Tomorrow's send hour, not 24h after the notification landed.
        with frozen_now(at(DAILY_DIGEST_HOUR, day=18)):
            assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_send_hour_follows_the_users_timezone(self, mailoutbox):
        # 08:00 in New York is 12:00 UTC (EDT in September).
        ny_user = make_user("newyork", digest_mode=True, tz="America/New_York")
        notify(ny_user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(ny_user, at(2))

        # 09:00 UTC is 05:00 in New York — before their send hour.
        with frozen_now(at(9)):
            assert send_batched_email_digests() == 0

        # 13:00 UTC is 09:00 in New York — past it.
        with frozen_now(at(13)):
            assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_size_trigger_does_not_force_the_digest_early(self, user, mailoutbox):
        """A busy day must not ship "today's digest" before the send hour."""
        QuietHours.objects.create(user=user, digest_mode=True)

        for i in range(BATCH_SIZE_TRIGGER + 2):
            notify(user, IMMEDIATE_EMAIL_EVENT, f"Post failed {i}")

        assert email_deliveries(user).count() == BATCH_SIZE_TRIGGER + 2
        queued_at(user, at(6))

        # Well past the size trigger, but the send hour has not arrived.
        with frozen_now(at(DAILY_DIGEST_HOUR - 1)):
            assert send_batched_email_digests() == 0
        assert mailoutbox == []

        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_a_huge_backlog_is_split_across_emails(self, user, mailoutbox):
        """One day's backlog must not render as a single enormous message."""
        QuietHours.objects.create(user=user, digest_mode=True)
        overflow = 10
        for i in range(MAX_DIGEST_NOTIFICATIONS + overflow):
            notify(user, IMMEDIATE_EMAIL_EVENT, f"Post failed {i}")
        queued_at(user, at(6))

        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 1
            assert email_deliveries(user).filter(status=DeliveryStatus.PENDING).count() == overflow

            # The remainder is not dropped — it goes out on the next sweep.
            assert send_batched_email_digests() == 1

        assert len(mailoutbox) == 2
        assert email_deliveries(user).filter(status=DeliveryStatus.PENDING).count() == 0

    def test_per_event_opt_out_still_wins(self, user, mailoutbox):
        """Turning email off for one event beats digest_mode turning it on."""
        QuietHours.objects.create(user=user, digest_mode=True)
        NotificationPreference.objects.create(
            user=user,
            event_type=IMMEDIATE_EMAIL_EVENT,
            channel=Channel.EMAIL,
            is_enabled=False,
        )

        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        notify(user, EventType.POST_REJECTED, "Post rejected")

        # The opted-out event never becomes an email delivery, so there is
        # nothing for the queue to pick up.
        assert not email_deliveries(user).filter(notification__event_type=IMMEDIATE_EMAIL_EVENT).exists()
        assert email_deliveries(user).filter(notification__event_type=EventType.POST_REJECTED).count() == 1

        queued_at(user, at(6))
        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 1

        assert len(mailoutbox) == 1
        assert "Post rejected" in mailoutbox[0].body
        assert "Post failed" not in mailoutbox[0].body

    def test_quiet_hours_defers_into_the_digest_instead_of_dropping(self, user, mailoutbox):
        """Suppressing email for a digest user would lose it, not silence it."""
        QuietHours.objects.create(
            user=user,
            is_enabled=True,
            start_time=datetime.time(0, 0),
            end_time=datetime.time(23, 59),
            timezone="UTC",
            digest_mode=True,
        )

        notify(user, QUIET_HOURS_DROPPABLE_EVENT, "Report ready")

        # Queued, not discarded: the digest's send hour is what keeps the user
        # undisturbed, so the notification still reaches them.
        assert mailoutbox == []
        assert email_deliveries(user).count() == 1

        queued_at(user, at(6))
        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 1
        assert "Report ready" in mailoutbox[0].body

    def test_failed_send_releases_the_claim_and_gives_up_at_max_attempts(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))

        with (
            patch.object(EmailMultiAlternatives, "send", side_effect=RuntimeError("smtp down")),
            frozen_now(at(DAILY_DIGEST_HOUR + 1)),
        ):
            for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                assert send_batched_email_digests() == 0
                delivery = email_deliveries(user).get()
                assert delivery.attempts == attempt
                # The claim is released each time, so the next sweep can retry.
                assert delivery.batch_claimed_at is None

            delivery = email_deliveries(user).get()
            assert delivery.status == DeliveryStatus.FAILED
            assert delivery.attempts == MAX_RETRY_ATTEMPTS
            assert "smtp down" in delivery.error_message
            # Exhausted rows are left alone rather than retried forever.
            assert send_batched_email_digests() == 0

        assert mailoutbox == []

    def test_stale_claim_is_reclaimed_but_a_fresh_one_is_not(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))
        sweep_time = at(DAILY_DIGEST_HOUR + 1)

        # A sweep already holds this row; a concurrent sweep must leave it be.
        email_deliveries(user).update(batch_claimed_at=sweep_time)
        with frozen_now(sweep_time):
            assert send_batched_email_digests() == 0
        assert mailoutbox == []

        # That sweep died without sending. Once the claim goes stale, reclaim it.
        email_deliveries(user).update(
            batch_claimed_at=sweep_time - datetime.timedelta(minutes=BATCH_CLAIM_TIMEOUT_MINUTES + 1)
        )
        with frozen_now(sweep_time):
            assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_queued_email_for_a_deactivated_recipient_is_retired(self, user, mailoutbox):
        """Otherwise these rows stay PENDING forever at the head of the queue."""
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))

        user.is_active = False
        user.save(update_fields=["is_active"])

        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 0

        delivery = email_deliveries(user).get()
        assert delivery.status == DeliveryStatus.FAILED
        assert "deactivated" in delivery.error_message
        assert mailoutbox == []


@pytest.mark.django_db
class TestNonDigestModeUser:
    def test_immediate_email_is_unaffected(self, user, mailoutbox):
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")

        assert len(mailoutbox) == 1
        delivery = email_deliveries(user).get()
        assert delivery.batch_queued_at is None
        assert delivery.status == DeliveryStatus.DELIVERED

        # Nothing was queued, so the sweep has nothing to do.
        assert send_batched_email_digests() == 0
        assert len(mailoutbox) == 1

    def test_digest_mode_off_explicitly_is_also_unaffected(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=False)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")

        assert len(mailoutbox) == 1
        assert email_deliveries(user).get().batch_queued_at is None

    def test_quiet_hours_still_suppresses_non_critical_email(self, user, mailoutbox):
        """Unchanged for everyone who did not opt into a digest."""
        QuietHours.objects.create(
            user=user,
            is_enabled=True,
            start_time=datetime.time(0, 0),
            end_time=datetime.time(23, 59),
            timezone="UTC",
        )
        notify(user, QUIET_HOURS_DROPPABLE_EVENT, "Report ready")

        assert mailoutbox == []
        assert not email_deliveries(user).exists()

    def test_rolling_batch_still_flushes_on_the_short_window(self, user, mailoutbox):
        notify(user, ROLLING_BATCH_EVENT, "Mention")

        assert mailoutbox == []
        assert send_batched_email_digests() == 0

        age_queue(user, minutes=BATCH_WINDOW_MINUTES + 1)
        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1
        assert "Mention" in mailoutbox[0].body

    def test_rolling_batch_still_flushes_on_the_size_trigger(self, user, mailoutbox):
        """The size trigger is intact for the short window — only daily opts out."""
        for i in range(BATCH_SIZE_TRIGGER):
            notify(user, ROLLING_BATCH_EVENT, f"Mention {i}")

        assert mailoutbox == []
        # No ageing: the group is over the size trigger, so it flushes early.
        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_a_digest_user_does_not_hold_up_anyone_else(self, user, mailoutbox):
        other = make_user("digest", digest_mode=True)

        notify(other, IMMEDIATE_EMAIL_EVENT, "Queued for the digest user")
        notify(user, IMMEDIATE_EMAIL_EVENT, "Immediate for everyone else")

        assert len(mailoutbox) == 1
        assert "Immediate for everyone else" in mailoutbox[0].body
        assert email_deliveries(other).get().batch_queued_at is not None
        assert email_deliveries(user).get().batch_queued_at is None


@pytest.mark.django_db
class TestPopulationsDoNotStarveEachOther:
    """Daily rows are always older than rolling rows, so a shared oldest-first
    window would hand the whole sweep budget to digests that aren't even due.

    Both budgets are patched down rather than filled with hundreds of real
    users — the property under test is that not-due daily groups never consume
    a budget, which does not depend on the budget's size.
    """

    def _waiting_digest_users(self, count):
        """Digest users with rows queued past their send hour, so not yet due."""
        for i in range(count):
            waiting = make_user(f"waiting{i}", digest_mode=True)
            notify(waiting, IMMEDIATE_EMAIL_EVENT, f"queued {i}")
        NotificationDelivery.objects.filter(channel=Channel.EMAIL).update(batch_queued_at=at(DAILY_DIGEST_HOUR + 1))

    def _due_rolling_user(self, label):
        victim = make_user(label)
        notify(victim, ROLLING_BATCH_EVENT, "should go out now")
        queued_at(victim, at(DAILY_DIGEST_HOUR + 1) - datetime.timedelta(minutes=BATCH_WINDOW_MINUTES + 1))
        return victim

    def test_pending_daily_groups_do_not_consume_the_group_budget(self, mailoutbox):
        budget = 2
        self._waiting_digest_users(budget)
        self._due_rolling_user("group-victim")

        with frozen_now(at(DAILY_DIGEST_HOUR + 2)), patch("apps.notifications.engine.BATCH_GROUP_LIMIT", budget):
            assert send_batched_email_digests() == 1

        assert len(mailoutbox) == 1
        assert "should go out now" in mailoutbox[0].body

    def test_pending_daily_rows_do_not_consume_the_candidate_budget(self, mailoutbox):
        budget = 2
        self._waiting_digest_users(budget + 1)
        self._due_rolling_user("candidate-victim")

        with frozen_now(at(DAILY_DIGEST_HOUR + 2)), patch("apps.notifications.engine.BATCH_CANDIDATE_LIMIT", budget):
            assert send_batched_email_digests() == 1

        assert len(mailoutbox) == 1
        assert "should go out now" in mailoutbox[0].body


@pytest.mark.django_db
class TestCrashSafety:
    """A worker that dies mid-send must not resend the same digest forever."""

    def test_a_crash_after_smtp_accepts_cannot_resend_indefinitely(self, user):
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))

        accepted = []

        def smtp_accepts_then_worker_dies(**kwargs):
            accepted.append(kwargs["subject"])
            # A BaseException is not caught by _send_digest_email's `except
            # Exception`, so nothing is recorded — exactly what a killed worker
            # leaves behind: mail delivered, rows still PENDING and claimed.
            raise KeyboardInterrupt("worker killed before bookkeeping")

        sweeps = MAX_RETRY_ATTEMPTS + 3
        for attempt in range(1, sweeps + 1):
            sweep_at = at(DAILY_DIGEST_HOUR + attempt)
            # Let the previous claim go stale so this sweep reclaims the rows.
            email_deliveries(user).filter(batch_claimed_at__isnull=False).update(
                batch_claimed_at=sweep_at - datetime.timedelta(minutes=BATCH_CLAIM_TIMEOUT_MINUTES + 1)
            )
            with (
                patch("apps.notifications.engine._send_notification_email", smtp_accepts_then_worker_dies),
                frozen_now(sweep_at),
                contextlib.suppress(KeyboardInterrupt),
            ):
                send_batched_email_digests()

        # The attempt is charged at claim time, so the crash loop is bounded by
        # MAX_RETRY_ATTEMPTS rather than resending on every stale-claim reclaim.
        assert len(accepted) == MAX_RETRY_ATTEMPTS
        assert email_deliveries(user).get().status == DeliveryStatus.FAILED

    def test_exhausted_queued_rows_are_retired_rather_than_left_pending(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))

        # Stand in for a worker that died after every claim: attempts are spent
        # but the rows were never marked delivered or failed.
        email_deliveries(user).update(attempts=MAX_RETRY_ATTEMPTS, batch_claimed_at=at(DAILY_DIGEST_HOUR))

        with frozen_now(at(DAILY_DIGEST_HOUR + 1)):
            assert send_batched_email_digests() == 0

        delivery = email_deliveries(user).get()
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.batch_claimed_at is None
        assert delivery.error_message
        assert mailoutbox == []

    def test_a_real_smtp_error_is_not_overwritten_by_the_generic_message(self, user, mailoutbox):
        QuietHours.objects.create(user=user, digest_mode=True)
        notify(user, IMMEDIATE_EMAIL_EVENT, "Post failed")
        queued_at(user, at(6))

        with (
            patch.object(EmailMultiAlternatives, "send", side_effect=RuntimeError("mailbox full")),
            frozen_now(at(DAILY_DIGEST_HOUR + 1)),
        ):
            for _ in range(MAX_RETRY_ATTEMPTS):
                send_batched_email_digests()

        delivery = email_deliveries(user).get()
        assert delivery.status == DeliveryStatus.FAILED
        assert "mailbox full" in delivery.error_message


@pytest.mark.django_db
class TestQuietHoursChannels:
    def test_a_digest_users_webhook_stays_suppressed(self, user, mailoutbox):
        """Only the queued email is exempt from quiet hours — not the webhook.

        A webhook fires immediately, so exempting it alongside email would
        defeat quiet hours outright for anyone who turned on a digest.
        """
        QuietHours.objects.create(
            user=user,
            is_enabled=True,
            start_time=datetime.time(0, 0),
            end_time=datetime.time(23, 59),
            timezone="UTC",
            digest_mode=True,
        )
        NotificationPreference.objects.create(
            user=user,
            event_type=QUIET_HOURS_DROPPABLE_EVENT,
            channel=Channel.WEBHOOK,
            is_enabled=True,
        )

        with patch("apps.notifications.engine._dispatch_webhook") as dispatch_webhook:
            notify(user, QUIET_HOURS_DROPPABLE_EVENT, "Report ready", data={"webhook_url": "https://hooks.test/x"})

        dispatch_webhook.assert_not_called()
        assert not NotificationDelivery.objects.filter(notification__user=user, channel=Channel.WEBHOOK).exists()
        # The email is still queued for the digest.
        assert email_deliveries(user).count() == 1


@pytest.mark.django_db
class TestConfiguredBatchingDelay:
    """org.email_batching_delay_minutes must actually drive the rolling window."""

    def _workspace(self):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        org = Organization.objects.create(name="Batching Org")
        return Workspace.objects.create(name="Batching Workspace", organization=org)

    def test_an_override_lengthens_the_window(self, user, mailoutbox):
        from apps.settings_manager.models import WorkspaceSetting

        workspace = self._workspace()
        WorkspaceSetting.objects.create(
            workspace=workspace, key=EMAIL_BATCHING_DELAY_SETTING, value=BATCH_WINDOW_MINUTES * 4
        )

        notify(user, ROLLING_BATCH_EVENT, "Mention", data={"workspace_id": str(workspace.id)})

        # Past the built-in default, but well inside the configured delay.
        age_queue(user, minutes=BATCH_WINDOW_MINUTES + 1)
        assert send_batched_email_digests() == 0
        assert mailoutbox == []

        age_queue(user, minutes=BATCH_WINDOW_MINUTES * 4 + 1)
        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_an_override_shortens_the_window(self, user, mailoutbox):
        from apps.settings_manager.models import WorkspaceSetting

        workspace = self._workspace()
        WorkspaceSetting.objects.create(workspace=workspace, key=EMAIL_BATCHING_DELAY_SETTING, value=1)

        notify(user, ROLLING_BATCH_EVENT, "Mention", data={"workspace_id": str(workspace.id)})
        age_queue(user, minutes=2)

        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_a_notification_without_a_workspace_uses_the_default(self, user, mailoutbox):
        notify(user, ROLLING_BATCH_EVENT, "Mention")
        age_queue(user, minutes=BATCH_WINDOW_MINUTES + 1)

        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1

    def test_an_unusable_workspace_id_falls_back_instead_of_crashing(self, user, mailoutbox):
        notify(user, ROLLING_BATCH_EVENT, "Mention", data={"workspace_id": "not-a-uuid"})
        age_queue(user, minutes=BATCH_WINDOW_MINUTES + 1)

        assert send_batched_email_digests() == 1
        assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_retry_sweep_leaves_queued_rows_alone(user, mailoutbox):
    """The double-send guard: only the digest sweep may send a queued row.

    Queued rows are PENDING with no next_retry_at, which is the only thing
    keeping retry_failed_deliveries from dispatching each one as its own email —
    restoring exactly the flood the queue exists to prevent.
    """
    QuietHours.objects.create(user=user, digest_mode=True)
    for i in range(3):
        notify(user, IMMEDIATE_EMAIL_EVENT, f"Post failed {i}")

    assert email_deliveries(user).count() == 3

    assert retry_failed_deliveries() == 0
    assert mailoutbox == []
    assert set(email_deliveries(user).values_list("status", flat=True)) == {DeliveryStatus.PENDING}
    assert all(d.attempts == 0 for d in email_deliveries(user))


@pytest.mark.django_db
def test_only_one_digest_implementation_remains():
    """Guards the double-send this change removed.

    ``send_daily_digests`` read notifications straight off the Notification
    table with no delivery bookkeeping, so it could not tell what had already
    been emailed. Scheduling it alongside the immediate path was the bug.
    """
    from apps.notifications import tasks

    assert not hasattr(tasks, "send_daily_digests")
    assert hasattr(tasks, "send_batched_email_digests")
