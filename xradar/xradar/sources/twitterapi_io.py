"""Adapter for twitterapi.io's advanced_search endpoint.

Chosen as the default paid source because it returns ``viewCount`` and the
author's follower count in the search response itself, so one call yields
everything the scoring needs. It is a third-party mirror, not X: treat data
quality as best-effort, and note that scraping mirrors sit in a grey area with
respect to X's terms of service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import http
from ..model import build_post, Post
from .base import Source, SourceError

ENDPOINT = "https://api.twitterapi.io/twitter/tweet/advanced_search"


class TwitterApiIoSource(Source):
    name = "twitterapi_io"
    cost_per_post = 0.00015  # ~$0.15 per 1000 posts returned
    likes_operator = "min_faves"

    def __init__(self, *, api_key: str = "", endpoint: str = ENDPOINT,
                 page_size_hint: int = 20, get_json=http.get_json, **_options):
        if not api_key:
            raise SourceError(
                "twitterapi_io needs an API key: set TWITTERAPI_IO_KEY in the "
                "environment (config references it as ${TWITTERAPI_IO_KEY})"
            )
        self.api_key = api_key
        self.endpoint = endpoint
        self.page_size_hint = page_size_hint
        self._get_json = get_json

    def search(self, query: str, since: datetime, limit: int) -> List[Post]:
        headers = {"X-API-Key": self.api_key}
        posts: List[Post] = []
        cursor: Optional[str] = None
        seen_cursors = set()
        fetched_at = datetime.now(tz=since.tzinfo)

        while len(posts) < limit:
            params: Dict[str, Any] = {"query": query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            payload = self._get_json(self.endpoint, params=params, headers=headers)
            tweets = payload.get("tweets") or payload.get("data") or []
            if not isinstance(tweets, list):
                raise SourceError(f"unexpected response shape: {list(payload)[:5]}")
            for raw in tweets:
                post = self._to_post(raw, fetched_at)
                if post is None:
                    continue
                if post.created_at < since:
                    # Results are newest-first, so anything older ends the walk.
                    return posts
                posts.append(post)
                if len(posts) >= limit:
                    return posts
            cursor = payload.get("next_cursor") or ""
            if not payload.get("has_next_page") or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return posts

    def _to_post(self, raw: dict, fetched_at: datetime) -> Optional[Post]:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        author = raw.get("author") or {}
        return build_post(
            id=raw.get("id"),
            url=raw.get("url") or raw.get("twitterUrl"),
            text=raw.get("text") or "",
            author_handle=author.get("userName") or author.get("screen_name") or "",
            author_name=author.get("name") or "",
            author_followers=author.get("followers", author.get("followersCount", 0)),
            created_at=raw.get("createdAt") or raw.get("created_at"),
            views=raw.get("viewCount", raw.get("views")),
            likes=raw.get("likeCount", 0),
            retweets=raw.get("retweetCount", 0),
            replies=raw.get("replyCount", 0),
            quotes=raw.get("quoteCount", 0),
            bookmarks=raw.get("bookmarkCount", 0),
            lang=raw.get("lang") or "",
            is_reply=bool(raw.get("isReply") or raw.get("inReplyToId")),
            is_retweet=bool(raw.get("retweeted_tweet") or raw.get("isRetweet")),
            source=self.name,
            fetched_at=fetched_at,
            raw=raw,
        )
