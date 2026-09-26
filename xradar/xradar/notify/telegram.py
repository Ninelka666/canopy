"""Telegram delivery: a bot token, a chat id, one HTTP call per alert."""

from __future__ import annotations

import html
from typing import List, Optional, Sequence

from .. import http
from ..model import Post
from ..scoring import Metrics, humanize

API_BASE = "https://api.telegram.org"
_SPARK = "▁▂▃▄▅▆▇█"


class TelegramError(Exception):
    pass


def sparkline(values: Sequence[Optional[int]]) -> str:
    """Tiny view-history chart, oldest to newest."""
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return ""
    low, high = min(clean), max(clean)
    if high <= low:
        return _SPARK[0] * len(clean)
    span = high - low
    return "".join(
        _SPARK[min(int((v - low) / span * (len(_SPARK) - 1)), len(_SPARK) - 1)]
        for v in clean
    )


def format_alert(post: Post, m: Metrics, rules: Sequence[str],
                 history: Sequence[Optional[int]] = ()) -> str:
    """Build the message body. Escaped for Telegram's HTML parse mode."""
    esc = html.escape
    views_label = f"~{humanize(m.views)}" if m.views_estimated else humanize(m.views)
    head = f"🚀 <b>{views_label} views</b> · {m.age_min:.0f} min · {humanize(m.vph_effective)}/h"

    author = f"@{esc(post.author_handle)}"
    if post.author_name:
        author += f" ({esc(post.author_name)})"
    second = (
        f"{author}\n{humanize(post.author_followers)} followers · "
        f"reach <b>×{m.reach_multiple:.1f}</b>"
    )

    stats = [f"♥ {humanize(post.likes)}", f"🔁 {humanize(post.retweets)}"]
    if m.engagement_rate is not None:
        stats.append(f"ER {m.engagement_rate * 100:.1f}%")
    if m.accel is not None:
        stats.append(f"accel ×{m.accel:.2f}")
    if m.author_outlier is not None:
        stats.append(f"vs author ×{m.author_outlier:.1f}")
    spark = sparkline(history)
    if spark:
        stats.append(spark)

    lines = [head, second, " · ".join(stats)]
    flags = [f"<code>{esc(r)}</code>" for r in rules]
    if m.views_estimated:
        flags.append("<i>views estimated from likes</i>")
    lines.append(" ".join(flags))

    text = post.text.strip().replace("\n", " ")
    if len(text) > 260:
        text = text[:257].rstrip() + "…"
    if text:
        lines.append(f"\n<blockquote>{esc(text)}</blockquote>")
    lines.append(post.url)
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, *, api_base: str = API_BASE,
                 post_json=http.post_json, disable_preview: bool = False):
        if not bot_token or not chat_id:
            raise TelegramError(
                "Telegram needs both a bot token and a chat id "
                "(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
            )
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = api_base
        self._post_json = post_json
        self.disable_preview = disable_preview

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        try:
            response = self._post_json(url, payload)
        except http.HttpError as exc:
            # Never let a token reach the log line.
            raise TelegramError(
                f"{method} failed: HTTP {exc.status} {exc.body[:200]}"
            ) from exc
        if not response.get("ok", False):
            raise TelegramError(
                f"{method} rejected: {response.get('description', response)}"
            )
        return response

    def send(self, text: str) -> dict:
        return self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": self.disable_preview,
            "link_preview_options": {"is_disabled": self.disable_preview},
        })

    def send_alert(self, post: Post, m: Metrics, rules: Sequence[str],
                   history: Sequence[Optional[int]] = ()) -> dict:
        return self.send(format_alert(post, m, rules, history))

    def check(self) -> str:
        """Verify credentials; returns the bot's username."""
        me = self._call("getMe", {})
        return (me.get("result") or {}).get("username", "?")
