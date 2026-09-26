"""SQLite storage: posts, their metric snapshots over time, and fired alerts.

Velocity is the whole point of this tool, and velocity needs history, so every
poll writes a snapshot rather than overwriting a row.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .model import Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id               TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    text             TEXT NOT NULL,
    author_handle    TEXT NOT NULL,
    author_name      TEXT NOT NULL DEFAULT '',
    author_followers INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    lang             TEXT NOT NULL DEFAULT '',
    topic            TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    first_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts (topic);

CREATE TABLE IF NOT EXISTS snapshots (
    post_id   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    views     INTEGER,
    likes     INTEGER NOT NULL DEFAULT 0,
    retweets  INTEGER NOT NULL DEFAULT 0,
    replies   INTEGER NOT NULL DEFAULT 0,
    quotes    INTEGER NOT NULL DEFAULT 0,
    bookmarks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, ts),
    FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);

CREATE TABLE IF NOT EXISTS alerts (
    post_id    TEXT NOT NULL,
    rule       TEXT NOT NULL,
    fired_at   TEXT NOT NULL,
    views      INTEGER NOT NULL DEFAULT 0,
    score      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (post_id, rule)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    source      TEXT NOT NULL,
    query       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class Store:
    def __init__(self, path: str = "xradar.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- writes

    def record(self, post: Post) -> None:
        """Upsert the post and append a snapshot of its current metrics."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO posts (id, url, text, author_handle, author_name,
                                   author_followers, created_at, lang, topic,
                                   source, first_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    author_followers = excluded.author_followers,
                    author_name      = excluded.author_name,
                    url              = excluded.url,
                    text             = excluded.text,
                    topic            = CASE WHEN posts.topic = ''
                                            THEN excluded.topic ELSE posts.topic END
                """,
                (
                    post.id, post.url, post.text, post.author_handle,
                    post.author_name, post.author_followers,
                    _iso(post.created_at), post.lang, post.topic, post.source,
                    _iso(post.fetched_at),
                ),
            )
            self.conn.execute(
                """
                INSERT INTO snapshots (post_id, ts, views, likes, retweets,
                                       replies, quotes, bookmarks)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(post_id, ts) DO UPDATE SET
                    views = excluded.views, likes = excluded.likes,
                    retweets = excluded.retweets, replies = excluded.replies,
                    quotes = excluded.quotes, bookmarks = excluded.bookmarks
                """,
                (
                    post.id, _iso(post.fetched_at), post.views, post.likes,
                    post.retweets, post.replies, post.quotes, post.bookmarks,
                ),
            )

    def record_many(self, posts: Iterable[Post]) -> int:
        count = 0
        for post in posts:
            self.record(post)
            count += 1
        return count

    def start_run(self, topic: str, source: str, query: str, now: datetime) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (topic, source, query, started_at) VALUES (?,?,?,?)",
                (topic, source, query, _iso(now)),
            )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: datetime, fetched: int,
                   error: Optional[str] = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, fetched=?, error=? WHERE id=?",
                (_iso(now), fetched, error, run_id),
            )

    def mark_alert(self, post_id: str, rule: str, now: datetime, views: int,
                   score: float) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO alerts (post_id, rule, fired_at, views, score)
                VALUES (?,?,?,?,?)
                ON CONFLICT(post_id, rule) DO UPDATE SET
                    fired_at = excluded.fired_at,
                    views    = MAX(alerts.views, excluded.views),
                    score    = excluded.score
                """,
                (post_id, rule, _iso(now), int(views), float(score)),
            )

    # ---------------------------------------------------------------- reads

    def alerted_views(self, post_id: str, rule: str) -> Optional[int]:
        """Views recorded the last time this rule fired for this post."""
        row = self.conn.execute(
            "SELECT views FROM alerts WHERE post_id=? AND rule=?", (post_id, rule)
        ).fetchone()
        return None if row is None else int(row["views"])

    def snapshots(self, post_id: str, limit: int = 50) -> List[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM snapshots WHERE post_id=? ORDER BY ts DESC LIMIT ?",
                (post_id, limit),
            )
        )

    def previous_snapshot(self, post_id: str, before: datetime,
                          min_gap_sec: int) -> Optional[sqlite3.Row]:
        """Newest snapshot at least ``min_gap_sec`` older than ``before``.

        Comparing against a snapshot taken seconds ago turns rounding noise
        into a huge fake velocity, so a minimum gap is enforced.
        """
        cutoff = before - timedelta(seconds=min_gap_sec)
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE post_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (post_id, _iso(cutoff)),
        ).fetchone()

    def author_baseline(self, handle: str, exclude_post_id: str,
                        min_posts: int = 3) -> Optional[float]:
        """Median peak views/hour across this author's other tracked posts.

        Returns None until enough history exists to make the median meaningful.
        """
        rows = self.conn.execute(
            """
            SELECT p.id, p.created_at, MAX(s.views) AS peak, MAX(s.ts) AS last_ts
            FROM posts p JOIN snapshots s ON s.post_id = p.id
            WHERE p.author_handle = ? AND p.id != ? AND s.views IS NOT NULL
            GROUP BY p.id
            """,
            (handle, exclude_post_id),
        ).fetchall()
        rates: List[float] = []
        for row in rows:
            if not row["peak"]:
                continue
            hours = (_dt(row["last_ts"]) - _dt(row["created_at"])).total_seconds() / 3600.0
            if hours <= 0.05:
                continue
            rates.append(float(row["peak"]) / hours)
        if len(rates) < min_posts:
            return None
        rates.sort()
        mid = len(rates) // 2
        if len(rates) % 2:
            return rates[mid]
        return (rates[mid - 1] + rates[mid]) / 2.0

    def tracked_posts(self, max_age_min: int) -> List[sqlite3.Row]:
        """Posts young enough to still be worth re-polling."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_min)
        return list(
            self.conn.execute(
                "SELECT * FROM posts WHERE created_at >= ? ORDER BY created_at DESC",
                (_iso(cutoff),),
            )
        )

    def latest_snapshot_rows(self, since_min: int, topic: Optional[str] = None
                             ) -> List[sqlite3.Row]:
        """Each post seen in the window, joined to its newest snapshot."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_min)
        sql = """
            SELECT p.*, s.ts AS snap_ts, s.views, s.likes, s.retweets,
                   s.replies, s.quotes, s.bookmarks
            FROM posts p
            JOIN snapshots s ON s.post_id = p.id
            WHERE p.created_at >= ?
              AND s.ts = (SELECT MAX(ts) FROM snapshots WHERE post_id = p.id)
        """
        params: List = [_iso(cutoff)]
        if topic:
            sql += " AND p.topic = ?"
            params.append(topic)
        return list(self.conn.execute(sql, params))

    def fetched_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(fetched), 0) AS n FROM runs WHERE started_at >= ?",
            (_iso(since),),
        ).fetchone()
        return int(row["n"])

    def fetched_by_day(self, days: int = 14) -> List[Tuple[str, int]]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = self.conn.execute(
            """
            SELECT substr(started_at, 1, 10) AS day, COALESCE(SUM(fetched), 0) AS n
            FROM runs WHERE started_at >= ? GROUP BY day ORDER BY day DESC
            """,
            (_iso(since),),
        ).fetchall()
        return [(r["day"], int(r["n"])) for r in rows]

    def prune(self, older_than_days: int = 14) -> int:
        """Drop posts (and their snapshots) older than the retention window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM posts WHERE created_at < ?", (_iso(cutoff),)
            )
            self.conn.execute(
                "DELETE FROM snapshots WHERE post_id NOT IN (SELECT id FROM posts)"
            )
        return cur.rowcount
