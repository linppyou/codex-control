"""Shared display formatting for CLI, analytics and web views."""
from __future__ import annotations

import time
import unicodedata


def window_name(minutes: int | None) -> str:
    if minutes is None:
        return "未知窗口"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}天"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时"
    return f"{minutes}分钟"


def fmt_ts(ts: int | None) -> str:
    return "-" if ts is None else time.strftime("%m-%d %H:%M", time.localtime(ts))


def clean_text(value: str, limit: int = 64) -> str:
    return "".join(
        "?" if (unicodedata.category(c)[0] in "CZ" and c != " ") else c for c in value[:limit]
    )

