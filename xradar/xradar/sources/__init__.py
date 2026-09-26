"""Source registry."""

from __future__ import annotations

from typing import Any, Dict

from .base import Source, SourceError, build_query
from .mock import MockSource
from .twitterapi_io import TwitterApiIoSource
from .x_api_v2 import XApiV2Source

REGISTRY = {
    MockSource.name: MockSource,
    TwitterApiIoSource.name: TwitterApiIoSource,
    XApiV2Source.name: XApiV2Source,
}


def create(name: str, options: Dict[str, Any] | None = None) -> Source:
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise SourceError(
            f"unknown source {name!r}; available: {', '.join(sorted(REGISTRY))}"
        ) from None
    return factory(**(options or {}))


__all__ = ["Source", "SourceError", "build_query", "create", "REGISTRY"]
