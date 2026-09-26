"""Deterministic synthetic source, so the pipeline can be proved for free.

Posts are anchored to an absolute time grid and grow along a logistic curve, so
repeated polls show views genuinely rising - which is what exercises velocity,
acceleration and de-duplication end to end without spending anything on a real
API key.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..model import build_post, Post
from .base import Source, build_query

_MIN_FAVES_RE = re.compile(r"min_(?:faves|likes):(\d+)")
_SLOT_MIN = 10


def _terms_only(query: str) -> str:
    """Strip operators, leaving the subject terms.

    The seed must not depend on ``since_time:``, which moves every second:
    otherwise every poll would invent a fresh set of posts and no view history
    would ever accumulate.
    """
    kept = [
        token for token in query.split()
        if ":" not in token and not token.startswith("-")
    ]
    return " ".join(kept).strip().strip('"') or "the topic"

TOPICS_TEXT = [
    "breaking: {kw} just shipped something nobody expected",
    "nobody is talking about what {kw} means for the next 12 months",
    "i spent three weeks testing {kw}. here is what actually held up",
    "the {kw} numbers released today do not say what the headline says",
    "unpopular opinion about {kw}, with receipts",
    "thread: how {kw} actually works, minus the hype",
]


def _rng(*parts) -> random.Random:
    """Seeded from a stable hash - Python's hash() is salted per process."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


class MockSource(Source):
    name = "mock"
    cost_per_post = 0.0
    likes_operator = "min_faves"

    def __init__(self, *, now_fn=None, **_options):
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def search(self, query: str, since: datetime, limit: int) -> List[Post]:
        now = self._now()
        gate_match = _MIN_FAVES_RE.search(query)
        gate = int(gate_match.group(1)) if gate_match else 0
        keyword = _terms_only(query)

        posts: List[Post] = []
        # Walk the 10-minute slots covered by the window, newest first.
        slot_now = int(now.timestamp() // (_SLOT_MIN * 60))
        slot_since = int(since.timestamp() // (_SLOT_MIN * 60))
        for slot in range(slot_now, slot_since - 1, -1):
            created = datetime.fromtimestamp(slot * _SLOT_MIN * 60, tz=timezone.utc)
            age_min = (now - created).total_seconds() / 60.0
            if age_min < 0:
                continue
            rng = _rng(keyword, slot)
            for index in range(rng.randint(1, 3)):
                post = self._make(keyword, slot, index, created, age_min, now)
                if post.likes < gate:
                    continue
                posts.append(post)
                if len(posts) >= limit:
                    return posts
        return posts

    def _make(self, keyword: str, slot: int, index: int, created: datetime,
              age_min: float, now: datetime) -> Post:
        rng = _rng(slot, index, keyword)
        # A heavy tail: most posts go nowhere, a few genuinely fly.
        roll = rng.random()
        if roll > 0.93:
            peak = rng.randint(800_000, 4_000_000)
        elif roll > 0.75:
            peak = rng.randint(120_000, 700_000)
        else:
            peak = rng.randint(2_000, 60_000)
        followers = int(10 ** rng.uniform(2.7, 6.2))
        midpoint = rng.uniform(35.0, 90.0)
        tau = rng.uniform(8.0, 22.0)
        # Logistic curve, normalized so views start near zero at age 0.
        def curve(t: float) -> float:
            return 1.0 / (1.0 + math.exp(-(t - midpoint) / tau))
        views = max(int(peak * (curve(age_min) - curve(0.0)) / (1.0 - curve(0.0))), 0)
        views_per_like = rng.uniform(180.0, 520.0)
        likes = int(views / views_per_like)
        post_id = f"mock{slot}{index:02d}"
        handle = f"mock_user{slot % 37}_{index}"
        return build_post(
            id=post_id,
            url=f"https://x.com/{handle}/status/{post_id}",
            text=rng.choice(TOPICS_TEXT).format(kw=keyword),
            author_handle=handle,
            author_name=f"Mock Account {slot % 37}",
            author_followers=followers,
            created_at=created,
            views=views,
            likes=likes,
            retweets=int(likes * rng.uniform(0.05, 0.3)),
            replies=int(likes * rng.uniform(0.02, 0.2)),
            quotes=int(likes * rng.uniform(0.01, 0.1)),
            bookmarks=int(likes * rng.uniform(0.05, 0.5)),
            lang="en",
            source=self.name,
            fetched_at=now,
        )
