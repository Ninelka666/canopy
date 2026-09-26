"""Adapter for the official X API v2 recent-search endpoint.

Two caveats drove this implementation:

* ``impression_count`` is documented as a public metric but is widely reported
  to come back zero or far below what the web UI shows. Missing views fall back
  to a likes-based estimate upstream, flagged as estimated.
* the likes gate operator is not accepted on every access level. A 400 that
  names the operator retries the query without it and filters client-side, so a
  rejected operator degrades the cost, not the result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import http
from ..model import build_post, Post
from .base import Source, SourceError

ENDPOINT = "https://api.x.com/2/tweets/search/recent"
TWEET_FIELDS = "created_at,public_metrics,lang,author_id,referenced_tweets"
USER_FIELDS = "username,name,public_metrics"


class XApiV2Source(Source):
    name = "x_api_v2"
    cost_per_post = 0.005  # pay-per-use read price
    likes_operator = "min_likes"

    def __init__(self, *, bearer_token: str = "", endpoint: str = ENDPOINT,
                 page_size: int = 100, get_json=http.get_json, **_options):
        if not bearer_token:
            raise SourceError(
                "x_api_v2 needs a bearer token: set X_BEARER_TOKEN in the "
                "environment (config references it as ${X_BEARER_TOKEN})"
            )
        self.bearer_token = bearer_token
        self.endpoint = endpoint
        self.page_size = max(10, min(int(page_size), 100))
        self._get_json = get_json
        #: Flipped off permanently once the API rejects the likes operator.
        self.gate_supported = True
        self.min_likes_fallback = 0

    def search(self, query: str, since: datetime, limit: int) -> List[Post]:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        # The recent-search window is 7 days, and start_time must not be in the
        # future relative to the API's clock; clamp defensively.
        start = max(since, datetime.now(timezone.utc) - timedelta(days=6))
        posts: List[Post] = []
        token: Optional[str] = None
        fetched_at = datetime.now(timezone.utc)
        effective_query = query

        while len(posts) < limit:
            params: Dict[str, Any] = {
                "query": effective_query,
                "max_results": min(self.page_size, max(limit - len(posts), 10)),
                "start_time": start.astimezone(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
                "tweet.fields": TWEET_FIELDS,
                "expansions": "author_id",
                "user.fields": USER_FIELDS,
            }
            if token:
                params["next_token"] = token
            try:
                payload = self._get_json(self.endpoint, params=params, headers=headers)
            except http.HttpError as exc:
                stripped = self._strip_gate(effective_query, exc)
                if stripped is None:
                    raise SourceError(str(exc)) from exc
                effective_query = stripped
                continue

            users = {
                u["id"]: u for u in (payload.get("includes", {}).get("users") or [])
                if isinstance(u, dict) and u.get("id")
            }
            for raw in payload.get("data") or []:
                post = self._to_post(raw, users, fetched_at)
                if post is None or post.likes < self.min_likes_fallback:
                    continue
                posts.append(post)
                if len(posts) >= limit:
                    return posts
            token = (payload.get("meta") or {}).get("next_token")
            if not token:
                break
        return posts

    def _strip_gate(self, query: str, exc: http.HttpError) -> Optional[str]:
        """Drop an unsupported likes operator and remember the threshold.

        Returns the rewritten query, or None if the error is unrelated.
        """
        if exc.status != 400 or not self.gate_supported:
            return None
        marker = f"{self.likes_operator}:"
        if marker not in query:
            return None
        if self.likes_operator not in exc.body and "operator" not in exc.body.lower():
            return None
        remaining: List[str] = []
        for token in query.split():
            if token.startswith(marker):
                try:
                    self.min_likes_fallback = int(token.split(":", 1)[1])
                except ValueError:
                    pass
            else:
                remaining.append(token)
        self.gate_supported = False
        return " ".join(remaining)

    def _to_post(self, raw: dict, users: Dict[str, dict],
                 fetched_at: datetime) -> Optional[Post]:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        metrics = raw.get("public_metrics") or {}
        author = users.get(str(raw.get("author_id")), {})
        followers = ((author.get("public_metrics") or {}).get("followers_count", 0))
        referenced = [r.get("type") for r in (raw.get("referenced_tweets") or [])]
        impressions = metrics.get("impression_count")
        return build_post(
            id=raw.get("id"),
            text=raw.get("text") or "",
            author_handle=author.get("username") or "",
            author_name=author.get("name") or "",
            author_followers=followers,
            created_at=raw.get("created_at"),
            # A reported 0 is treated as "not reported": the API does this
            # routinely, and a real 0 on a post with likes is impossible.
            views=impressions if impressions else None,
            likes=metrics.get("like_count", 0),
            retweets=metrics.get("retweet_count", 0),
            replies=metrics.get("reply_count", 0),
            quotes=metrics.get("quote_count", 0),
            bookmarks=metrics.get("bookmark_count", 0),
            lang=raw.get("lang") or "",
            is_reply="replied_to" in referenced,
            is_retweet="retweeted" in referenced,
            source=self.name,
            fetched_at=fetched_at,
            raw=raw,
        )
