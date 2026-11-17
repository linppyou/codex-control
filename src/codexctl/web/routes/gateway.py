from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import Depends, Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import codexctl.accounts.repository as account_repo
import codexctl.db as db
import codexctl.gateway.storage as storage
import codexctl.sites.inventory as sites
import codexctl.usage.pricing as pricing
import codexctl.usage.repository as usage_repo
import codexctl.users.repository as user_repo
import codexctl.users.sysusers as sysusers
import codexctl.users.webauth as webauth
from codexctl.formatting import fmt_ts
from codexctl.gateway.storage import GatewayError
from codexctl.gateway.upstream import Gateway
from codexctl.web.dependencies import current_admin, current_user
from codexctl.web.responses import _redirect


def _profile(base: str, model: str, max_context: int | None = None) -> str:
    url = json.dumps(base.rstrip("/"), ensure_ascii=False)
    catalog = json.dumps(base.rstrip("/") + "/codex/models", ensure_ascii=False)
    context = f"\n# model_context_window = {max_context}" if type(max_context) is int and max_context > 0 else ""
    return f'''model_provider = "codexctl"
model = {json.dumps(model)}{context}

[model_providers.codexctl]
name = "Codex Control"
base_url = {url}
model_catalog_url = {catalog}
wire_api = "responses"
env_key = "CODEXCTL_API_KEY"
requires_openai_auth = false
supports_websockets = false

[model_providers.codexctl.capabilities]
remote_compaction = "v2"'''


def register(app, gateway: Gateway):
    ctx = gateway.ctx
    canonical = ctx.settings.gateway_client_version
    render = app.state.render

    def account(label: str):
        with db.connection(ctx.settings.db_path) as conn:
            result = account_repo.get_account(conn, label)
        if result is None:
            raise HTTPException(404, "账号不存在")
        return result

    @app.get("/accounts/{label}/models")
    def model_page(label: str, request: Request, admin: tuple[str, str] = Depends(current_admin)):
        acc = account(label)
        with db.connection(ctx.settings.db_path) as conn:
            rows = storage.account_models(conn, acc.id)
            cached = storage.get_catalog(conn, acc.id, canonical)
        entries = {m["slug"]: m for m in cached[0]["models"]} if cached else {}
        return render(request, "api_models.html", admin=admin[0], csrf=admin[1], account=acc, rows=rows,
                      entries=entries, version=canonical, synced=fmt_ts(cached[1]) if cached else None)

    @app.post("/accounts/{label}/models/refresh")
    async def model_refresh(label: str, admin: tuple[str, str] = Depends(current_admin)):
        acc = await run_in_threadpool(account, label)
        page = f"/accounts/{label}/models"
        try:
            await gateway.catalog(acc.id, canonical, force=True)
        except GatewayError as exc:
            return _redirect(page, err=str(exc))
        return _redirect(page, msg="已刷新模型目录")

    @app.post("/accounts/{label}/models/action")
    async def model_action(label: str, model: str = Form(""), action: str = Form(""),
                           admin: tuple[str, str] = Depends(current_admin)):
        acc = await run_in_threadpool(account, label)
        page = f"/accounts/{label}/models"
        actor = f"web:{admin[0]}"

        def apply():
            with db.connection(ctx.settings.db_path) as conn:
                storage.enable_model(conn, acc.id, model, action == "enable", actor, ctx.now())

        try:
            if action in ("enable", "disable"):
                await run_in_threadpool(apply)
            elif action in ("test", "test_fast"):
                await gateway.test_model(acc.id, model, fast=action == "test_fast", actor=actor)
            else:
                raise HTTPException(400, "无效操作")
        except GatewayError as exc:
            return _redirect(page, err=str(exc))
        return _redirect(page, msg=f"{model} 已{'通过测试' if action.startswith('test') else '更新'}")

    def user_exists(conn, name):
        if not sysusers.valid_name(name):
            return False
        if storage.user_key(conn, name) is not None:
            return True
        if not ctx.settings.manages_local_codex:
            return name != "admin" and webauth.get_hash(conn, name) is not None
        try:
            ctx.lookup_user(name, ctx.settings.min_uid)
        except sysusers.UserError:
            return False
        return True

    @app.get("/users/{name}/api-key")
    def key_config(name: str, request: Request, admin: tuple[str, str] = Depends(current_user)):
        if admin[0] not in ("admin", name) or not sysusers.valid_name(name):
            raise HTTPException(404)
        with db.connection(ctx.settings.db_path) as conn:
            conn.execute("BEGIN")
            key = storage.user_key(conn, name)
            if key is None:
                raise HTTPException(404, "API Key 不存在")
            token = storage.reveal_user_key(ctx.vault, key)
            active = user_repo.active_assignment(conn, name)
            account = account_repo.get_account_by_pk(conn, active.account_pk) if active else None
            cached = storage.get_catalog(conn, account.id, canonical) if account else None
            enabled = {row["model_id"] for row in storage.enabled_models(conn, account.id)} if account else set()
        info = next((m for m in cached[0]["models"] if m["slug"] in enabled), {}) if cached else {}
        model = info.get("slug") or min(enabled, default="")
        base = ctx.settings.gateway_base_url
        return render(request, "api_key_config.html", admin=admin[0], csrf=admin[1], token=token,
                      profile=_profile(base, model, info.get("max_context_window")) if model else None,
                      base=base, model=model, context_window=info.get("context_window"),
                      max_context=info.get("max_context_window"), username=name, account=account,
                      flash=False)

    @app.post("/users/{name}/api-key")
    def manage_user_key(name: str, action: str = Form(""), version: int = Form(0),
                        admin: tuple[str, str] = Depends(current_admin)):
        actor = f"web:{admin[0]}"
        path = f"/users/{name}"
        with db.connection(ctx.settings.db_path) as conn:
            if not user_exists(conn, name):
                raise HTTPException(404)
            try:
                if action == "create":
                    with db.transaction(conn):
                        storage.ensure_user_key(conn, ctx.vault, name, actor, ctx.now())
                    msg = "已生成 API Key"
                elif action == "rotate":
                    storage.rotate_user_key(conn, ctx.vault, name, version, actor, ctx.now())
                    msg = "已重置 API Key，旧密钥已失效"
                elif action in ("fast_enable", "fast_disable"):
                    storage.set_user_fast(conn, name, action == "fast_enable", actor, ctx.now())
                    msg = "已更新 Fast 权限"
                else:
                    raise HTTPException(400, "无效操作")
            except GatewayError as exc:
                return _redirect(path, err=str(exc))
        return _redirect(path, msg=msg)

    @app.post("/users/{name}/web-password")
    def web_password(name: str, password: str = Form(""), confirm: str = Form(""),
                     admin: tuple[str, str] = Depends(current_admin)):
        with db.connection(ctx.settings.db_path) as conn:
            if ctx.settings.manages_local_codex or name in {"admin", "root"} or not user_exists(conn, name):
                raise HTTPException(404)
            path = f"/users/{name}"
            if password != confirm or len(password) > 128:
                return _redirect(path, err="请输入两个相同的有效密码")
            try:
                if webauth.get_hash(conn, name) is None:
                    webauth.create_admin(conn, name, password, now=ctx.now())
                else:
                    webauth.set_password(conn, name, password, now=ctx.now())
                with db.transaction(conn):
                    storage.ensure_user_key(conn, ctx.vault, name, f"web:{admin[0]}", ctx.now())
            except webauth.AuthError as exc:
                return _redirect(path, err=str(exc))
        return _redirect(path, msg="已设置面板登录密码")

    @app.get("/usage/api")
    def usage_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        with db.connection(ctx.settings.db_path) as conn:
            book = pricing.PriceBook(usage_repo.list_prices(conn))
            midnight = datetime.fromtimestamp(ctx.now(), sites.BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
            summaries = storage.usage_summary(conn, int((midnight - timedelta(days=6)).timestamp()))
        rows = [
            dict(row, cost=book.cost(row["model_id"], row["input_tokens"], row["cached_input_tokens"], row["output_tokens"])
                 if row["input_tokens"] is not None and row["output_tokens"] is not None else None)
            for row in summaries
        ]
        return render(request, "api_usage.html", admin=admin[0], csrf=admin[1], rows=rows)
