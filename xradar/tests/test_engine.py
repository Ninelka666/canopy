import unittest
from datetime import datetime, timedelta, timezone

from xradar import config as cfgmod
from xradar.config import Config, ConfigError, Rule, Topic
from xradar.engine import BudgetExceeded, Engine, gate_for, target_views
from xradar.model import build_post
from xradar.sources.base import Source, SourceError, build_query
from xradar.store import Store

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


class ScriptedSource(Source):
    """Returns whatever the test hands it, stamped with the current clock."""

    name = "scripted"
    cost_per_post = 0.001

    def __init__(self, clock, batches):
        self.clock = clock
        self.batches = list(batches)
        self.queries = []

    def search(self, query, since, limit):
        self.queries.append(query)
        batch = self.batches.pop(0) if self.batches else []
        from dataclasses import replace
        return [replace(p, fetched_at=self.clock()) for p in batch][:limit]


class FailingSource(Source):
    name = "failing"

    def search(self, query, since, limit):
        raise SourceError("upstream is down")


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send_alert(self, post, metrics, rules, history=()):
        self.sent.append((post.id, tuple(rules), metrics.views, tuple(history)))


class ExplodingNotifier:
    def send_alert(self, *_a, **_kw):
        raise RuntimeError("channel down")


def make_post(views, minutes_old, clock, *, pid="p1", followers=10_000, likes=None):
    return build_post(
        id=pid, text="t", author_handle="acct", author_followers=followers,
        created_at=clock() - timedelta(minutes=minutes_old),
        fetched_at=clock(), source="scripted", views=views,
        likes=likes if likes is not None else views // 300,
    )


def make_config(**overrides):
    base = dict(
        source="scripted", db_path=":memory:", interval_sec=60, lookback_min=150,
        track_max_age_min=360, max_fetch_per_run=100, daily_fetch_budget=1000,
        views_per_like=300.0, velocity_min_gap_sec=150,
        renotify_growth_factor=3.0,
        topics=[Topic(name="t1", query="topic one")],
        rules=[Rule(name="flying", max_age_min=90, min_views=100_000)],
    )
    base.update(overrides)
    return Config(**base)


class TestGate(unittest.TestCase):
    def test_gate_derives_from_the_cheapest_rule(self):
        rules = [Rule(name="a", min_views=100_000), Rule(name="b", min_views=40_000)]
        self.assertEqual(target_views(rules), 40_000)
        topic = Topic(name="t", query="q")
        self.assertEqual(gate_for(topic, rules, 300.0), 133)

    def test_velocity_rule_contributes_a_view_target(self):
        rules = [Rule(name="v", min_vph=50_000, max_age_min=180)]
        self.assertEqual(target_views(rules), 150_000)

    def test_topic_override_wins(self):
        topic = Topic(name="t", query="q", min_faves=1000)
        self.assertEqual(gate_for(topic, [Rule(name="a", min_views=1)], 300.0), 1000)

    def test_query_carries_the_gate_and_exclusions(self):
        q = build_query("ai", min_faves=333, lang="en", since=NOW)
        self.assertIn("min_faves:333", q)
        self.assertIn("lang:en", q)
        self.assertIn("-filter:replies", q)
        self.assertIn(f"since_time:{int(NOW.timestamp())}", q)

    def test_official_api_operator_name_differs(self):
        q = build_query("ai", min_faves=333, likes_operator="min_likes",
                        include_since=False)
        self.assertIn("min_likes:333", q)
        self.assertNotIn("since_time", q)


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")
        self.notifier = RecordingNotifier()

    def tearDown(self):
        self.store.close()

    def engine(self, batches, config=None):
        source = ScriptedSource(self.clock, batches)
        cfg = config or make_config()
        return Engine(cfg, self.store, source, [self.notifier], now_fn=self.clock)

    def test_matching_post_alerts_once_then_is_suppressed(self):
        first = [make_post(120_000, 40, self.clock)]
        engine = self.engine([first, first])
        found = engine.scan_topic(engine.config.topics[0])
        self.assertEqual([f.rules for f in found], [["flying"]])
        self.assertEqual(len(self.notifier.sent), 1)

        self.clock.advance(5)
        again = engine.scan_topic(engine.config.topics[0])
        self.assertEqual([f.rules for f in again], [["flying"]])
        # Still matching, but already announced.
        self.assertEqual(len(self.notifier.sent), 1)

    def test_big_growth_renotifies(self):
        engine = self.engine([
            [make_post(120_000, 40, self.clock)],
            [make_post(500_000, 45, self.clock)],
        ])
        engine.scan_topic(engine.config.topics[0])
        self.clock.advance(5)
        engine.scan_topic(engine.config.topics[0])
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.notifier.sent[1][2], 500_000)

    def test_velocity_accumulates_across_passes(self):
        engine = self.engine([
            [make_post(60_000, 30, self.clock)],
            [make_post(200_000, 40, self.clock)],
        ])
        engine.scan_topic(engine.config.topics[0])
        self.clock.advance(10)
        findings = engine.scan_topic(engine.config.topics[0])
        metrics = findings[0].metrics
        self.assertIsNotNone(metrics.vph_now)
        self.assertAlmostEqual(metrics.vph_now, 840_000, delta=1_000)
        self.assertGreater(metrics.accel, 1.0)

    def test_snapshot_history_reaches_the_notifier(self):
        engine = self.engine([
            [make_post(120_000, 40, self.clock)],
            [make_post(600_000, 50, self.clock)],
        ])
        engine.scan_topic(engine.config.topics[0])
        self.clock.advance(10)
        engine.scan_topic(engine.config.topics[0])
        self.assertEqual(self.notifier.sent[-1][3], (120_000, 600_000))

    def test_non_matching_posts_are_stored_but_not_alerted(self):
        engine = self.engine([[make_post(5_000, 40, self.clock)]])
        self.assertEqual(engine.scan_topic(engine.config.topics[0]), [])
        self.assertEqual(self.notifier.sent, [])
        # Still tracked, so its velocity can be measured if it picks up.
        self.assertEqual(len(self.store.tracked_posts(360)), 1)

    def test_posts_beyond_the_tracking_window_are_dropped(self):
        engine = self.engine([[make_post(500_000, 500, self.clock)]])
        self.assertEqual(engine.scan_topic(engine.config.topics[0]), [])
        self.assertEqual(self.store.tracked_posts(360), [])

    def test_replies_are_filtered_client_side(self):
        from dataclasses import replace
        reply = replace(make_post(500_000, 30, self.clock), is_reply=True)
        engine = self.engine([[reply]])
        self.assertEqual(engine.scan_topic(engine.config.topics[0]), [])

    def test_dry_run_sends_nothing_and_records_nothing(self):
        engine = self.engine([[make_post(120_000, 40, self.clock)],
                              [make_post(120_000, 45, self.clock)]])
        found = engine.scan_topic(engine.config.topics[0], dry_run=True)
        self.assertEqual(len(found), 1)
        self.assertEqual(self.notifier.sent, [])
        # A dry run must not consume the alert, so a real pass still fires.
        self.clock.advance(5)
        engine.scan_topic(engine.config.topics[0])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_budget_exhaustion_stops_scanning(self):
        cfg = make_config(daily_fetch_budget=1)
        engine = self.engine([[make_post(120_000, 40, self.clock)]] * 3, config=cfg)
        engine.scan_topic(cfg.topics[0])
        with self.assertRaises(BudgetExceeded):
            engine.scan_topic(cfg.topics[0])

    def test_fetch_count_is_recorded_for_cost_tracking(self):
        engine = self.engine([[make_post(1_000, 40, self.clock, pid=f"p{i}")
                               for i in range(7)]])
        engine.scan_topic(engine.config.topics[0])
        midnight = self.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(self.store.fetched_since(midnight), 7)

    def test_one_failing_topic_does_not_stop_the_others(self):
        cfg = make_config(topics=[Topic(name="bad", query="q"),
                                  Topic(name="good", query="q")])
        source = FailingSource()
        engine = Engine(cfg, self.store, source, [self.notifier], now_fn=self.clock)
        findings, errors = engine.scan_all()
        self.assertEqual(findings, [])
        self.assertEqual([name for name, _ in errors], ["bad", "good"])

    def test_source_failure_is_recorded_on_the_run(self):
        cfg = make_config()
        engine = Engine(cfg, self.store, FailingSource(), [], now_fn=self.clock)
        with self.assertRaises(SourceError):
            engine.scan_topic(cfg.topics[0])
        row = self.store.conn.execute("SELECT error FROM runs").fetchone()
        self.assertIn("upstream is down", row["error"])

    def test_a_dead_notifier_does_not_lose_the_scan(self):
        source = ScriptedSource(self.clock, [[make_post(120_000, 40, self.clock)]])
        engine = Engine(make_config(), self.store, source,
                        [ExplodingNotifier(), self.notifier], now_fn=self.clock)
        found = engine.scan_topic(engine.config.topics[0])
        self.assertEqual(len(found), 1)
        # The working channel still received it.
        self.assertEqual(len(self.notifier.sent), 1)

    def test_disabled_topics_are_skipped(self):
        cfg = make_config(topics=[Topic(name="off", query="q", enabled=False)])
        source = ScriptedSource(self.clock, [[make_post(500_000, 10, self.clock)]])
        engine = Engine(cfg, self.store, source, [self.notifier], now_fn=self.clock)
        findings, errors = engine.scan_all()
        self.assertEqual((findings, errors), ([], []))
        self.assertEqual(source.queries, [])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_previous_snapshot_respects_the_minimum_gap(self):
        self.store.record(make_post(1_000, 30, self.clock))
        self.clock.advance(1)
        self.store.record(make_post(2_000, 31, self.clock))
        # One minute apart is inside the 150s gap: too noisy to use.
        self.assertIsNone(self.store.previous_snapshot(
            "p1", self.clock(), min_gap_sec=150))
        self.clock.advance(10)
        self.store.record(make_post(9_000, 41, self.clock))
        row = self.store.previous_snapshot("p1", self.clock(), min_gap_sec=150)
        self.assertEqual(row["views"], 2_000)

    def test_author_baseline_needs_history(self):
        for i in range(2):
            self.store.record(make_post(10_000, 60, self.clock, pid=f"old{i}"))
            self.clock.advance(1)
        self.assertIsNone(self.store.author_baseline("acct", "p1"))
        self.store.record(make_post(10_000, 60, self.clock, pid="old3"))
        self.assertIsNotNone(self.store.author_baseline("acct", "p1"))

    def test_baseline_is_a_median_not_an_average(self):
        for i, views in enumerate([1_000, 2_000, 3_000, 400_000]):
            self.store.record(make_post(views, 60, self.clock, pid=f"h{i}"))
        baseline = self.store.author_baseline("acct", "p1")
        # Median of the four rates, so the one outlier does not set the bar.
        self.assertLess(baseline, 10_000)

    def test_prune_removes_old_posts_and_their_snapshots(self):
        self.store.record(make_post(1_000, 60 * 24 * 30, self.clock, pid="ancient"))
        self.store.record(make_post(1_000, 10, self.clock, pid="recent"))
        self.assertEqual(self.store.prune(older_than_days=14), 1)
        rows = self.store.conn.execute("SELECT id FROM posts").fetchall()
        self.assertEqual([r["id"] for r in rows], ["recent"])
        remaining = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots").fetchone()
        self.assertEqual(remaining["n"], 1)


class TestConfig(unittest.TestCase):
    def test_env_interpolation(self):
        import os
        os.environ["XRADAR_TEST_TOKEN"] = "abc123"
        cfg = cfgmod.from_dict({
            "telegram_bot_token": "${XRADAR_TEST_TOKEN}",
            "topics": [{"name": "t", "query": "q"}],
        })
        self.assertEqual(cfg.telegram_bot_token, "abc123")

    def test_unset_variable_becomes_empty(self):
        cfg = cfgmod.from_dict({"telegram_bot_token": "${XRADAR_NOT_SET_ANYWHERE}"})
        self.assertEqual(cfg.telegram_bot_token, "")

    def test_typo_in_a_field_is_rejected(self):
        with self.assertRaises(ConfigError):
            cfgmod.from_dict({"intervall_sec": 60})
        with self.assertRaises(ConfigError):
            cfgmod.from_dict({"rules": [{"name": "r", "min_viewz": 1}]})

    def test_duplicate_rule_names_are_rejected(self):
        with self.assertRaises(ConfigError):
            cfgmod.from_dict({"rules": [{"name": "r"}, {"name": "r"}]})

    def test_topic_referencing_an_unknown_rule_is_rejected(self):
        cfg = cfgmod.from_dict({
            "rules": [{"name": "known"}],
            "topics": [{"name": "t", "query": "q", "rules": ["ghost"]}],
        })
        with self.assertRaises(ConfigError):
            cfg.rules_for(cfg.topics[0])

    def test_topic_needs_a_query(self):
        with self.assertRaises(ConfigError):
            cfgmod.from_dict({"topics": [{"name": "t", "query": ""}]})

    def test_defaults_include_the_three_starter_rules(self):
        cfg = cfgmod.from_dict({})
        self.assertEqual([r.name for r in cfg.rules],
                         ["flying_1h", "escape_velocity", "accelerating"])


if __name__ == "__main__":
    unittest.main()
