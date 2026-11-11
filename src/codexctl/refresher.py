from __future__ import annotations

import logging

from . import accounts, events, oauth, repo
from .config import Settings
from .context import Context
from .db import transaction
from .locks import account_lock

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (60, 300, 900, 3600)
MIN_REFRESH_GAP = 3600


def needs_refresh(acc: repo.Account, now: int, settings: Settings) -> bool:
    if acc.status != "active":
        return False
    if acc.next_retry_at is not None and now < acc.next_retry_at:
        return False
    if now - acc.last_refresh < MIN_REFRESH_GAP:
        return False
    if now - acc.last_refresh >= settings.refresh_interval_days * 86400:
        return True
    return acc.access_expires_at - now < settings.refresh_min_validity_days * 86400


def refresh_account(ctx: Context, acc: repo.Account, actor: str = "scheduler", *, blocking: bool = True) -> bool:
    with account_lock(ctx.settings.state_dir, acc.label, blocking=blocking):
        return _refresh_locked(ctx, acc, actor)


def _refresh_locked(ctx: Context, acc: repo.Account, actor: str) -> bool:
    fresh = repo.get_account(ctx.conn, acc.label)
    if fresh is None:
        return False
    if fresh.last_refresh != acc.last_refresh:
        return True  # 其他进程已经刷新过
    if fresh.status == "needs_relogin":
        return False
    acc = fresh
    now = ctx.now()
    bundle = accounts.load_bundle(ctx, acc)
    client_id = bundle.client_id or ctx.settings.oauth_client_id
    try:
        result = oauth.refresh_tokens(ctx.http, ctx.settings.oauth_token_url, client_id, bundle.refresh_token)
    except oauth.RefreshError as e:
        with transaction(ctx.conn):
            if e.retryable:
                delay = BACKOFF_SECONDS[min(acc.retry_count, len(BACKOFF_SECONDS) - 1)]
                repo.mark_retry(ctx.conn, acc.id, str(e), now + delay)
                msg = f"刷新失败，{delay} 秒后重试：{e}"
                level = "warning"
                if acc.access_expires_at - now < 86400:
                    level = "critical"
                    msg += "；access_token 将在 24 小时内过期，用户端 Codex 可能自行刷新"
                events.raise_alert(ctx.conn, level, "refresh_retry", acc.label, msg, now=now)
            else:
                repo.mark_needs_relogin(ctx.conn, acc.id, str(e))
                events.raise_alert(
                    ctx.conn, "critical", "refresh_failed", acc.label, f"刷新被拒绝，需要重新登录：{e}", now=now
                )
            events.audit(ctx.conn, actor, "account.refresh_failed", acc.label, str(e), now=now)
        log.warning("刷新 %s 失败：%s", acc.label, e)
        return False
    try:
        new = bundle.with_refreshed(
            access_token=result.access_token, refresh_token=result.refresh_token, id_token=result.id_token, now=now
        )
        # 先把新凭证持久化到保险库，再发布文件：服务端已轮换 token，保险库必须是权威来源。
        with transaction(ctx.conn):
            repo.store_secret(ctx.conn, acc.id, ctx.vault.encrypt(new.to_bytes(), accounts._aad(acc.label)), now)
            repo.update_token_meta(
                ctx.conn,
                acc.id,
                email=new.email,
                plan_type=new.plan_type,
                last_refresh=now,
                access_expires_at=new.access_expires_at,
            )
            for kind in ("refresh_retry", "refresh_failed"):
                events.resolve_alert(ctx.conn, kind, acc.label, now=now)
            events.audit(ctx.conn, actor, "account.refresh", acc.label, now=now)
    except Exception as e:
        msg = f"服务端已轮换 token，但新凭证未能保存：{type(e).__name__}"
        with transaction(ctx.conn):
            repo.mark_needs_relogin(ctx.conn, acc.id, msg)
            events.raise_alert(ctx.conn, "critical", "refresh_failed", acc.label, msg, now=now)
            events.audit(ctx.conn, actor, "account.refresh_failed", acc.label, msg, now=now)
        log.error("刷新 %s：%s", acc.label, msg)
        return False
    try:
        accounts.publish_account(ctx, repo.get_account(ctx.conn, acc.label), new)
    except Exception:
        with transaction(ctx.conn):
            events.raise_alert(
                ctx.conn, "critical", "publish_failed", acc.label,
                "凭证已更新但发布失败，请执行 account republish", now=now,
            )
        log.exception("发布 %s 失败", acc.label)
    else:
        with transaction(ctx.conn):
            events.resolve_alert(ctx.conn, "publish_failed", acc.label, now=now)
    return True


def refresh_due(ctx: Context) -> list[str]:
    now = ctx.now()
    refreshed = []
    for acc in repo.list_accounts(ctx.conn):
        try:
            if needs_refresh(acc, now, ctx.settings) and refresh_account(ctx, acc):
                refreshed.append(acc.label)
        except Exception:
            log.exception("刷新 %s 时出现未预期错误", acc.label)
    return refreshed
