"""One scan pass: fetch, snapshot, score, decide, notify."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .config import Config, Rule, Topic
from .model import Post, parse_timestamp
from .scoring import Metrics, compute, evaluate, suggested_min_faves
from .sources import Source, SourceError, build_query
from .store import Store

log = logging.getLogger("xradar")

DEFAULT_TARGET_VIEWS = 100_000


@dataclass
class Finding:
    post: Post
    metrics: Metrics
    rules: List[str]
    notified: bool = False


class BudgetExceeded(Exception):
    """The daily fetch budget is spent; scanning stops rather than billing on."""


def target_views(rules: Sequence[Rule]) -> int:
    """The lowest view count any rule could fire at.

    Used to size the likes gate: gating higher than the cheapest rule needs
    would silently blind that rule.
    """
    targets: List[float] = []
    for rule in rules:
        if rule.min_views:
            targets.append(float(rule.min_views))
        if rule.min_vph and rule.max_age_min:
            targets.append(rule.min_vph * (rule.max_age_min / 60.0))
        elif rule.min_vph_now and rule.max_age_min:
            targets.append(rule.min_vph_now * (rule.max_age_min / 60.0))
    return int(min(targets)) if targets else DEFAULT_TARGET_VIEWS


def gate_for(topic: Topic, rules: Sequence[Rule], views_per_like: float) -> int:
    if topic.min_faves is not None:
        return max(int(topic.min_faves), 0)
    return suggested_min_faves(target_views(rules), views_per_like)


class Engine:
    def __init__(self, config: Config, store: Store, source: Source,
                 notifiers: Optional[Sequence[object]] = None,
                 now_fn=None):
        self.config = config
        self.store = store
        self.source = source
        self.notifiers = list(notifiers or [])
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------ budget

    def spent_today(self) -> int:
        now = self._now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.store.fetched_since(midnight)

    def remaining_budget(self) -> int:
        if self.config.daily_fetch_budget <= 0:
            return 1 << 30
        return max(self.config.daily_fetch_budget - self.spent_today(), 0)

    # -------------------------------------------------------------------- scan

    def scan_topic(self, topic: Topic, *, dry_run: bool = False) -> List[Finding]:
        now = self._now()
        rules = self.config.rules_for(topic)
        if not rules:
            log.warning("topic %s has no rules; nothing can fire", topic.name)

        budget = self.remaining_budget()
        if budget <= 0:
            raise BudgetExceeded(
                f"daily fetch budget of {self.config.daily_fetch_budget} posts is spent"
            )
        limit = max(min(self.config.max_fetch_per_run, budget), 1)

        since = now - timedelta(minutes=self.config.lookback_min)
        query = build_query(
            topic.query,
            min_faves=gate_for(topic, rules, self.config.views_per_like),
            lang=topic.lang,
            exclude_replies=topic.exclude_replies,
            exclude_retweets=topic.exclude_retweets,
            likes_operator=self.source.likes_operator,
            since=since,
            # The official API takes a start_time parameter instead.
            include_since=self.source.name != "x_api_v2",
        )

        run_id = self.store.start_run(topic.name, self.source.name, query, now)
        try:
            posts = self.source.search(query, since, limit)
        except SourceError as exc:
            self.store.finish_run(run_id, self._now(), 0, str(exc))
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            self.store.finish_run(run_id, self._now(), 0, repr(exc))
            raise

        kept = [p for p in posts if self._keep(p, topic, now)]
        findings: List[Finding] = []
        for post in kept:
            tagged = post if post.topic else _with_topic(post, topic.name)
            finding = self._assess(tagged, rules, now)
            if finding.rules:
                findings.append(finding)
        self.store.finish_run(run_id, self._now(), len(posts), None)

        findings.sort(key=lambda f: f.metrics.score, reverse=True)
        for finding in findings:
            fresh = self._fresh_rules(finding)
            if not fresh or dry_run:
                continue
            self._notify(finding, fresh)
            finding.notified = True
        return findings

    def scan_all(self, *, dry_run: bool = False
                 ) -> Tuple[List[Finding], List[Tuple[str, Exception]]]:
        """Scan every enabled topic. One topic failing does not stop the rest."""
        findings: List[Finding] = []
        errors: List[Tuple[str, Exception]] = []
        for topic in self.config.enabled_topics():
            try:
                findings.extend(self.scan_topic(topic, dry_run=dry_run))
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - reported per topic
                log.error("topic %s failed: %s", topic.name, exc)
                errors.append((topic.name, exc))
        findings.sort(key=lambda f: f.metrics.score, reverse=True)
        return findings, errors

    # ----------------------------------------------------------------- helpers

    def _keep(self, post: Post, topic: Topic, now: datetime) -> bool:
        """Client-side guard: sources honour operators inconsistently."""
        if topic.exclude_replies and post.is_reply:
            return False
        if topic.exclude_retweets and post.is_retweet:
            return False
        age_min = post.age_minutes(now)
        if age_min > self.config.track_max_age_min:
            return False
        # A post dated in the future is a clock or parsing problem, not a scoop.
        return age_min >= 0

    def _assess(self, post: Post, rules: Sequence[Rule], now: datetime) -> Finding:
        self.store.record(post)
        previous = self.store.previous_snapshot(
            post.id, post.fetched_at, self.config.velocity_min_gap_sec
        )
        baseline = self.store.author_baseline(post.author_handle, post.id)
        metrics = compute(
            post,
            now=now,
            prev_views=previous["views"] if previous else None,
            prev_likes=previous["likes"] if previous else 0,
            prev_ts=parse_timestamp(previous["ts"]) if previous else None,
            baseline_vph=baseline,
            views_per_like=self.config.views_per_like,
        )
        return Finding(post=post, metrics=metrics, rules=evaluate(rules, post, metrics))

    def _fresh_rules(self, finding: Finding) -> List[str]:
        """Rules worth sending: never fired, or the post has grown a lot since.

        Without this every poll would re-announce the same post for as long as
        it stays above the threshold.
        """
        factor = self.config.renotify_growth_factor
        fresh: List[str] = []
        for rule in finding.rules:
            previous = self.store.alerted_views(finding.post.id, rule)
            if previous is None:
                fresh.append(rule)
            elif factor > 1 and finding.metrics.views >= previous * factor:
                fresh.append(rule)
        return fresh

    def _notify(self, finding: Finding, rules: List[str]) -> None:
        history = [
            row["views"] for row in
            reversed(self.store.snapshots(finding.post.id, limit=12))
        ]
        for notifier in self.notifiers:
            try:
                notifier.send_alert(finding.post, finding.metrics, rules, history)
            except Exception as exc:  # noqa: BLE001 - a dead channel must not
                # lose the alert for the others, nor kill the scan loop.
                log.error("notifier %s failed: %s", type(notifier).__name__, exc)
        now = self._now()
        for rule in rules:
            self.store.mark_alert(
                finding.post.id, rule, now, finding.metrics.views,
                finding.metrics.score,
            )


def _with_topic(post: Post, topic: str) -> Post:
    from dataclasses import replace
    return replace(post, topic=topic)
