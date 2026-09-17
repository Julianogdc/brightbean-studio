"""Tests for YouTubeProvider analytics, batching and error classification."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from providers.exceptions import APIError, QuotaExceededError, RateLimitError, TokenExpiredError
from providers.google_errors import next_google_quota_reset
from providers.youtube import _ANALYTICS_VIDEO_FILTER_CHUNK, ANALYTICS_BASE, API_BASE, YouTubeProvider


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _date_range() -> tuple[datetime, datetime]:
    return (
        datetime(2005, 2, 14, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 6, 3, 23, 59, 59, tzinfo=UTC),
    )


class TestGetPostAnalytics:
    @patch.object(YouTubeProvider, "_request")
    def test_request_shape(self, mock_request):
        mock_request.return_value = _make_response(
            {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "estimatedMinutesWatched"},
                    {"name": "averageViewPercentage"},
                    {"name": "shares"},
                ],
                "rows": [],
            }
        )

        provider = YouTubeProvider()
        provider.get_post_analytics("token-xyz", ["abc123", "def456"], _date_range())

        # One call, GET against the Analytics /reports endpoint.
        assert mock_request.call_count == 1
        args, kwargs = mock_request.call_args
        assert args[0] == "GET"
        assert args[1] == "https://youtubeanalytics.googleapis.com/v2/reports"
        assert kwargs["access_token"] == "token-xyz"
        params = kwargs["params"]
        assert params["ids"] == "channel==MINE"
        assert params["startDate"] == "2005-02-14"
        assert params["endDate"] == "2026-06-03"
        assert params["metrics"] == "estimatedMinutesWatched,averageViewPercentage,shares"
        assert params["dimensions"] == "video"
        # filter joins post_ids with commas after the `video==` operator.
        assert params["filters"] == "video==abc123,def456"

    @patch.object(YouTubeProvider, "_request")
    def test_parses_rows_into_post_metrics(self, mock_request):
        mock_request.return_value = _make_response(
            {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "estimatedMinutesWatched"},
                    {"name": "averageViewPercentage"},
                    {"name": "shares"},
                ],
                "rows": [
                    ["abc123", 1500.0, 47.5, 12.0],
                    ["def456", 0.0, 0.0, 0.0],
                ],
            }
        )

        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", ["abc123", "def456"], _date_range())

        assert set(result.keys()) == {"abc123", "def456"}
        # watch_time and avg_view_pct flow through ``extra`` (the catalog
        # mapper reads them from _GENERIC_POST_EXTRA_KEYS).
        assert result["abc123"].extra == {"watch_time": 1500.0, "avg_view_pct": 47.5}
        # shares lives on the PostMetrics dataclass field — that's where
        # ``_post_metrics_to_dict`` looks for it.
        assert result["abc123"].shares == 12
        # Real zero is preserved on extras, not dropped — same semantics
        # as the closure in get_account_metrics.
        assert result["def456"].extra == {"watch_time": 0.0, "avg_view_pct": 0.0}
        assert result["def456"].shares == 0

    @patch.object(YouTubeProvider, "_request")
    def test_none_columns_are_skipped(self, mock_request):
        # Analytics returns ``None`` when a metric isn't reportable for a row
        # (e.g. shares disabled for a video). Skip the key entirely — not 0.
        mock_request.return_value = _make_response(
            {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "estimatedMinutesWatched"},
                    {"name": "averageViewPercentage"},
                    {"name": "shares"},
                ],
                "rows": [
                    ["abc123", 100.0, None, None],
                ],
            }
        )

        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", ["abc123"], _date_range())

        assert result["abc123"].extra == {"watch_time": 100.0}
        # ``None`` shares falls back to the dataclass default of 0 (and
        # ``_post_metrics_to_dict`` skips writing zero-valued shares rows).
        assert result["abc123"].shares == 0

    @patch.object(YouTubeProvider, "_request")
    def test_empty_post_ids_skips_request(self, mock_request):
        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", [], _date_range())

        assert result == {}
        mock_request.assert_not_called()

    @patch.object(YouTubeProvider, "_request")
    def test_chunks_over_filter_cap(self, mock_request):
        # YouTube caps `filters=video==<list>` at 500 IDs. Inputs above that
        # are split into multiple requests and their results merged.
        chunk = _ANALYTICS_VIDEO_FILTER_CHUNK
        post_ids = [f"v{i}" for i in range(chunk + 3)]

        def fake_request(*args, **kwargs):
            ids = kwargs["params"]["filters"].removeprefix("video==").split(",")
            return _make_response(
                {
                    "columnHeaders": [
                        {"name": "video"},
                        {"name": "estimatedMinutesWatched"},
                        {"name": "averageViewPercentage"},
                        {"name": "shares"},
                    ],
                    "rows": [[vid, 1.0, 1.0, 1.0] for vid in ids],
                }
            )

        mock_request.side_effect = fake_request
        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", post_ids, _date_range())

        assert mock_request.call_count == 2
        assert len(result) == chunk + 3

        first_filter = mock_request.call_args_list[0].kwargs["params"]["filters"]
        second_filter = mock_request.call_args_list[1].kwargs["params"]["filters"]
        assert len(first_filter.removeprefix("video==").split(",")) == chunk
        assert len(second_filter.removeprefix("video==").split(",")) == 3

    @patch.object(YouTubeProvider, "_request")
    def test_deadline_stops_before_the_next_filter_chunk(self, mock_request):
        chunk = _ANALYTICS_VIDEO_FILTER_CHUNK
        post_ids = [f"v{i}" for i in range(chunk + 3)]
        mock_request.return_value = _make_response({"columnHeaders": [], "rows": []})
        before_deadline = datetime(2026, 1, 1, tzinfo=UTC)
        after_deadline = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)

        with patch("providers.youtube.datetime") as mocked_datetime:
            mocked_datetime.now.side_effect = [before_deadline, after_deadline]
            provider = YouTubeProvider()
            result = provider.get_post_analytics(
                "token",
                post_ids,
                _date_range(),
                deadline=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            )

        assert result == {}
        assert mock_request.call_count == 1

    @patch.object(YouTubeProvider, "_request")
    def test_empty_rows_returns_empty_dict(self, mock_request):
        # API returns no rows when no videos have analytics data in the window.
        mock_request.return_value = _make_response(
            {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "estimatedMinutesWatched"},
                ],
                "rows": [],
            }
        )

        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", ["abc"], _date_range())

        assert result == {}

    @patch.object(YouTubeProvider, "_request")
    def test_row_with_no_extra_is_omitted(self, mock_request):
        # If every metric column came back as None, the video should be
        # absent from the result — callers treat absence as "no data".
        mock_request.return_value = _make_response(
            {
                "columnHeaders": [
                    {"name": "video"},
                    {"name": "estimatedMinutesWatched"},
                    {"name": "averageViewPercentage"},
                    {"name": "shares"},
                ],
                "rows": [
                    ["abc123", None, None, None],
                    ["def456", 50.0, None, None],
                ],
            }
        )

        provider = YouTubeProvider()
        result = provider.get_post_analytics("token", ["abc123", "def456"], _date_range())

        assert set(result.keys()) == {"def456"}
        assert result["def456"].extra == {"watch_time": 50.0}


class TestErrorClassification:
    """Google reports a spent quota as 403, not 429.

    The base class only ever looked at the status code, so every quota failure
    arrived as a generic ``APIError`` — indistinguishable from a permission
    refusal. That is what let an exhausted quota tell healthy accounts to
    reconnect, and what let the analytics sync keep hammering an API that had
    already said "not until tomorrow".
    """

    @staticmethod
    def _error(status: int, payload: dict, *, url: str = f"{API_BASE}/videos") -> MagicMock:
        resp = MagicMock()
        resp.status_code = status
        resp.url = url
        resp.headers = {}
        resp.text = json.dumps(payload)
        resp.json = MagicMock(return_value=payload)
        return resp

    _QUOTA_BODY = {
        "error": {
            "code": 403,
            "message": 'The request cannot be completed because you have exceeded your <a href="/youtube/v3/getting-started#quota">quota</a>.',
            "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
        }
    }

    def test_quota_exceeded_403_raises_quota_exceeded_error(self):
        # A pinned clock, injected rather than patched: provider and assertion
        # then share one instant, where two live reads either side of Pacific
        # midnight would be a day apart.
        now = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)

        exc = YouTubeProvider()._error_for_response(self._error(403, self._QUOTA_BODY), now=now)

        assert isinstance(exc, QuotaExceededError)
        # Subclassing RateLimitError is what keeps every existing consumer right.
        assert isinstance(exc, RateLimitError)
        assert exc.quota_scope == "data"
        assert exc.status_code == 403
        # The real helper, not a stand-in: this is the assertion that ties a
        # daily-quota deadline to Pacific midnight at all. Which midnight that
        # is belongs to tests/providers/test_google_errors.py.
        assert exc.resets_at == next_google_quota_reset(now)
        assert exc.resets_at == datetime(2026, 9, 18, 7, 0, tzinfo=UTC)
        # Prose, not the response body: error_messages._is_user_safe rejects
        # anything containing '{"'.
        assert '{"' not in str(exc)
        assert exc.raw_response == self._QUOTA_BODY

    def test_analytics_endpoint_quota_uses_analytics_scope(self):
        exc = YouTubeProvider()._error_for_response(self._error(403, self._QUOTA_BODY, url=f"{ANALYTICS_BASE}/reports"))

        assert exc.quota_scope == "analytics"

    def test_rate_limit_exceeded_gets_a_short_cooldown_not_a_day(self):
        """A per-second throttle must not cost the rest of the day's syncing."""
        body = {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}}
        # Deliberately inside the last five minutes of the Pacific day.
        now = datetime(2026, 9, 17, 6, 56, 29, tzinfo=UTC)

        exc = YouTubeProvider()._error_for_response(self._error(403, body), now=now)

        assert isinstance(exc, QuotaExceededError)
        # The literal five minutes, not ``_THROTTLE_COOLDOWN``: asserting
        # against the constant the code itself uses would hold for whatever
        # value someone later widened it to.
        assert exc.resets_at == now + timedelta(minutes=5)

    def test_a_throttle_cooldown_may_outlast_the_next_daily_reset(self):
        """The window that made the old assertion fail once a day.

        In the last five minutes of the Pacific day the next daily reset is
        *nearer* than a five-minute throttle cooldown. That is correct — a
        burst throttle and a daily budget are different clocks — so nothing
        may assert the cooldown is the earlier of the two.
        """
        body = {"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}}
        now = datetime(2026, 9, 17, 6, 56, 29, tzinfo=UTC)

        exc = YouTubeProvider()._error_for_response(self._error(403, body), now=now)

        assert next_google_quota_reset(now) < exc.resets_at
        # Both are still ahead of the moment that produced them.
        assert now < next_google_quota_reset(now)
        assert now < exc.resets_at

    def test_401_invalid_credentials_raises_token_expired_carrying_status(self):
        """``status_code`` here is load-bearing, not decorative.

        ``engine._is_ambiguous_submission_failure`` reads it through a
        ``getattr`` default of None and treats a missing code as "outcome
        unknown, do not retry" — so dropping it would silently flip a 401 first
        comment from retryable to untouchable.
        """
        body = {
            "error": {
                "code": 401,
                "errors": [{"message": "Invalid Credentials", "reason": "authError"}],
                "status": "UNAUTHENTICATED",
            }
        }

        exc = YouTubeProvider()._error_for_response(self._error(401, body))

        assert isinstance(exc, TokenExpiredError)
        assert exc.status_code == 401

    def test_unauthenticated_status_without_401_still_classifies(self):
        """The Analytics API leans on ``error.status`` rather than a reason."""
        body = {"error": {"code": 403, "status": "UNAUTHENTICATED"}}

        exc = YouTubeProvider()._error_for_response(self._error(403, body, url=f"{ANALYTICS_BASE}/reports"))

        assert isinstance(exc, TokenExpiredError)

    def test_permission_403_still_raises_plain_api_error(self):
        """A scope refusal must keep reaching ``_is_insufficient_scope``."""
        from apps.analytics.tasks import _is_insufficient_scope

        body = {"error": {"code": 403, "message": "Forbidden", "errors": [{"reason": "forbidden"}]}}

        exc = YouTubeProvider()._error_for_response(self._error(403, body))

        assert isinstance(exc, APIError)
        assert not isinstance(exc, QuotaExceededError)
        assert exc.status_code == 403
        assert _is_insufficient_scope(exc)

    def test_429_still_raises_plain_rate_limit_error(self):
        """The base contract an override must preserve."""
        resp = self._error(429, {"error": {"code": 429}})
        resp.headers = {"Retry-After": "30"}

        exc = YouTubeProvider()._error_for_response(resp)

        assert type(exc) is RateLimitError
        assert exc.retry_after == 30

    def test_500_still_raises_plain_api_error(self):
        exc = YouTubeProvider()._error_for_response(self._error(500, {"error": {"code": 500}}))

        assert type(exc) is APIError
        assert exc.status_code == 500


class TestGetPostMetricsBatch:
    """``videos.list`` charges 1 quota unit for 50 ids exactly as for one.

    Asking one at a time spent 50x the quota it needed to, which is what put a
    10,000-unit daily budget within reach of a single misbehaving sync loop.
    """

    @staticmethod
    def _items(video_ids):
        return {"items": [{"id": vid, "statistics": {"viewCount": "10", "likeCount": "2"}} for vid in video_ids]}

    @patch.object(YouTubeProvider, "_request")
    def test_joins_ids_into_one_request(self, mock_request):
        mock_request.return_value = _make_response(self._items(["a", "b", "c"]))

        result = YouTubeProvider().get_post_metrics_batch("tok", ["a", "b", "c"])

        assert mock_request.call_count == 1
        assert mock_request.call_args.kwargs["params"]["id"] == "a,b,c"
        assert set(result) == {"a", "b", "c"}
        assert result["a"].video_views == 10
        assert result["a"].engagements == 2

    @patch.object(YouTubeProvider, "_request")
    def test_chunks_at_fifty(self, mock_request):
        ids = [f"v{i}" for i in range(120)]
        mock_request.side_effect = lambda *a, **kw: _make_response(self._items(kw["params"]["id"].split(",")))

        result = YouTubeProvider().get_post_metrics_batch("tok", ids)

        assert mock_request.call_count == 3  # 50 + 50 + 20
        assert len(result) == 120

    @patch.object(YouTubeProvider, "_request")
    def test_ids_the_api_omits_are_absent_not_zero(self, mock_request):
        """A deleted video must not overwrite its own history with a flat line."""
        mock_request.return_value = _make_response(self._items(["a"]))

        result = YouTubeProvider().get_post_metrics_batch("tok", ["a", "deleted"])

        assert "deleted" not in result

    @patch.object(YouTubeProvider, "_request")
    def test_empty_input_makes_no_request(self, mock_request):
        assert YouTubeProvider().get_post_metrics_batch("tok", []) == {}
        assert mock_request.call_count == 0

    @patch.object(YouTubeProvider, "_request")
    def test_singular_delegates_to_batch(self, mock_request):
        mock_request.return_value = _make_response(self._items(["a"]))

        metrics = YouTubeProvider().get_post_metrics("tok", "a")

        assert metrics.video_views == 10
        assert mock_request.call_args.kwargs["params"]["id"] == "a"

    @patch.object(YouTubeProvider, "_request")
    def test_singular_returns_empty_metrics_when_absent(self, mock_request):
        """The contract the single-post sync path has always relied on."""
        mock_request.return_value = _make_response({"items": []})

        metrics = YouTubeProvider().get_post_metrics("tok", "gone")

        assert metrics.video_views == 0
        assert metrics.likes == 0
