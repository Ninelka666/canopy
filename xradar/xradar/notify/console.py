"""Terminal output: an aligned table and one-line alerts."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..model import Post
from ..scoring import Metrics, humanize

COLUMNS: Tuple[Tuple[str, int], ...] = (
    ("score", 6), ("views", 8), ("age", 6), ("views/h", 9),
    ("reach", 7), ("accel", 6), ("followers", 10), ("author", 18),
    ("rules", 26), ("text", 40),
)


def _cell(value: str, width: int) -> str:
    value = value.replace("\n", " ")
    if len(value) > width:
        value = value[: width - 1] + "…"
    return value.ljust(width)


def format_table(rows: Sequence[Tuple[Post, Metrics, List[str]]]) -> str:
    if not rows:
        return "no posts matched."
    out = [" ".join(_cell(name, width) for name, width in COLUMNS).rstrip()]
    out.append("─" * min(sum(w + 1 for _, w in COLUMNS) - 1, 160))
    for post, m, rules in rows:
        views = f"~{humanize(m.views)}" if m.views_estimated else humanize(m.views)
        cells = (
            f"{m.score:.1f}", views, f"{m.age_min:.0f}m",
            humanize(m.vph_effective), f"x{m.reach_multiple:.1f}",
            "-" if m.accel is None else f"x{m.accel:.2f}",
            humanize(post.author_followers), f"@{post.author_handle}",
            ",".join(rules) or "-", post.text.strip(),
        )
        out.append(" ".join(_cell(c, w) for c, (_, w) in zip(cells, COLUMNS)).rstrip())
    return "\n".join(out)


class ConsoleNotifier:
    def __init__(self, stream=None):
        import sys
        self.stream = stream or sys.stdout

    def send_alert(self, post: Post, m: Metrics, rules: Sequence[str],
                   history: Sequence[Optional[int]] = ()) -> None:
        views = f"~{humanize(m.views)}" if m.views_estimated else humanize(m.views)
        print(
            f"🚀 {views} views in {m.age_min:.0f}m ({humanize(m.vph_effective)}/h) "
            f"reach x{m.reach_multiple:.1f} @{post.author_handle} "
            f"[{','.join(rules)}] {post.url}",
            file=self.stream, flush=True,
        )
