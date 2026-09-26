import unittest
from datetime import datetime, timedelta, timezone

from xradar.config import DEFAULT_RULES, Rule
from xradar.model import build_post, parse_timestamp
from xradar.scoring import (compute, estimate_views, evaluate, humanize,
                            matches, suggested_min_faves)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


def post(**kw):
    defaults = dict(id="1", text="t", author_handle="a", source="mock",
                    created_at=NOW - timedelta(minutes=50), fetched_at=NOW,
                    author_followers=10_000)
    defaults.update(kw)
    return build_post(**defaults)


class TestTimestamps(unittest.TestCase):
    def test_legacy_twitter_format(self):
        self.assertEqual(
            parse_timestamp("Tue Dec 10 07:00:30 +0000 2024"),
            datetime(2024, 12, 10, 7, 0, 30, tzinfo=timezone.utc),
        )

    def test_iso_and_epoch_forms(self):
        expected = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_timestamp("2026-09-26T10:00:00.000Z"), expected)
        self.assertEqual(parse_timestamp(expected.timestamp()), expected)
        self.assertEqual(parse_timestamp(expected.timestamp() * 1000), expected)

    def test_naive_input_is_treated_as_utc(self):
        self.assertEqual(
            parse_timestamp("2026-09-26T10:00:00").tzinfo, timezone.utc
        )


class TestEstimation(unittest.TestCase):
    def test_real_views_are_used_as_is(self):
        self.assertEqual(estimate_views(5000, 10, 300.0), (5000, False))

    def test_missing_views_fall_back_to_likes(self):
        self.assertEqual(estimate_views(None, 10, 300.0), (3000, True))

    def test_zero_views_is_treated_as_missing(self):
        # The official API reports 0 impressions routinely; a post with likes
        # cannot truly have zero views.
        self.assertEqual(estimate_views(0, 10, 300.0), (3000, True))


class TestMetrics(unittest.TestCase):
    def test_average_velocity(self):
        m = compute(post(views=100_000), now=NOW)
        self.assertAlmostEqual(m.vph_avg, 120_000, delta=1)
        self.assertIsNone(m.vph_now)
        self.assertIsNone(m.accel)

    def test_current_velocity_from_previous_snapshot(self):
        m = compute(post(views=150_000), now=NOW, prev_views=100_000,
                    prev_ts=NOW - timedelta(minutes=10))
        self.assertAlmostEqual(m.vph_now, 300_000, delta=1)
        self.assertEqual(m.delta_views, 50_000)

    def test_counters_stepping_backwards_do_not_go_negative(self):
        m = compute(post(views=90_000), now=NOW, prev_views=100_000,
                    prev_ts=NOW - timedelta(minutes=10))
        self.assertEqual(m.delta_views, 0)
        self.assertEqual(m.vph_now, 0.0)

    def test_acceleration_distinguishes_climbing_from_stalled(self):
        climbing = compute(post(views=150_000), now=NOW, prev_views=60_000,
                           prev_ts=NOW - timedelta(minutes=6))
        stalled = compute(post(views=302_000), now=NOW, prev_views=300_000,
                          prev_ts=NOW - timedelta(minutes=6))
        self.assertGreater(climbing.accel, 1.0)
        self.assertLess(stalled.accel, 1.0)

    def test_reach_multiple_separates_small_accounts(self):
        big = compute(post(views=142_000, author_followers=900_000), now=NOW)
        small = compute(post(views=142_000, author_followers=8_000), now=NOW)
        self.assertLess(big.reach_multiple, 1.0)
        self.assertGreater(small.reach_multiple, 10.0)
        self.assertGreater(small.score, big.score)

    def test_author_outlier_uses_baseline(self):
        m = compute(post(views=120_000), now=NOW, baseline_vph=10_000)
        self.assertAlmostEqual(m.author_outlier, 14.4, delta=0.1)

    def test_very_fresh_post_is_not_infinitely_fast(self):
        m = compute(post(created_at=NOW, views=1_000), now=NOW)
        self.assertEqual(m.age_min, 0.5)
        self.assertAlmostEqual(m.vph_avg, 120_000, delta=1)

    def test_estimated_views_are_discounted_in_score(self):
        real = compute(post(views=150_000, likes=500), now=NOW)
        est = compute(post(views=None, likes=500), now=NOW, views_per_like=300.0)
        self.assertTrue(est.views_estimated)
        self.assertLess(est.score, real.score)


class TestRules(unittest.TestCase):
    def setUp(self):
        self.rules = [Rule.from_dict(r) for r in DEFAULT_RULES]

    def test_the_literal_ask_fires(self):
        p = post(views=142_000, likes=1_200,
                 created_at=NOW - timedelta(minutes=47))
        self.assertIn("flying_1h", evaluate(self.rules, p, compute(p, now=NOW)))

    def test_too_old_does_not_fire(self):
        p = post(views=142_000, likes=1_200,
                 created_at=NOW - timedelta(minutes=200))
        self.assertNotIn("flying_1h", evaluate(self.rules, p, compute(p, now=NOW)))

    def test_missing_metric_fails_the_condition(self):
        # No previous snapshot means no vph_now, so a rule requiring it must
        # not fire on the strength of the other conditions.
        rule = Rule(name="r", min_vph_now=1000)
        p = post(views=500_000)
        self.assertFalse(matches(rule, p, compute(p, now=NOW)))

    def test_rule_can_refuse_estimated_views(self):
        rule = Rule(name="r", min_views=1000, allow_estimated_views=False)
        p = post(views=None, likes=100)
        self.assertFalse(matches(rule, p, compute(p, now=NOW)))

    def test_max_followers_excludes_mega_accounts(self):
        rule = Rule(name="r", min_views=1000, max_followers=100_000)
        small = post(views=200_000, author_followers=5_000)
        big = post(views=200_000, author_followers=5_000_000)
        self.assertTrue(matches(rule, small, compute(small, now=NOW)))
        self.assertFalse(matches(rule, big, compute(big, now=NOW)))


class TestHelpers(unittest.TestCase):
    def test_likes_gate_for_a_view_target(self):
        self.assertEqual(suggested_min_faves(100_000, 300.0), 333)
        self.assertEqual(suggested_min_faves(1_000, 300.0), 20)  # floor

    def test_humanize(self):
        self.assertEqual(humanize(999), "999")
        self.assertEqual(humanize(1_500), "1.5k")
        self.assertEqual(humanize(2_000_000), "2M")
        self.assertEqual(humanize(None), "-")


if __name__ == "__main__":
    unittest.main()
