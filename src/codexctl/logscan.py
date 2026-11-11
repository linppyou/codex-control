from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import quota, repo
from .authfile import parse_rfc3339
from .context import Context
from .db import transaction
from .repo import USAGE_FIELDS
from .sysusers import UserError

log = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_COUNTER = 2**53
FUTURE_SLACK = 300


@dataclass(frozen=True)
class TokenCount:
    ts: int
    totals: dict[str, int] | None  # None 表示该事件不含用量（只更新额度）
    rate_limits: dict | None


def parse_line(line: str) -> TokenCount | None:
    try:
        d = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or d.get("type") != "event_msg":
        return None
    payload = d.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    stamp = d.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        ts = parse_rfc3339(stamp)
    except ValueError:
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    totals = None
    if isinstance(usage, dict):
        totals = {}
        for f in USAGE_FIELDS:
            v = usage.get(f, 0)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < MAX_COUNTER:
                return None
            totals[f] = v
    rate_limits = payload.get("rate_limits")
    return TokenCount(ts, totals, rate_limits if isinstance(rate_limits, dict) else None)


def usage_delta(prev: dict[str, int] | None, cur: dict[str, int]) -> dict[str, int]:
    if prev is None or cur["total_tokens"] < prev.get("total_tokens", 0):
        return dict(cur)
    return {f: max(cur[f] - prev.get(f, 0), 0) for f in USAGE_FIELDS}


def scan_file(ctx: Context, username: str, path: Path) -> int:
    """从上次位置继续解析一个会话文件，返回写入的用量事件数。只消费以换行结尾的完整行，分块读取。"""
    try:
        uid = ctx.lookup_user(username, ctx.settings.min_uid).uid
    except UserError:
        return 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return 0
    count = 0
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
            return 0
        row = repo.get_offset(ctx.conn, str(path))
        offset, prev = 0, None
        if row is not None and row["inode"] == st.st_ino and row["offset"] <= st.st_size:
            offset = row["offset"]
            prev = json.loads(row["last_totals"]) if row["last_totals"] else None
        now = ctx.now()
        lo, hi = now - ctx.settings.log_lookback_days * 86400, now + FUTURE_SLACK
        while True:
            f.seek(offset)
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            end = chunk.rfind(b"\n")
            if end < 0:
                if len(chunk) >= MAX_LINE:
                    log.warning("跳过无换行的超长数据 %s（%d 字节）", path, len(chunk))
                    with transaction(ctx.conn):
                        repo.set_offset(ctx.conn, str(path), st.st_ino, offset + len(chunk),
                                        json.dumps(prev) if prev is not None else None)
                    offset += len(chunk)
                    continue
                break
            with transaction(ctx.conn):
                for raw in chunk[: end + 1].splitlines():
                    tc = parse_line(raw.decode("utf-8", errors="replace"))
                    if tc is None or not lo <= tc.ts <= hi:
                        continue
                    account_pk = repo.account_at(ctx.conn, username, tc.ts)
                    if tc.totals is not None:
                        delta = usage_delta(prev, tc.totals)
                        prev = tc.totals
                        if delta["total_tokens"] > 0:
                            repo.insert_usage(ctx.conn, username, account_pk, tc.ts, delta, str(path))
                            count += 1
                    if tc.rate_limits and account_pk is not None:
                        for w in quota.windows_from_rate_limits(tc.rate_limits):
                            repo.insert_quota_sample(
                                ctx.conn, account_pk, tc.ts, w.win, w.used_percent, w.window_minutes,
                                w.resets_at, "log",
                            )
                offset += end + 1
                repo.set_offset(ctx.conn, str(path), st.st_ino, offset, json.dumps(prev) if prev is not None else None)
    return count


def _safe_dir(p: Path, uid: int | None) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and (uid is None or st.st_uid == uid)


def session_files(home: Path, since: int, *, uid: int | None = None) -> list[Path]:
    base = home / ".codex" / "sessions"
    if not (_safe_dir(home / ".codex", uid) and _safe_dir(base, uid)):
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            p = Path(dirpath) / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mtime >= since:
                found.append(p)
    return sorted(found)


def scan_all(ctx: Context) -> int:
    since = ctx.now() - ctx.settings.log_lookback_days * 86400
    total = 0
    for name in repo.users_with_recent_assignments(ctx.conn, since):
        try:
            user = ctx.lookup_user(name, ctx.settings.min_uid)
        except UserError:
            continue
        for path in session_files(user.home, since, uid=user.uid):
            try:
                total += scan_file(ctx, name, path)
            except Exception as e:  # 单个文件出错不能影响其他文件
                log.warning("扫描会话日志失败 %s：%r", path, e)
    for acc in repo.list_accounts(ctx.conn):
        quota.evaluate_thresholds(ctx, acc)
    return total
