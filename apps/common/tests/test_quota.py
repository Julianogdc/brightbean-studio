"""The shared quota breaker's alerting behaviour.

The breaker's mechanics (idempotency, the per-run cache, expiry) are exercised
through its callers in ``apps/analytics/tests/test_tasks.py`` and
``apps/inbox/tests/test_sync.py``. What is tested here is the part no caller
can assert for itself: whether a block is loud enough for anyone to notice.

That distinction is the whole reason this file exists. A spent daily budget
means a platform is gone for the rest of the day — publishing, analytics and
reconnecting included — and the first time it happened nobody found out until a
user wrote in. A five-minute throttle is the breaker doing its job. Logging
both identically means either the daily case is missed or the throttle case
trains everyone to ignore it.
"""

import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.quota import credential_key, quota_blocked_until, trip_quota_block


class TestCredentialKey:
    def test_the_same_client_gets_the_same_key(self):
        """Accounts sharing an OAuth client share a budget, so they must share a row."""
        assert credential_key({"client_id": "abc"}) == credential_key({"client_id": "abc"})

    def test_different_clients_do_not_collide(self):
        assert credential_key({"client_id": "abc"}) != credential_key({"client_id": "xyz"})

    def test_the_key_does_not_contain_the_client_id(self):
        """It lands in the database and in logs; the client_id must not."""
        assert "abc" not in credential_key({"client_id": "abc"})

    @pytest.mark.parametrize("credentials", [None, {}, {"client_id": ""}])
    def test_unresolvable_credentials_share_one_key(self, credentials):
        assert credential_key(credentials) == "unknown"


@pytest.mark.django_db
class TestBlockAlerting:
    def test_a_spent_daily_budget_logs_at_error(self, caplog):
        """ERROR is what reaches Sentry, and a lost day is worth an event."""
        with caplog.at_level(logging.WARNING, logger="apps.common.quota"):
            trip_quota_block(
                "youtube",
                credential_key({"client_id": "c"}),
                "data",
                until=timezone.now() + timedelta(hours=9),
                reason="daily quota exhausted",
            )

        assert [r.levelno for r in caplog.records] == [logging.ERROR]

    def test_a_short_throttle_stays_a_warning(self, caplog):
        """Paging someone for a five-minute cooldown is how alerts get ignored."""
        with caplog.at_level(logging.WARNING, logger="apps.common.quota"):
            trip_quota_block(
                "youtube",
                credential_key({"client_id": "c"}),
                "data",
                until=timezone.now() + timedelta(minutes=5),
                reason="request rate throttled",
            )

        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_a_later_shorter_block_cannot_shorten_a_longer_one(self, caplog):
        """A throttle arriving mid-exhaustion must not wave the platform back in."""
        key = credential_key({"client_id": "c"})
        long_until = timezone.now() + timedelta(hours=9)
        trip_quota_block("youtube", key, "data", until=long_until, reason="daily quota exhausted")

        trip_quota_block(
            "youtube",
            key,
            "data",
            until=timezone.now() + timedelta(minutes=5),
            reason="request rate throttled",
        )

        assert quota_blocked_until("youtube", key, "data") == long_until
