"""Turning raw counters into the signal "this post is taking off right now".

Three distinct ideas, deliberately kept apart:

* average velocity (``vph_avg``) - views since publication, per hour. Simple,
  but a post that did 200k in its first ten minutes and then died still looks
  fast by this measure an hour later.
* current velocity (``vph_now``) - views gained between the last two
  snapshots, per hour. This is what says "still climbing".
* reach multiple - views divided by the author's followers. Above ~3 the post
  has left its own follower graph, which is what actually distinguishes a
  viral post from a big account's ordinary Tuesday.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .config import Rule
from .model import Post


@dataclass
class Metrics:
    age_min: float
    views: int
    views_estimated: bool
    vph_avg: float
    vph_now: Optional[float]
    accel: Optional[float]
    reach_multiple: float
    engagement_rate: Optional[float]
    author_outlier: Optional[float]
    score: float
    delta_views: Optional[int] = None
    delta_min: Optional[float] = None

    @property
    def vph_effective(self) -> float:
        """Current velocity when known, otherwise the lifetime average."""
        return self.vph_now if self.vph_now is not None else self.vph_avg


def estimate_views(views: Optional[int], likes: int, views_per_like: float) -> tuple:
    """Return (views, estimated?).

    The official API often reports no impressions at all. Rather than treat
    that as zero and go blind, views are estimated from likes - a rough
    heuristic (X's like-to-view ratio commonly lands near 1:200-1:500) that is
    always flagged so a rule can refuse to act on it.
    """
    if views is not None and views > 0:
        return int(views), False
    return int(max(likes, 0) * views_per_like), True


def compute(
    post: Post,
    *,
    now: Optional[datetime] = None,
    prev_views: Optional[int] = None,
    prev_likes: int = 0,
    prev_ts: Optional[datetime] = None,
    baseline_vph: Optional[float] = None,
    views_per_like: float = 300.0,
) -> Metrics:
    now = now or datetime.now(timezone.utc)
    age_min = max(post.age_minutes(now), 0.5)  # floor: a fresh post is not infinitely fast
    views, estimated = estimate_views(post.views, post.likes, views_per_like)

    vph_avg = views / (age_min / 60.0)

    vph_now: Optional[float] = None
    accel: Optional[float] = None
    delta_views: Optional[int] = None
    delta_min: Optional[float] = None
    if prev_ts is not None:
        prev_val, _ = estimate_views(prev_views, prev_likes, views_per_like)
        delta_min = (post.fetched_at - prev_ts).total_seconds() / 60.0
        if delta_min > 0:
            # Counters occasionally step backwards between reads; a negative
            # delta is noise, not a post losing views.
            delta_views = max(views - prev_val, 0)
            vph_now = delta_views / (delta_min / 60.0)
            prev_age_min = max(age_min - delta_min, 0.5)
            prev_avg = prev_val / (prev_age_min / 60.0)
            if prev_avg > 0:
                accel = vph_now / prev_avg

    followers = max(post.author_followers, 1)
    reach_multiple = views / followers
    engagement_rate = (post.engagements / views) if views > 0 else None
    author_outlier = None
    if baseline_vph and baseline_vph > 0:
        author_outlier = (vph_now if vph_now is not None else vph_avg) / baseline_vph

    return Metrics(
        age_min=age_min,
        views=views,
        views_estimated=estimated,
        vph_avg=vph_avg,
        vph_now=vph_now,
        accel=accel,
        reach_multiple=reach_multiple,
        engagement_rate=engagement_rate,
        author_outlier=author_outlier,
        score=_score(vph_now if vph_now is not None else vph_avg, reach_multiple,
                     accel, engagement_rate, estimated),
        delta_views=delta_views,
        delta_min=delta_min,
    )


def _score(vph: float, reach_multiple: float, accel: Optional[float],
           engagement_rate: Optional[float], estimated: bool) -> float:
    """A ranking number, not a truth claim.

    Velocity dominates (log-scaled, so 1M/h ranks above 100k/h without
    swamping every other term), reach multiple and acceleration add to it, and
    an estimated view count is discounted because it rests on a guessed ratio.
    """
    # Scaled across the range that matters: 1k views/h scores 0, 1M/h scores
    # 100. Below 1k/h nothing is taking off, above 1M/h the difference is
    # academic.
    decades = (math.log10(1.0 + max(vph, 0.0)) - 3.0) / 3.0
    velocity_part = 100.0 * min(max(decades, 0.0), 1.0)
    reach_part = 25.0 * min(reach_multiple / 10.0, 1.0)
    accel_part = 15.0 * min(max((accel or 1.0) - 1.0, 0.0), 1.0)
    er_part = 10.0 * min((engagement_rate or 0.0) / 0.05, 1.0)
    total = velocity_part + reach_part + accel_part + er_part
    return round(total * (0.85 if estimated else 1.0), 1)


def matches(rule: Rule, post: Post, m: Metrics) -> bool:
    """True when every condition the rule sets is satisfied.

    A condition on a metric that could not be computed fails: absence of
    evidence is never treated as evidence.
    """
    if m.views_estimated and not rule.allow_estimated_views:
        return False
    checks = (
        (rule.max_age_min, lambda v: m.age_min <= v),
        (rule.min_age_min, lambda v: m.age_min >= v),
        (rule.min_views, lambda v: m.views >= v),
        (rule.min_vph, lambda v: m.vph_avg >= v),
        (rule.min_vph_now, lambda v: m.vph_now is not None and m.vph_now >= v),
        (rule.min_accel, lambda v: m.accel is not None and m.accel >= v),
        (rule.min_reach_multiple, lambda v: m.reach_multiple >= v),
        (rule.max_followers, lambda v: post.author_followers <= v),
        (rule.min_followers, lambda v: post.author_followers >= v),
        (rule.min_likes, lambda v: post.likes >= v),
        (rule.min_engagement_rate,
         lambda v: m.engagement_rate is not None and m.engagement_rate >= v),
        (rule.min_author_outlier,
         lambda v: m.author_outlier is not None and m.author_outlier >= v),
    )
    return all(check(value) for value, check in checks if value is not None)


def evaluate(rules: Sequence[Rule], post: Post, m: Metrics) -> List[str]:
    return [r.name for r in rules if matches(r, post, m)]


def suggested_min_faves(target_views: int, views_per_like: float,
                        floor: int = 20) -> int:
    """Likes gate that a post at ``target_views`` would plausibly clear.

    X exposes no ``min_views`` search operator, so the cheap way to find
    high-view posts is to gate the search on likes and read views only for what
    comes back. The divisor is a heuristic: a low ratio widens the net and
    costs more, a high one risks missing view-heavy, like-light posts (video,
    breaking news).
    """
    return max(int(target_views / max(views_per_like, 1.0)), floor)


def humanize(n: Optional[float]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            trimmed = n / limit
            return f"{trimmed:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))
