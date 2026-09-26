"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, sources
from .config import Config, ConfigError, DEFAULT_CONFIG_PATH, DEFAULT_RULES, load
from .engine import BudgetExceeded, Engine, Finding, gate_for, target_views
from .model import parse_timestamp
from .notify import ConsoleNotifier, TelegramNotifier, format_table
from .notify.telegram import TelegramError
from .scoring import compute, evaluate, humanize
from .sources import SourceError
from .store import Store

log = logging.getLogger("xradar")

CONFIG_TEMPLATE = {
    "source": "mock",
    "source_options": {
        "_comment": "for twitterapi_io: api_key; for x_api_v2: bearer_token",
        "api_key": "${TWITTERAPI_IO_KEY}",
    },
    "db_path": "xradar.db",
    "interval_sec": 300,
    "lookback_min": 150,
    "track_max_age_min": 360,
    "max_fetch_per_run": 200,
    "daily_fetch_budget": 20000,
    "views_per_like": 300.0,
    "velocity_min_gap_sec": 150,
    "renotify_growth_factor": 3.0,
    "telegram_bot_token": "${TELEGRAM_BOT_TOKEN}",
    "telegram_chat_id": "${TELEGRAM_CHAT_ID}",
    "notify_console": True,
    "topics": [
        {
            "name": "ai-agents",
            "query": '("ai agents" OR "agentic" OR "llm agents")',
            "lang": "en",
        },
        {
            "name": "crypto-infra",
            "query": '(rollup OR "modular blockchain" OR restaking)',
            "lang": "en",
            "enabled": False,
        },
    ],
    "rules": DEFAULT_RULES,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xradar",
        description="Find posts on X that are taking off right now.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"xradar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config.json and .env template")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")

    scan = sub.add_parser("scan", help="run one pass over every enabled topic")
    scan.add_argument("--topic", action="append", help="limit to this topic (repeatable)")
    scan.add_argument("--dry-run", action="store_true",
                      help="score and print, send nothing, record no alerts")
    scan.add_argument("--json", action="store_true", help="emit findings as JSON")

    watch = sub.add_parser("watch", help="poll on an interval until Ctrl+C")
    watch.add_argument("--topic", action="append")
    watch.add_argument("--interval", type=int, help="seconds between passes")
    watch.add_argument("--dry-run", action="store_true")

    top = sub.add_parser("top", help="rank what is already in the local database")
    top.add_argument("--hours", type=float, default=6.0)
    top.add_argument("--topic")
    top.add_argument("--limit", type=int, default=25)
    top.add_argument("--all", action="store_true",
                     help="include posts that matched no rule")

    sub.add_parser("check", help="validate config, source credentials and Telegram")

    cost = sub.add_parser("cost", help="posts fetched and estimated spend")
    cost.add_argument("--days", type=int, default=14)

    prune = sub.add_parser("prune", help="delete posts older than the retention window")
    prune.add_argument("--days", type=int, default=14)
    return parser


# --------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    path: Path = args.config
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    path.write_text(json.dumps(CONFIG_TEMPLATE, indent=2) + "\n", encoding="utf-8")
    env_path = path.parent / ".env.example"
    env_path.write_text(
        "# Source credentials - fill in the one matching config.source\n"
        "TWITTERAPI_IO_KEY=\n"
        "X_BEARER_TOKEN=\n"
        "\n# Telegram: create a bot with @BotFather, then message it once and read\n"
        "# your chat id from https://api.telegram.org/bot<TOKEN>/getUpdates\n"
        "TELEGRAM_BOT_TOKEN=\n"
        "TELEGRAM_CHAT_ID=\n",
        encoding="utf-8",
    )
    print(f"wrote {path} and {env_path}")
    print("next: edit the topics, then run `python -m xradar scan --dry-run`")
    print("(source is 'mock' for now, so it costs nothing and needs no key)")
    return 0


def _build(config: Config, *, quiet: bool = False):
    source = sources.create(config.source, config.source_options)
    store = Store(config.db_path)
    notifiers: List[object] = []
    if config.telegram_bot_token and config.telegram_chat_id:
        notifiers.append(TelegramNotifier(config.telegram_bot_token,
                                          config.telegram_chat_id))
    elif not quiet:
        print("note: Telegram is not configured, alerting to console only",
              file=sys.stderr)
    if config.notify_console or not notifiers:
        notifiers.append(ConsoleNotifier())
    return source, store, Engine(config, store, source, notifiers)


def _select_topics(config: Config, names: Optional[Sequence[str]]) -> Config:
    if not names:
        return config
    wanted = set(names)
    unknown = wanted - {t.name for t in config.topics}
    if unknown:
        raise ConfigError(f"unknown topic(s) {sorted(unknown)}")
    from dataclasses import replace
    return replace(config, topics=[t for t in config.topics if t.name in wanted])


def _finding_dict(f: Finding) -> dict:
    m = f.metrics
    return {
        "id": f.post.id,
        "url": f.post.url,
        "author": f.post.author_handle,
        "followers": f.post.author_followers,
        "topic": f.post.topic,
        "created_at": f.post.created_at.isoformat(),
        "age_min": round(m.age_min, 1),
        "views": m.views,
        "views_estimated": m.views_estimated,
        "views_per_hour": round(m.vph_effective),
        "views_per_hour_avg": round(m.vph_avg),
        "views_per_hour_now": None if m.vph_now is None else round(m.vph_now),
        "accel": None if m.accel is None else round(m.accel, 2),
        "reach_multiple": round(m.reach_multiple, 2),
        "engagement_rate": None if m.engagement_rate is None else round(m.engagement_rate, 4),
        "author_outlier": None if m.author_outlier is None else round(m.author_outlier, 2),
        "score": m.score,
        "rules": f.rules,
        "notified": f.notified,
        "text": f.post.text,
    }


def cmd_scan(args, config: Config) -> int:
    config = _select_topics(config, args.topic)
    source, store, engine = _build(config, quiet=args.json)
    try:
        findings, errors = engine.scan_all(dry_run=args.dry_run)
    except BudgetExceeded as exc:
        print(f"budget: {exc}", file=sys.stderr)
        return 2
    finally:
        source.close()

    if args.json:
        print(json.dumps([_finding_dict(f) for f in findings], indent=2))
    else:
        rows = [(f.post, f.metrics, f.rules) for f in findings]
        print(format_table(rows))
        gates = ", ".join(
            f"{t.name}:{gate_for(t, config.rules_for(t), config.views_per_like)}"
            for t in config.enabled_topics()
        )
        print(f"\nsource={config.source} likes gate ({gates}) "
              f"fetched_today={engine.spent_today()}")
    for name, exc in errors:
        print(f"error in topic {name}: {exc}", file=sys.stderr)
    store.close()
    return 1 if errors and not findings else 0


def cmd_watch(args, config: Config) -> int:
    config = _select_topics(config, args.topic)
    interval = max(args.interval or config.interval_sec, 30)
    source, store, engine = _build(config)
    stopping = {"flag": False}

    def _stop(_signum, _frame):
        stopping["flag"] = True
        print("\nstopping after this pass…", file=sys.stderr)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    topics = ", ".join(t.name for t in config.enabled_topics()) or "(none enabled)"
    print(f"xradar watching [{topics}] every {interval}s via {config.source}; "
          f"Ctrl+C to stop")
    passes = 0
    try:
        while not stopping["flag"]:
            started = time.monotonic()
            try:
                findings, errors = engine.scan_all()
            except BudgetExceeded as exc:
                print(f"budget: {exc}; stopping", file=sys.stderr)
                return 2
            passes += 1
            sent = sum(1 for f in findings if f.notified)
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] pass {passes}: {len(findings)} matching, {sent} alerted, "
                  f"{engine.spent_today()} posts fetched today", flush=True)
            for name, exc in errors:
                print(f"  error in topic {name}: {exc}", file=sys.stderr)
            # Sleep in short slices so Ctrl+C is felt immediately.
            deadline = started + interval
            while not stopping["flag"] and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        source.close()
        store.close()
    return 0


def cmd_top(args, config: Config) -> int:
    from .model import build_post
    store = Store(config.db_path)
    rows = store.latest_snapshot_rows(int(args.hours * 60), args.topic)
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        post = build_post(
            id=row["id"], url=row["url"], text=row["text"],
            author_handle=row["author_handle"], author_name=row["author_name"],
            author_followers=row["author_followers"], created_at=row["created_at"],
            source=row["source"], views=row["views"], likes=row["likes"],
            retweets=row["retweets"], replies=row["replies"], quotes=row["quotes"],
            bookmarks=row["bookmarks"], lang=row["lang"], topic=row["topic"],
            fetched_at=parse_timestamp(row["snap_ts"]),
        )
        previous = store.previous_snapshot(post.id, post.fetched_at,
                                           config.velocity_min_gap_sec)
        metrics = compute(
            post, now=now,
            prev_views=previous["views"] if previous else None,
            prev_likes=previous["likes"] if previous else 0,
            prev_ts=parse_timestamp(previous["ts"]) if previous else None,
            baseline_vph=store.author_baseline(post.author_handle, post.id),
            views_per_like=config.views_per_like,
        )
        matched = evaluate(config.rules, post, metrics)
        if matched or args.all:
            out.append((post, metrics, matched))
    out.sort(key=lambda item: item[1].score, reverse=True)
    print(format_table(out[: args.limit]))
    store.close()
    return 0


def cmd_check(args, config: Config) -> int:
    ok = True
    print(f"config      : {args.config} parsed, "
          f"{len(config.enabled_topics())}/{len(config.topics)} topics enabled, "
          f"{len(config.rules)} rules")
    for topic in config.enabled_topics():
        rules = config.rules_for(topic)
        print(f"  topic {topic.name:<16} gate=min_faves:"
              f"{gate_for(topic, rules, config.views_per_like)} "
              f"target={humanize(target_views(rules))} views  rules="
              f"{','.join(r.name for r in rules)}")
    try:
        source = sources.create(config.source, config.source_options)
        print(f"source      : {source.name} ready "
              f"(~${source.cost_per_post * 1000:.2f} per 1000 posts)")
        source.close()
    except SourceError as exc:
        print(f"source      : FAILED - {exc}")
        ok = False

    if config.telegram_bot_token and config.telegram_chat_id:
        try:
            notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
            username = notifier.check()
            notifier.send("xradar: connection test ok ✅")
            print(f"telegram    : ok, bot @{username}, test message sent")
        except TelegramError as exc:
            print(f"telegram    : FAILED - {exc}")
            ok = False
    else:
        print("telegram    : not configured (console alerts only)")

    store = Store(config.db_path)
    print(f"database    : {config.db_path}, "
          f"{store.fetched_since(datetime.now(timezone.utc) - timedelta(days=1))} "
          f"posts fetched in the last 24h")
    store.close()
    return 0 if ok else 1


def cmd_cost(args, config: Config) -> int:
    store = Store(config.db_path)
    try:
        per_post = sources.REGISTRY[config.source].cost_per_post
    except KeyError:
        per_post = 0.0
    rows = store.fetched_by_day(args.days)
    total = sum(n for _, n in rows)
    print(f"source {config.source} at ${per_post:.5f} per post fetched\n")
    print(f"{'day':<12}{'posts':>10}{'est. cost':>12}")
    for day, n in rows:
        print(f"{day:<12}{n:>10}{'$' + format(n * per_post, '.2f'):>12}")
    print(f"{'total':<12}{total:>10}{'$' + format(total * per_post, '.2f'):>12}")
    if config.daily_fetch_budget > 0:
        print(f"\ndaily budget: {config.daily_fetch_budget} posts "
              f"(~${config.daily_fetch_budget * per_post:.2f})")
    store.close()
    return 0


def cmd_prune(args, config: Config) -> int:
    store = Store(config.db_path)
    removed = store.prune(args.days)
    print(f"removed {removed} posts older than {args.days} days")
    store.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )
    if args.command == "init":
        return cmd_init(args)
    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    handlers = {
        "scan": cmd_scan, "watch": cmd_watch, "top": cmd_top,
        "check": cmd_check, "cost": cmd_cost, "prune": cmd_prune,
    }
    try:
        return handlers[args.command](args, config)
    except (ConfigError, SourceError, TelegramError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
