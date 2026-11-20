from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import codexctl.accounts.repository as account_repo
import codexctl.usage.quota as quota
import codexctl.usage.repository as usage_repo
import codexctl.users.repository as user_repo
from codexctl.accounts.authfile import parse_rfc3339
from codexctl.context import Context
from codexctl.db import transaction
from codexctl.usage.repository import USAGE_FIELDS
from codexctl.users.sysusers import UserError

log = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024
MAX_LINE = 1024 * 1024
MAX_COUNTER = 2**53
FUTURE_SLACK = 300
MAX_MODEL = 64


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


def parse_model(line: str) -> str | None:
    """从 turn_context 事件中取出当前模型名；不是该事件或字段无效时返回 None。"""
    try:
        d = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(d, dict) or d.get("type") != "turn_context":
        return None
    payload = d.get("payload")
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str):
        return None
    model = model.strip()
    return model[:MAX_MODEL] if model and model.isprintable() else None


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
        row = usage_repo.get_offset(ctx.conn, str(path))
        offset, prev, model = 0, None, None
        if row is not None and row["inode"] == st.st_ino and row["offset"] <= st.st_size:
            offset = row["offset"]
            prev = json.loads(row["last_totals"]) if row["last_totals"] else None
            model = row["last_model"]
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
                        usage_repo.set_offset(ctx.conn, str(path), st.st_ino, offset + len(chunk),
                                        json.dumps(prev) if prev is not None else None, model)
                    offset += len(chunk)
                    continue
                break
            with transaction(ctx.conn):
                for raw in chunk[: end + 1].splitlines():
                    text = raw.decode("utf-8", errors="replace")
                    if b'"turn_context"' in raw:
                        model = parse_model(text) or model
                        continue
                    tc = parse_line(text)
                    if tc is None or not lo <= tc.ts <= hi:
                        continue
                    account_pk = user_repo.account_at(ctx.conn, username, tc.ts)
                    if tc.totals is not None:
                        delta = usage_delta(prev, tc.totals)
                        prev = tc.totals
                        if delta["total_tokens"] > 0:
                            usage_repo.insert_usage(ctx.conn, username, account_pk, tc.ts, delta, str(path), model)
                            count += 1
                    if tc.rate_limits and account_pk is not None:
                        for w in quota.windows_from_rate_limits(tc.rate_limits):
                            usage_repo.insert_quota_sample(
                                ctx.conn, account_pk, tc.ts, w.win, w.used_percent, w.window_minutes,
                                w.resets_at, "log",
                            )
                offset += end + 1
                usage_repo.set_offset(
                    ctx.conn, str(path), st.st_ino, offset, json.dumps(prev) if prev is not None else None, model
                )
    return count


def _scanned_lines(f, limit: int):
    """逐行产出文件前 limit 字节中以换行结尾的完整行；无换行的超长数据整块跳过。"""
    offset, buf = 0, b""
    while offset < limit:
        chunk = f.read(min(CHUNK_SIZE, limit - offset))
        if not chunk:
            return
        offset += len(chunk)
        buf += chunk
        end = buf.rfind(b"\n")
        if end < 0:
            if len(buf) >= CHUNK_SIZE:
                buf = b""
            continue
        yield from buf[: end + 1].splitlines()
        buf = buf[end + 1:]


def backfill_file_models(ctx: Context, username: str, path: Path) -> int:
    """重读一个会话文件中已扫描过的部分，按 turn_context 给缺模型的用量补上模型名，返回更新条数。"""
    try:
        uid = ctx.lookup_user(username, ctx.settings.min_uid).uid
    except UserError:
        return 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return 0
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
            return 0
        row = usage_repo.get_offset(ctx.conn, str(path))
        same = row is not None and row["inode"] == st.st_ino and row["offset"] <= st.st_size
        limit = row["offset"] if same else st.st_size
        model, pairs = None, []
        for raw in _scanned_lines(f, limit):
            if b'"turn_context"' in raw:
                model = parse_model(raw.decode("utf-8", errors="replace")) or model
            elif model and b'"token_count"' in raw:
                tc = parse_line(raw.decode("utf-8", errors="replace"))
                if tc is not None and tc.totals is not None:
                    pairs.append((model, tc.ts))
    with transaction(ctx.conn):
        count = usage_repo.fill_usage_models(ctx.conn, username, str(path), pairs)
        if same and model and row["last_model"] is None:
            usage_repo.set_offset_model(ctx.conn, str(path), model)
    return count


def backfill_models(ctx: Context) -> int:
    """给加入模型统计之前入库的用量补上模型名（只改模型为空的记录），返回更新条数。"""
    total = 0
    for username, path in usage_repo.usage_files_missing_model(ctx.conn):
        try:
            total += backfill_file_models(ctx, username, Path(path))
        except Exception as e:  # 单个文件出错不能影响其他文件
            log.warning("回填模型失败 %s：%r", path, e)
    return total


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
    for name in user_repo.users_with_recent_assignments(ctx.conn, since):
        try:
            user = ctx.lookup_user(name, ctx.settings.min_uid)
        except UserError:
            continue
        for path in session_files(user.home, since, uid=user.uid):
            try:
                total += scan_file(ctx, name, path)
            except Exception as e:  # 单个文件出错不能影响其他文件
                log.warning("扫描会话日志失败 %s：%r", path, e)
    with transaction(ctx.conn):
        for acc in account_repo.list_accounts(ctx.conn):
            quota.evaluate_thresholds(ctx, acc)
    return total
