from __future__ import annotations

import logging

from . import accounts, events, oauth, repo
from .config import Settings
from .context import Context
from .db import transaction

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (60, 300, 900, 3600)


def needs_refresh(acc: repo.Account, now: int, settings: Settings) -> bool:
    if acc.status != "active":
        return False
    if acc.next_retry_at is not None and now < acc.next_retry_at:
        return False
    if now - acc.last_refresh >= settings.refresh_interval_days * 86400:
        return True
    return acc.access_expires_at - now < settings.refresh_min_validity_days * 86400


def refresh_account(ctx: Context, acc: repo.Account, actor: str = "scheduler") -> bool:
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
                events.raise_alert(
                    ctx.conn, "warning", "refresh_retry", acc.label, f"刷新失败，{delay} 秒后重试：{e}", now=now
                )
            else:
                repo.set_status(ctx.conn, acc.id, "needs_relogin", str(e))
                events.raise_alert(
                    ctx.conn, "critical", "refresh_failed", acc.label, f"刷新被拒绝，需要重新登录：{e}", now=now
                )
            events.audit(ctx.conn, actor, "account.refresh_failed", acc.label, str(e), now=now)
        log.warning("刷新 %s 失败：%s", acc.label, e)
        return False
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
    accounts.publish_account(ctx, repo.get_account(ctx.conn, acc.label), new)
    return True


def refresh_due(ctx: Context) -> list[str]:
    now = ctx.now()
    refreshed = []
    for acc in repo.list_accounts(ctx.conn):
        if needs_refresh(acc, now, ctx.settings) and refresh_account(ctx, acc):
            refreshed.append(acc.label)
    return refreshed
