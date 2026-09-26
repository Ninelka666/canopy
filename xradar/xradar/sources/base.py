"""Source adapter contract and search-query construction."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

from ..model import Post


class SourceError(Exception):
    """A source could not answer; the caller decides whether to continue."""


class Source:
    """Fetches recent posts for a query.

    Adapters exist because the data market keeps moving: the official API has
    changed pricing and metric availability repeatedly, and mirrors come and
    go. Everything above this line works in terms of :class:`Post`.
    """

    name = "base"
    #: USD per post returned, for the cost estimate. 0 for free sources.
    cost_per_post = 0.0
    #: Search operator this source accepts as a likes gate.
    likes_operator = "min_faves"

    def search(self, query: str, since: datetime, limit: int) -> List[Post]:
        raise NotImplementedError

    def close(self) -> None:
        pass


def build_query(
    terms: str,
    *,
    min_faves: Optional[int] = None,
    lang: Optional[str] = None,
    exclude_replies: bool = True,
    exclude_retweets: bool = True,
    likes_operator: str = "min_faves",
    since: Optional[datetime] = None,
    include_since: bool = True,
) -> str:
    """Assemble a search string.

    The likes gate is the cost control: X has no ``min_views`` operator, so
    high-view posts are found by asking for posts with enough likes and reading
    views only for those.
    """
    parts: List[str] = [terms.strip()]
    if min_faves and min_faves > 0:
        parts.append(f"{likes_operator}:{int(min_faves)}")
    if lang:
        parts.append(f"lang:{lang}")
    if exclude_replies:
        parts.append("-filter:replies")
    if exclude_retweets:
        parts.append("-filter:nativeretweets -filter:retweets")
    if since is not None and include_since:
        parts.append(f"since_time:{int(since.timestamp())}")
    return " ".join(p for p in parts if p)
