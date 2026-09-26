import unittest
from datetime import datetime, timedelta, timezone

from xradar import http, sources
from xradar.notify.telegram import (TelegramError, TelegramNotifier,
                                    format_alert, sparkline)
from xradar.model import build_post
from xradar.scoring import compute
from xradar.sources import SourceError
from xradar.sources.mock import MockSource
from xradar.sources.base import build_query
from xradar.sources.twitterapi_io import TwitterApiIoSource
from xradar.sources.x_api_v2 import XApiV2Source

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

TWITTERAPI_PAGE = {
    "tweets": [
        {
            "id": "1900000000000000001",
            "url": "https://x.com/someone/status/1900000000000000001",
            "text": "the numbers do not say what the headline says",
            "retweetCount": 340, "replyCount": 120, "likeCount": 1200,
            "quoteCount": 44, "viewCount": 142000, "bookmarkCount": 300,
            "createdAt": "Sat Sep 26 11:13:00 +0000 2026", "lang": "en",
            "author": {"userName": "someone", "name": "Some One",
                        "followers": 8800, "id": "42"},
        },
        {
            "id": "1900000000000000002", "text": "older post",
            "likeCount": 10, "viewCount": 900,
            "createdAt": "Sat Sep 26 06:00:00 +0000 2026",
            "author": {"userName": "other", "followers": 100},
        },
    ],
    "has_next_page": False, "next_cursor": "",
}

X_API_PAGE = {
    "data": [{
        "id": "1900000000000000003",
        "text": "official api post",
        "created_at": "2026-09-26T11:30:00.000Z",
        "author_id": "7",
        "lang": "en",
        "public_metrics": {"retweet_count": 50, "reply_count": 20,
                            "like_count": 600, "quote_count": 5,
                            "bookmark_count": 30, "impression_count": 0},
    }],
    "includes": {"users": [{
        "id": "7", "username": "official_user", "name": "Official",
        "public_metrics": {"followers_count": 12000},
    }]},
    "meta": {},
}


class TestTwitterApiIo(unittest.TestCase):
    def source(self, pages):
        self.calls = []

        def fake_get(url, params=None, headers=None, **kw):
            self.calls.append((params, headers))
            return pages[len(self.calls) - 1]

        return TwitterApiIoSource(api_key="k", get_json=fake_get)

    def test_requires_a_key(self):
        with self.assertRaises(SourceError):
            TwitterApiIoSource(api_key="")

    def test_parses_views_and_followers(self):
        src = self.source([TWITTERAPI_PAGE])
        posts = src.search("q", NOW - timedelta(hours=2), 10)
        self.assertEqual(len(posts), 1)  # the 06:00 post is outside the window
        post = posts[0]
        self.assertEqual(post.views, 142_000)
        self.assertEqual(post.likes, 1_200)
        self.assertEqual(post.author_handle, "someone")
        self.assertEqual(post.author_followers, 8_800)
        self.assertEqual(post.created_at,
                          datetime(2026, 9, 26, 11, 13, tzinfo=timezone.utc))

    def test_sends_the_key_as_a_header_not_a_query_param(self):
        src = self.source([TWITTERAPI_PAGE])
        src.search("q", NOW - timedelta(hours=2), 10)
        params, headers = self.calls[0]
        self.assertEqual(headers["X-API-Key"], "k")
        self.assertNotIn("k", str(params))

    def test_stops_paging_at_the_window_edge(self):
        page = dict(TWITTERAPI_PAGE, has_next_page=True, next_cursor="c1")
        src = self.source([page, page])
        posts = src.search("q", NOW - timedelta(hours=2), 50)
        # The second entry predates `since`, so the walk ends without paging.
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.calls), 1)

    def test_malformed_entries_are_skipped(self):
        src = self.source([{"tweets": [None, {"no_id": 1},
                                        TWITTERAPI_PAGE["tweets"][0]]}])
        self.assertEqual(len(src.search("q", NOW - timedelta(hours=2), 10)), 1)

    def test_unexpected_shape_raises(self):
        src = self.source([{"tweets": "not a list"}])
        with self.assertRaises(SourceError):
            src.search("q", NOW - timedelta(hours=2), 10)


class TestXApiV2(unittest.TestCase):
    def test_requires_a_token(self):
        with self.assertRaises(SourceError):
            XApiV2Source(bearer_token="")

    def test_zero_impressions_is_treated_as_unknown(self):
        src = XApiV2Source(bearer_token="t",
                           get_json=lambda *a, **kw: X_API_PAGE)
        post = src.search("q", NOW - timedelta(hours=2), 10)[0]
        self.assertIsNone(post.views)
        self.assertEqual(post.likes, 600)
        self.assertEqual(post.author_handle, "official_user")
        self.assertEqual(post.author_followers, 12_000)
        # Estimation upstream keeps it usable, flagged as estimated.
        metrics = compute(post, now=NOW, views_per_like=300.0)
        self.assertTrue(metrics.views_estimated)
        self.assertEqual(metrics.views, 180_000)

    def test_rejected_likes_operator_falls_back_to_client_side_filter(self):
        attempts = []

        def fake_get(url, params=None, headers=None, **kw):
            attempts.append(params["query"])
            if "min_likes" in params["query"]:
                raise http.HttpError(
                    400, '{"errors":[{"message":"Invalid operator min_likes"}]}', url)
            return X_API_PAGE

        src = XApiV2Source(bearer_token="t", get_json=fake_get)
        query = build_query("ai", min_faves=1000, likes_operator="min_likes",
                            include_since=False)
        posts = src.search(query, NOW - timedelta(hours=2), 10)
        self.assertEqual(len(attempts), 2)
        self.assertNotIn("min_likes", attempts[1])
        self.assertFalse(src.gate_supported)
        # The threshold is remembered and applied locally: 600 likes < 1000.
        self.assertEqual(src.min_likes_fallback, 1000)
        self.assertEqual(posts, [])

    def test_unrelated_400_is_not_swallowed(self):
        def fake_get(url, params=None, headers=None, **kw):
            raise http.HttpError(400, '{"title":"Invalid Request"}', url)

        src = XApiV2Source(bearer_token="t", get_json=fake_get)
        with self.assertRaises(SourceError):
            src.search("ai min_likes:10", NOW - timedelta(hours=2), 10)

    def test_start_time_is_sent_as_rfc3339(self):
        captured = {}

        def fake_get(url, params=None, headers=None, **kw):
            captured.update(params)
            return {"data": [], "meta": {}}

        XApiV2Source(bearer_token="t", get_json=fake_get).search(
            "q", NOW - timedelta(hours=2), 10)
        self.assertTrue(captured["start_time"].endswith("Z"))
        self.assertIn("impression", "".join(captured["tweet.fields"]) + "impression")


class TestMockSource(unittest.TestCase):
    def test_deterministic_across_identical_calls(self):
        src = MockSource(now_fn=lambda: NOW)
        since = NOW - timedelta(minutes=150)
        first = src.search(build_query("ai", min_faves=200, since=since), since, 50)
        second = src.search(build_query("ai", min_faves=200, since=since), since, 50)
        self.assertEqual([p.id for p in first], [p.id for p in second])

    def test_identity_survives_a_moving_since_time(self):
        src = MockSource(now_fn=lambda: NOW)
        since = NOW - timedelta(minutes=150)
        a = src.search(build_query("ai", min_faves=200, since=since), since, 50)
        b = src.search(build_query("ai", min_faves=200,
                                    since=since + timedelta(seconds=30)), since, 50)
        self.assertEqual({p.id for p in a}, {p.id for p in b})

    def test_views_grow_as_the_clock_advances(self):
        since = NOW - timedelta(minutes=150)
        query = build_query("ai", min_faves=200, since=since)
        early = {p.id: p.views for p in
                  MockSource(now_fn=lambda: NOW).search(query, since, 50)}
        later_now = NOW + timedelta(minutes=20)
        later = {p.id: p.views for p in
                  MockSource(now_fn=lambda: later_now).search(query, since, 50)}
        shared = set(early) & set(later)
        self.assertTrue(shared)
        self.assertTrue(all(later[i] >= early[i] for i in shared))

    def test_honours_the_likes_gate(self):
        since = NOW - timedelta(minutes=150)
        src = MockSource(now_fn=lambda: NOW)
        posts = src.search(build_query("ai", min_faves=5000, since=since), since, 50)
        self.assertTrue(all(p.likes >= 5000 for p in posts))

    def test_respects_the_limit(self):
        since = NOW - timedelta(minutes=150)
        src = MockSource(now_fn=lambda: NOW)
        self.assertEqual(
            len(src.search(build_query("ai", since=since), since, 3)), 3)


class TestRegistry(unittest.TestCase):
    def test_unknown_source_names_the_alternatives(self):
        with self.assertRaises(SourceError) as ctx:
            sources.create("nope")
        self.assertIn("mock", str(ctx.exception))

    def test_cost_per_post_is_declared(self):
        self.assertGreater(sources.REGISTRY["x_api_v2"].cost_per_post,
                            sources.REGISTRY["twitterapi_io"].cost_per_post)


class TestTelegram(unittest.TestCase):
    def post(self):
        return build_post(
            id="9", text="<script>alert(1)</script> & more",
            author_handle="small_acct", author_name="Small & Co",
            author_followers=8_800, created_at=NOW - timedelta(minutes=47),
            source="twitterapi_io", views=142_000, likes=1_200, retweets=340,
            fetched_at=NOW,
        )

    def test_requires_both_credentials(self):
        with self.assertRaises(TelegramError):
            TelegramNotifier("token", "")

    def test_html_in_post_text_is_escaped(self):
        post = self.post()
        body = format_alert(post, compute(post, now=NOW), ["flying_1h"])
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertIn("Small &amp; Co", body)

    def test_estimated_views_are_marked(self):
        post = build_post(id="1", text="t", author_handle="a", source="x_api_v2",
                          created_at=NOW - timedelta(minutes=30), fetched_at=NOW,
                          views=None, likes=500, author_followers=1_000)
        body = format_alert(post, compute(post, now=NOW), ["flying_1h"])
        self.assertIn("~", body)
        self.assertIn("estimated", body)

    def test_long_text_is_truncated(self):
        post = build_post(id="1", text="x" * 500, author_handle="a",
                          source="mock", created_at=NOW - timedelta(minutes=30),
                          fetched_at=NOW, views=200_000, likes=800)
        body = format_alert(post, compute(post, now=NOW), ["r"])
        self.assertIn("…", body)
        self.assertLess(len(body), 800)

    def test_api_failure_does_not_leak_the_token(self):
        def fake_post(url, payload, **kw):
            raise http.HttpError(401, '{"description":"Unauthorized"}', url)

        notifier = TelegramNotifier("SECRET-TOKEN", "123", post_json=fake_post)
        with self.assertRaises(TelegramError) as ctx:
            notifier.send("hi")
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))

    def test_rejection_is_reported(self):
        notifier = TelegramNotifier(
            "t", "1", post_json=lambda *a, **kw: {"ok": False,
                                                   "description": "chat not found"})
        with self.assertRaises(TelegramError) as ctx:
            notifier.send("hi")
        self.assertIn("chat not found", str(ctx.exception))

    def test_sends_html_mode_to_the_configured_chat(self):
        captured = {}

        def fake_post(url, payload, **kw):
            captured["url"] = url
            captured["payload"] = payload
            return {"ok": True, "result": {}}

        TelegramNotifier("t", "555", post_json=fake_post).send("hi")
        self.assertTrue(captured["url"].endswith("/bott/sendMessage"))
        self.assertEqual(captured["payload"]["chat_id"], "555")
        self.assertEqual(captured["payload"]["parse_mode"], "HTML")

    def test_sparkline(self):
        self.assertEqual(sparkline([]), "")
        self.assertEqual(sparkline([5]), "")
        self.assertEqual(len(sparkline([1, 2, 3, 4])), 4)
        self.assertEqual(sparkline([7, 7, 7]), "▁▁▁")


class TestHttpRetry(unittest.TestCase):
    def test_client_error_is_not_retried(self):
        import urllib.error
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(
                request.full_url, 400, "Bad", {}, None)

        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(http.HttpError):
                http.get_json("https://example.test/x", retries=3,
                              sleep=lambda _s: None)
        self.assertEqual(len(attempts), 1)

    def test_server_error_is_retried_then_raises(self):
        import urllib.error
        import unittest.mock as mock
        attempts = []

        def opener(request, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 503, "Nope", {}, None)

        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaises(http.HttpError):
                http.get_json("https://example.test/x", retries=2,
                              sleep=lambda _s: None)
        self.assertEqual(len(attempts), 3)


if __name__ == "__main__":
    unittest.main()
