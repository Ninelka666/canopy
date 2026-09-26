"""Normalized post record shared by every source adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional


def parse_timestamp(raw) -> datetime:
    """Parse the several timestamp shapes X and its mirrors emit, as aware UTC.

    Handles the legacy Twitter form ("Tue Dec 10 07:00:30 +0000 2024"),
    ISO 8601 with Z or an offset, and epoch seconds/milliseconds.
    """
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, (int, float)):
        seconds = raw / 1000.0 if raw > 1e11 else float(raw)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(raw).strip()
        if not text:
            raise ValueError("empty timestamp")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = parsedate_to_datetime(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _int(value, default: int = 0) -> int:
    if value is None or value is True or value is False:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Post:
    """One post as seen at one moment in time.

    ``views`` is Optional on purpose: the official API frequently returns no
    impression figure at all, and a missing count must never be mistaken for a
    real zero. Scoring estimates views from likes in that case and marks the
    figure as estimated.
    """

    id: str
    url: str
    text: str
    author_handle: str
    created_at: datetime
    fetched_at: datetime
    source: str
    author_name: str = ""
    author_followers: int = 0
    views: Optional[int] = None
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    bookmarks: int = 0
    lang: str = ""
    is_reply: bool = False
    is_retweet: bool = False
    topic: str = ""
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def engagements(self) -> int:
        return self.likes + self.retweets + self.replies + self.quotes

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max((now - self.created_at).total_seconds() / 60.0, 0.0)


_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")


def clean_handle(raw) -> str:
    text = str(raw or "").strip()
    match = _HANDLE_RE.match(text)
    return match.group(1) if match else text.lstrip("@")


def build_post(
    *,
    id,
    text,
    author_handle,
    created_at,
    source,
    url=None,
    views=None,
    likes=0,
    retweets=0,
    replies=0,
    quotes=0,
    bookmarks=0,
    author_name="",
    author_followers=0,
    lang="",
    is_reply=False,
    is_retweet=False,
    topic="",
    fetched_at=None,
    raw=None,
) -> Post:
    """Build a Post from loosely typed source JSON, coercing numeric fields."""
    handle = clean_handle(author_handle)
    post_id = str(id)
    return Post(
        id=post_id,
        url=url or f"https://x.com/{handle or 'i'}/status/{post_id}",
        text=str(text or ""),
        author_handle=handle,
        author_name=str(author_name or ""),
        author_followers=_int(author_followers),
        created_at=parse_timestamp(created_at),
        fetched_at=fetched_at or datetime.now(timezone.utc),
        source=source,
        views=None if views is None else max(_int(views), 0),
        likes=_int(likes),
        retweets=_int(retweets),
        replies=_int(replies),
        quotes=_int(quotes),
        bookmarks=_int(bookmarks),
        lang=str(lang or ""),
        is_reply=bool(is_reply),
        is_retweet=bool(is_retweet),
        topic=topic,
        raw=raw or {},
    )
