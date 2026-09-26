"""Configuration loading: JSON on disk, secrets from the environment."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = Path("config.json")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for a malformed or incomplete configuration."""


def expand_env(value: Any) -> Any:
    """Replace ``${VAR}`` with the environment value, recursively.

    Keeps secrets out of the config file: the file references a variable name,
    the value comes from the environment at run time. An unset variable expands
    to an empty string so the caller can report it as missing by name.
    """
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass
class Rule:
    """A named alert condition. Every field that is set must pass.

    A condition on a metric that could not be computed counts as a failure:
    the tool never claims a post is flying on evidence it does not have.
    """

    name: str
    max_age_min: Optional[float] = None
    min_age_min: Optional[float] = None
    min_views: Optional[int] = None
    min_vph: Optional[float] = None
    min_vph_now: Optional[float] = None
    min_accel: Optional[float] = None
    min_reach_multiple: Optional[float] = None
    max_followers: Optional[int] = None
    min_followers: Optional[int] = None
    min_likes: Optional[int] = None
    min_engagement_rate: Optional[float] = None
    min_author_outlier: Optional[float] = None
    allow_estimated_views: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Rule":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"rule {data.get('name', '?')!r}: unknown field(s) {sorted(unknown)}"
            )
        if "name" not in data:
            raise ConfigError("every rule needs a 'name'")
        return cls(**data)


@dataclass
class Topic:
    """A subject to watch: search terms plus per-topic overrides."""

    name: str
    query: str
    lang: Optional[str] = None
    min_faves: Optional[int] = None
    exclude_replies: bool = True
    exclude_retweets: bool = True
    rules: Optional[List[str]] = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Topic":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(
                f"topic {data.get('name', '?')!r}: unknown field(s) {sorted(unknown)}"
            )
        for required in ("name", "query"):
            if not data.get(required):
                raise ConfigError(f"every topic needs a non-empty '{required}'")
        return cls(**data)


DEFAULT_RULES: List[Dict[str, Any]] = [
    # The literal ask: ~100k views inside the first hour or so.
    {
        "name": "flying_1h",
        "max_age_min": 90,
        "min_views": 100_000,
        "min_likes": 50,
    },
    # The find that is actually worth money: a small account escaping its
    # own follower graph. reach_multiple = views / followers.
    {
        "name": "escape_velocity",
        "max_age_min": 180,
        "min_vph": 50_000,
        "min_reach_multiple": 3.0,
        "max_followers": 200_000,
        "min_likes": 50,
    },
    # Still picking up speed between the last two snapshots, so there is
    # time left to react.
    {
        "name": "accelerating",
        "max_age_min": 240,
        "min_age_min": 10,
        "min_vph_now": 80_000,
        "min_accel": 1.25,
        "allow_estimated_views": False,
    },
]


@dataclass
class Config:
    source: str = "mock"
    source_options: Dict[str, Any] = field(default_factory=dict)
    db_path: str = "xradar.db"
    topics: List[Topic] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)
    interval_sec: int = 300
    lookback_min: int = 150
    track_max_age_min: int = 360
    max_fetch_per_run: int = 200
    daily_fetch_budget: int = 20_000
    views_per_like: float = 300.0
    velocity_min_gap_sec: int = 150
    renotify_growth_factor: float = 3.0
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_console: bool = True

    @property
    def rules_by_name(self) -> Dict[str, Rule]:
        return {r.name: r for r in self.rules}

    def rules_for(self, topic: Topic) -> List[Rule]:
        if not topic.rules:
            return list(self.rules)
        index = self.rules_by_name
        missing = [n for n in topic.rules if n not in index]
        if missing:
            raise ConfigError(
                f"topic {topic.name!r} references undefined rule(s) {missing}"
            )
        return [index[n] for n in topic.rules]

    def enabled_topics(self) -> List[Topic]:
        return [t for t in self.topics if t.enabled]


def from_dict(data: Dict[str, Any]) -> Config:
    data = expand_env(dict(data))
    known = {f for f in Config.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown top-level field(s) {sorted(unknown)}")
    topics = [Topic.from_dict(t) for t in data.pop("topics", [])]
    raw_rules = data.pop("rules", None)
    rules = [Rule.from_dict(r) for r in (raw_rules if raw_rules is not None else DEFAULT_RULES)]
    names = [r.name for r in rules]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate rule name(s) {sorted(duplicates)}")
    cfg = Config(**data)
    return replace(cfg, topics=topics, rules=rules)


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no config at {path}; run `python -m xradar init` first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return from_dict(data)
