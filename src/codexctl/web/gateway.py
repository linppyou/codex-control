from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import db, pricing, repo, sysusers
from ..cli import fmt_ts
from ..gateway import storage, upstream
from ..gateway.storage import GatewayError
from ..gateway.upstream import Gateway
from .app import current_admin
from .views import _redirect


def require_admin(admin: tuple[str, str] = Depends(current_admin)) -> tuple[str, str]:
    if admin[0] != "admin":
        raise HTTPException(403, "仅管理员可以管理 API 网关")
    return admin


def _keys_url(owner: str | None = None, account_id: int | None = None) -> str:
    query = {}
    if owner:
        query["username"] = owner
    elif owner == "":
        query["unassigned"] = 1
    if account_id:
        query["account_id"] = account_id
    return "/api-keys" + ("?" + urlencode(query) if query else "")


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

    def users() -> list[str]:
        if not ctx.settings.manages_local_codex:
            return []
        hidden = set(ctx.settings.hidden_users)
        return [u.name for u in ctx.list_users(ctx.settings.min_uid) if u.name not in hidden]

    def account(label: str):
        with db.connection(ctx.settings.db_path) as conn:
            result = repo.get_account(conn, label)
        if result is None:
            raise HTTPException(404, "账号不存在")
        return result

    @app.get("/accounts/{label}/models")
    def model_page(label: str, request: Request, admin: tuple[str, str] = Depends(require_admin)):
        acc = account(label)
        with db.connection(ctx.settings.db_path) as conn:
            rows = storage.account_models(conn, acc.id)
            cached = storage.get_catalog(conn, acc.id, canonical)
        entries = {m["slug"]: m for m in cached[0]["models"]} if cached else {}
        return render(request, "api_models.html", admin=admin[0], csrf=admin[1], account=acc, rows=rows,
                      entries=entries, version=canonical, synced=fmt_ts(cached[1]) if cached else None)

    @app.post("/accounts/{label}/models/refresh")
    async def model_refresh(label: str, admin: tuple[str, str] = Depends(require_admin)):
        acc = await run_in_threadpool(account, label)
        page = f"/accounts/{label}/models"
        try:
            await gateway.catalog(acc.id, canonical, force=True)
        except GatewayError as exc:
            return _redirect(page, err=str(exc))
        return _redirect(page, msg="已刷新模型目录")

    @app.post("/accounts/{label}/models/action")
    async def model_action(label: str, model: str = Form(""), action: str = Form(""),
                           admin: tuple[str, str] = Depends(require_admin)):
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

    @app.get("/api-keys")
    async def keys_page(request: Request, username: str = "", account_id: int | None = None,
                        unassigned: bool = False, q: str = "", admin: tuple[str, str] = Depends(require_admin)):
        username = username.strip()
        q = q.strip()[:80]
        if username and (unassigned or not sysusers.valid_name(username)):
            raise HTTPException(400, "用户名格式无效")
        owner = "" if unassigned else username or None
        user_choices = await run_in_threadpool(users)
        if owner is None:
            def directory():
                with db.connection(ctx.settings.db_path) as conn:
                    counts = storage.key_counts_by_user(conn)
                users_with_keys = {name for name in counts if name}
                labels = sorted(set(user_choices) | users_with_keys)
                groups = [(name, *counts.get(name, (0, 0))) for name in labels if q.casefold() in name.casefold()]
                if "" in counts and q.casefold() in "未绑定":
                    groups.append(("", *counts[""]))
                return groups

            groups = await run_in_threadpool(directory)
            return render(request, "api_keys.html", admin=admin[0], csrf=admin[1], owner=None,
                          groups=groups, q=q, keys_url=_keys_url)

        can_create = bool(username and (not ctx.settings.manages_local_codex or username in user_choices))

        def selection():
            with db.connection(ctx.settings.db_path) as conn:
                candidates = [a for a in repo.list_accounts(conn) if a.status == "active"]
                assignment = repo.active_assignment(conn, username) if username else None
            accounts_by_id = {a.id: a for a in candidates}
            auto = accounts_by_id.get(assignment.account_pk) if assignment else None
            chosen = (accounts_by_id.get(account_id) if account_id else auto) if can_create else None
            return candidates, chosen

        candidates, chosen = await run_in_threadpool(selection)
        account_pk = chosen.id if chosen else None
        catalog_error = ""
        if chosen:
            try:
                await gateway.catalog(chosen.id, canonical)
            except GatewayError as exc:
                catalog_error = str(exc)

        def load_keys():
            with db.connection(ctx.settings.db_path) as conn:
                rows = storage.list_keys(conn, owner)
                options = [(row["model_id"], True) for row in storage.enabled_models(conn, account_pk)] if chosen else []
                by_account = {
                    pk: {model["model_id"] for model in storage.enabled_models(conn, pk)}
                    for pk in {row["account_pk"] for row in rows}
                }
            for row in rows:
                usable = by_account[row["account_pk"]]
                granted = {model["model_id"] for model in row["models"]}
                row["edit_models"] = [(model, model in usable) for model in sorted(usable | granted)]
            return rows, options

        rows, options = await run_in_threadpool(load_keys)
        if q:
            needle = q.casefold()
            rows = [row for row in rows if any(needle in value.casefold() for value in (
                row["name"], "cctl_" + row["public_id"], row["label"], row["display_name"] or "",
                *(model["model_id"] for model in row["models"]),
            ))]
        return render(request, "api_keys.html", admin=admin[0], csrf=admin[1], rows=rows, accounts=candidates,
                      account_pk=account_pk, requested_account=account_id, owner=owner, chosen=chosen,
                      users=user_choices, can_create=can_create, options=options, catalog_error=catalog_error,
                      now=ctx.now(), fmt_ts=fmt_ts, q=q, keys_url=_keys_url)

    def secret_page(request, admin, public_id, token):
        with db.connection(ctx.settings.db_path) as conn:
            key = storage.get_key(conn, public_id)
            catalog = storage.get_catalog(conn, key["account_pk"], canonical)
        model = key["default_model_id"]
        info = next((entry for entry in catalog[0]["models"] if entry["slug"] == model), {}) if catalog else {}
        base = ctx.settings.gateway_base_url
        return render(request, "api_key_config.html", admin=admin[0], csrf=admin[1], token=token,
                      profile=_profile(base, model, info.get("max_context_window")), base=base, model=model,
                      context_window=info.get("context_window"), max_context=info.get("max_context_window"),
                      username=key["username"], keys_url=_keys_url)

    @app.get("/api-keys/{public_id}/config")
    def key_config(public_id: str, request: Request, admin: tuple[str, str] = Depends(require_admin)):
        try:
            return secret_page(request, admin, public_id, None)
        except GatewayError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/api-keys/save")
    def key_save(request: Request, name: str = Form(""), account_pk: int = Form(0), model_ids: list[str] = Form([]),
                 default_model: str = Form(""), allow_fast: bool = Form(False), username: str = Form(""),
                 public_id: str = Form(""), admin: tuple[str, str] = Depends(require_admin)):
        username = username.strip()
        target = _keys_url(username if sysusers.valid_name(username) else None, account_pk)
        try:
            with db.connection(ctx.settings.db_path) as conn:
                existing = storage.get_key(conn, public_id) if public_id else None
                if existing is not None:
                    target = _keys_url(existing["username"] or "")
                if (username and ctx.settings.manages_local_codex and (existing is None or not existing["username"])
                        and username not in users()):
                    raise GatewayError(400, "invalid_key_user", "用户不在面板用户列表中")
                public_id, token = storage.save_key(
                    conn, actor=f"web:{admin[0]}", now=ctx.now(), name=name, account_pk=account_pk, model_ids=model_ids,
                    default_model=default_model, allow_fast=allow_fast, username=username,
                    public_id=public_id or None,
                )
                owner = (existing["username"] if existing else None) or username
        except GatewayError as exc:
            return _redirect(target, err=str(exc))
        if token:
            return secret_page(request, admin, public_id, token)
        return _redirect(_keys_url(owner), msg="已保存 API Key 设置")

    @app.post("/api-keys/{public_id}/rotate")
    def key_rotate(public_id: str, request: Request, version: int = Form(0),
                   admin: tuple[str, str] = Depends(require_admin)):
        try:
            with db.connection(ctx.settings.db_path) as conn:
                token = storage.rotate_key(conn, public_id, version, f"web:{admin[0]}", ctx.now())
        except GatewayError as exc:
            return _redirect("/api-keys", err=str(exc))
        return secret_page(request, admin, public_id, token)

    @app.post("/api-keys/{public_id}/revoke")
    def key_revoke(public_id: str, admin: tuple[str, str] = Depends(require_admin)):
        try:
            with db.connection(ctx.settings.db_path) as conn:
                owner = storage.revoke_key(conn, public_id, f"web:{admin[0]}", ctx.now())
        except GatewayError as exc:
            return _redirect("/api-keys", err=str(exc))
        return _redirect(_keys_url(owner), msg="已撤销 API Key")

    @app.post("/api-keys/{public_id}/delete")
    def key_delete(public_id: str, admin: tuple[str, str] = Depends(require_admin)):
        try:
            with db.connection(ctx.settings.db_path) as conn:
                owner = storage.delete_key(conn, public_id, f"web:{admin[0]}", ctx.now())
        except GatewayError as exc:
            return _redirect("/api-keys", err=str(exc))
        return _redirect(_keys_url(owner), msg="已删除 API Key")

    @app.get("/api-keys/usage")
    def usage_page(request: Request, admin: tuple[str, str] = Depends(require_admin)):
        with db.connection(ctx.settings.db_path) as conn:
            book = pricing.PriceBook(repo.list_prices(conn))
            summaries = storage.usage_summary(conn, ctx.now() - 7 * 86400)
        rows = [
            dict(row, cost=book.cost(row["model_id"], row["input_tokens"], row["cached_input_tokens"], row["output_tokens"])
                 if row["input_tokens"] is not None and row["output_tokens"] is not None else None)
            for row in summaries
        ]
        return render(request, "api_usage.html", admin=admin[0], csrf=admin[1], rows=rows)
