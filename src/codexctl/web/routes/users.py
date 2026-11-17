from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

import codexctl.accounts.repository as account_repo
import codexctl.db as db
import codexctl.gateway.storage as storage
import codexctl.sites.inventory as sites
import codexctl.usage.pricing as pricing
import codexctl.usage.repository as usage_repo
import codexctl.users.assignments as assignments
import codexctl.users.procs as procs
import codexctl.users.repository as user_repo
import codexctl.users.service as useradmin
import codexctl.users.webauth as webauth
from codexctl.formatting import fmt_ts
from codexctl.usage.analytics import group_users, usage_windows
from codexctl.users.sysusers import UserError
from codexctl.web.dependencies import clean_text, current_admin, current_user
from codexctl.web.presenters import HANDLED, _current_account_quota
from codexctl.web.responses import _redirect


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins

    def actor_of(admin: tuple[str, str]) -> str:
        return f"web:{admin[0]}"


    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        show_all = request.query_params.get("all") == "1"
        with lock:
            devlogins.sweep()
            now = ctx.now()
            hidden = set(ctx.settings.hidden_users)
            book = pricing.PriceBook(usage_repo.list_prices(ctx.conn))
            usage = {
                u["raw"]: u
                for u in group_users(
                    usage_repo.usage_by_user_model_windows(ctx.conn, *usage_windows(ctx)), {}, book
                )
            }
            accounts_by_id = {a.id: a for a in account_repo.list_accounts(ctx.conn)}
            assigned = {a.username: accounts_by_id.get(a.account_pk) for a in user_repo.all_active(ctx.conn)}
            key_users = storage.user_key_names(ctx.conn) if ctx.settings.gateway_enabled else set()
            managed = ctx.settings.manages_local_codex
            local_users = {u.name: u for u in ctx.list_users(ctx.settings.min_uid)} if managed else {}
            names = set(local_users) | key_users
            if not managed:
                names.update((set(webauth.list_admins(ctx.conn)) - {"admin"}) | set(assigned))
            rows = []
            for name in sorted(names):
                if not show_all and name in hidden:
                    continue
                u = local_users.get(name)
                link = ctx.publisher.read_link(u) if u else None
                stale = procs.stale_procs(
                    procs.list_codex_procs(u.uid, ctx.settings.codex_bin.parent, home=u.home),
                    user_repo.last_assignment_change(ctx.conn, name),
                ) if u else []
                used = usage.get(name, {})
                account = assigned.get(name)
                rows.append({
                    "name": name,
                    "key": name in key_users,
                    "account": clean_text(account.name) if account else "",
                    "account_label": clean_text(account.label) if account and account.display_name else "",
                    "link": clean_text(link.kind) if link else "—",
                    "target": clean_text(link.target or "", 200) if link else "",
                    "t24": used.get("today", 0),
                    "t7": used.get("week", 0),
                    "c24": used.get("cost_today"),
                    "c7": used.get("cost_week"),
                    "u24": used.get("unpriced_today", 0),
                    "u7": used.get("unpriced_week", 0),
                    "stale": stale,
                    "stale_title": "；".join(
                        f"pid {p.pid} {clean_text(p.exe, 80)} 于 {fmt_ts(p.started_at)} 启动" for p in stale
                    ),
                })
            choices = [
                {"label": a.label, "name": clean_text(a.name)}
                for a in account_repo.list_accounts(ctx.conn) if a.status != "disabled"
            ]
        return render(
            request, "users.html", admin=admin[0], csrf=admin[1], rows=rows, choices=choices, show_all=show_all
        )


    @app.post("/users/create")
    def users_create(
        username: str = Form(""), password: str = Form(""), password2: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        name = username.strip()
        if not ctx.settings.manages_local_codex:
            if name in {"admin", "root"} or password != password2 or len(password) > 128:
                return _redirect("/users", err="请使用非保留用户名，并输入两个相同的有效密码")
            with lock:
                try:
                    if storage.user_key(ctx.conn, name) is not None:
                        raise webauth.AuthError("用户已存在，请在用户详情中设置登录密码")
                    webauth.create_admin(ctx.conn, name, password, now=ctx.now())
                    with db.transaction(ctx.conn):
                        storage.ensure_user_key(ctx.conn, ctx.vault, name, actor_of(admin), ctx.now())
                except webauth.AuthError as exc:
                    return _redirect("/users", err=str(exc))
            return _redirect("/users", msg=f"用户 {name} 已创建，请分配账号")
        with lock:
            devlogins.sweep()
        try:
            useradmin.validate_new_user(ctx, name, password, password2, panel_login=True)
        except HANDLED as e:
            return _redirect("/users", err=str(e))
        # codex 首次安装可能耗时较长，放在全局锁之外，避免阻塞面板其它请求
        try:
            codex_note = useradmin.ensure_codex(ctx)
        except HANDLED as e:
            return _redirect("/users", err=str(e))
        with lock:
            try:
                user, note = useradmin.create(
                    ctx, name, password, password2, actor_of(admin), codex_note=codex_note, panel_login=True,
                )
            except HANDLED as e:
                return _redirect("/users", err=str(e))
        msg = f"用户 {user.name} 已创建（uid {user.uid}，家目录 {user.home}）"
        if note:
            msg += f"，{note}"
        msg += "；已开通面板登录，可用同一组用户名和密码登录。请在列表勾选后分配 Codex 账号"
        return _redirect("/users", msg=msg)


    @app.post("/users/assign")
    def users_assign(
        users: list[str] = Form([]), account: str = Form(""), admin: tuple[str, str] = Depends(current_admin)
    ):
        if not users:
            return _redirect("/users", err="请先选择用户")
        if not account:
            return _redirect("/users", err="请选择账号")
        ok, bad, backups = [], [], 0
        with lock:
            devlogins.sweep()
            hidden = set(ctx.settings.hidden_users)
            for name in users:
                if name in hidden:
                    bad.append(f"{clean_text(name, 32)}：隐藏用户不可分配")
                    continue
                try:
                    if assignments.assign(ctx, name, account, actor_of(admin)) is not None:
                        backups += 1
                    ok.append(name)
                except HANDLED as e:
                    bad.append(f"{clean_text(name, 32)}：{e}")
        msg = f"已分配 {len(ok)} 个用户到 {account}" if ok else ""
        if backups:
            msg += f"（{backups} 个用户的原 auth.json 已备份）"
        for name in ok if ctx.settings.manages_local_codex else ():
            n = len(assignments.stale_codex_procs(ctx, name))
            if n:
                msg += (
                    f"。{clean_text(name, 32)} 有 {n} 个旧 codex 进程仍在使用旧账号，"
                    "通知对方后点『重启 codex』"
                )
        return _redirect("/users", msg=msg, err="；".join(bad))


    @app.post("/users/unassign")
    def users_unassign(users: list[str] = Form([]), admin: tuple[str, str] = Depends(current_admin)):
        if not users:
            return _redirect("/users", err="请先选择用户")
        ok, bad = 0, []
        done: list[str] = []
        with lock:
            devlogins.sweep()
            for name in users:
                try:
                    if assignments.unassign(ctx, name, actor_of(admin)):
                        ok += 1
                        done.append(name)
                except HANDLED as e:
                    bad.append(f"{clean_text(name, 32)}：{e}")
        msg = f"已回收 {ok} 个用户" if ok else ""
        for name in done if ctx.settings.manages_local_codex else ():
            n = len(assignments.stale_codex_procs(ctx, name))
            if n:
                msg += (
                    f"。{clean_text(name, 32)} 有 {n} 个旧 codex 进程仍在使用旧账号，"
                    "通知对方后点『重启 codex』"
                )
        return _redirect("/users", msg=msg, err="；".join(bad))


    def _safe_user_next(value: str) -> str:
        ok = value == "/users" or (value.startswith("/users/") and "\\" not in value and value.isprintable())
        return value if ok else "/users"


    def fmt_duration(seconds: int) -> str:
        if seconds <= 0:
            return "< 1 秒"
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, rem_secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} 分钟" if rem_secs == 0 else f"{minutes} 分 {rem_secs} 秒"
        hours, rem_mins = divmod(minutes, 60)
        return f"{hours} 小时" if rem_mins == 0 else f"{hours} 小时 {rem_mins} 分钟"


    def _format_session_id(session_file: str) -> str:
        base = os.path.basename(session_file)
        if base.startswith("rollout-") and base.endswith(".jsonl"):
            return base[len("rollout-") : -len(".jsonl")]
        if base.endswith(".jsonl"):
            return base[: -len(".jsonl")]
        return base


    def _session_titles(home: Path, uid: int, session_files: list[str]) -> dict[str, str]:
        """Read the most recent title updates from the user's Codex session index."""
        wanted = {}
        for path in session_files:
            match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$", os.path.basename(path))
            if match:
                wanted[match.group(1).lower()] = path
        if not wanted:
            return {}
        try:
            directory = os.open(home / ".codex", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                st = os.fstat(directory)
                if st.st_uid != uid:
                    return {}
                fd = os.open("session_index.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid != uid:
                    return {}
                # Bound memory and work for a user-controlled file; the index is append-only.
                offset = max(0, st.st_size - 8 * 1024 * 1024)
                f.seek(offset)
                if offset:
                    f.readline(8 * 1024 * 1024)
                lines = f.read(8 * 1024 * 1024).splitlines()
        except OSError:
            return {}
        titles = {}
        for line in reversed(lines):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                thread_id, title = row.get("id"), row.get("thread_name")
                if not isinstance(thread_id, str) or not isinstance(title, str):
                    continue
                path = wanted.get(thread_id.lower())
                if path and path not in titles and title.strip():
                    titles[path] = " ".join(title.split())[:200]
            except (ValueError, UnicodeError):
                continue
        return titles


    def _shorten_cwd(cwd: str | None, home: Path | None) -> str | None:
        if not cwd:
            return None
        if home:
            home_str = str(home)
            if cwd == home_str:
                return "~"
            if cwd.startswith(home_str + "/"):
                return "~" + cwd[len(home_str) :]
        return cwd


    def extract_session_meta(path_str: str, user_uid: int | None = None) -> dict[str, str | None]:
        meta: dict[str, str | None] = {"cwd": None, "first_prompt": None}
        try:
            fd = os.open(path_str, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
        except OSError:
            return meta
        try:
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or (user_uid is not None and st.st_uid != user_uid):
                    return meta
                chunk = f.read(16 * 1024)
        except OSError:
            return meta

        for raw in chunk.splitlines()[:20]:
            try:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            payload = d.get("payload")
            if isinstance(payload, dict):
                if not meta["cwd"]:
                    cwd = payload.get("cwd") or payload.get("working_directory") or payload.get("project_dir")
                    if isinstance(cwd, str) and cwd.strip():
                        meta["cwd"] = cwd.strip()
                if not meta["first_prompt"]:
                    p = None
                    if isinstance(payload.get("prompt"), str):
                        p = payload["prompt"]
                    elif isinstance(payload.get("text"), str) and payload.get("type") in ("user_input", "user_message"):
                        p = payload["text"]
                    elif payload.get("type") == "message" and payload.get("role") == "user":
                        content = payload.get("content")
                        if isinstance(content, str):
                            p = content
                        elif isinstance(content, list) and content and isinstance(content[0], dict):
                            p = content[0].get("text")
                    if isinstance(p, str) and p.strip():
                        clean_p = " ".join(p.strip().split())
                        meta["first_prompt"] = clean_p[:80] + ("…" if len(clean_p) > 80 else "")
            if meta["cwd"] and meta["first_prompt"]:
                break
        return meta


    @app.post("/users/{name}/fix")
    def user_fix(name: str, next: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        back = _safe_user_next(next)
        with lock:
            devlogins.sweep()
            try:
                assignments.reconcile_user(ctx, name, actor_of(admin))
            except HANDLED as e:
                return _redirect(back, err=str(e))
        return _redirect(back, msg=f"已修复 {clean_text(name, 32)}")


    @app.post("/users/{name}/restart-codex")
    def user_restart_codex(name: str, next: str = Form(""), admin: tuple[str, str] = Depends(current_admin)):
        back = _safe_user_next(next)
        with lock:
            devlogins.sweep()
            try:
                ended, unable = assignments.restart_codex(ctx, name, actor_of(admin))
            except HANDLED as e:
                return _redirect(back, err=str(e))
        label = clean_text(name, 32)
        if not ended and not unable:
            return _redirect(back, msg=f"{label} 没有旧 codex 进程")
        msg = f"已结束 {label} 的 {len(ended)} 个 codex 进程"
        err = f"部分进程无法结束：{', '.join(str(p) for p in unable)}" if unable else ""
        return _redirect(back, msg=msg, err=err)


    @app.get("/users/{name}", response_class=HTMLResponse)
    def user_detail_page(name: str, request: Request, days: int = 7, page: int = 1, model: str = "", account_filter: str = "", admin: tuple[str, str] = Depends(current_user)):
        is_admin = admin[0] == "admin"
        if not is_admin and name != admin[0]:
            raise HTTPException(status_code=403, detail="仅可查看本人数据")
        clean_name = clean_text(name.strip(), 32)
        if ctx.settings.gateway_enabled and name != clean_name:
            raise HTTPException(404)
        if not ctx.settings.manages_local_codex:
            with lock:
                key = storage.user_key(ctx.conn, clean_name)
                login = webauth.get_hash(ctx.conn, clean_name) is not None and clean_name != "admin"
                active = user_repo.active_assignment(ctx.conn, clean_name)
                if key is None and not login and active is None:
                    raise HTTPException(404)
                account = account_repo.get_account_by_pk(ctx.conn, active.account_pk) if active else None
                current_account_quota = _current_account_quota(ctx, account)
            return render(request, "user_detail.html", admin=admin[0], csrf=admin[1], username=clean_name,
                          account=account, current_account_quota=current_account_quota,
                          api_key=key, show_api_key=True, show_web_login=True, web_login=login)
        today, week = usage_windows(ctx)
        days = days if days in (7, 30, 90) else 7
        page = max(1, page)
        model, account_filter = model[:200], account_filter[:200]
        local_today = datetime.fromtimestamp(ctx.now(), sites.BEIJING).date()
        start_day = local_today - timedelta(days=days - 1)
        start_ts = int(datetime.combine(start_day, datetime.min.time(), sites.BEIJING).timestamp())
        end_ts = int(datetime.combine(local_today + timedelta(days=1), datetime.min.time(), sites.BEIJING).timestamp())
        with lock:
            if is_admin:
                devlogins.sweep()
            user = None
            try:
                user = ctx.lookup_user(clean_name, ctx.settings.min_uid)
            except UserError:
                pass
            total_sessions = usage_repo.user_session_count(ctx.conn, clean_name)
            show_api_key = ctx.settings.gateway_enabled
            key = storage.user_key(ctx.conn, clean_name) if show_api_key else None
            if is_admin and user is None and total_sessions == 0 and key is None:
                return _redirect("/users", err=f"用户 {clean_name} 不存在")

            book = pricing.PriceBook(usage_repo.list_prices(ctx.conn))

            active = user_repo.active_assignment(ctx.conn, clean_name)
            account = account_repo.get_account_by_pk(ctx.conn, active.account_pk) if active else None
            current_account_quota = _current_account_quota(ctx, account)
            link = ctx.publisher.read_link(user) if user else None
            stale = procs.stale_procs(
                procs.list_codex_procs(user.uid, ctx.settings.codex_bin.parent, home=user.home),
                user_repo.last_assignment_change(ctx.conn, clean_name),
            ) if user else []

            model_rows = usage_repo.usage_by_model_for_user(ctx.conn, clean_name, today, week)
            models = []
            summary = {
                "today": 0, "week": 0, "total": 0, "turns": 0,
                "cost_today": 0.0, "cost_week": 0.0, "cost_total": 0.0,
                "unpriced_today": 0, "unpriced_week": 0, "unpriced_total": 0,
                "today_input": 0, "today_output": 0, "week_input": 0, "week_output": 0,
            }
            for r in model_rows:
                m = {
                    "name": clean_text(r["model"]) if r["model"] else "未知模型",
                    "known": bool(r["model"]),
                    "today": r["today"] or 0, "week": r["week"] or 0, "total": r["total"] or 0,
                    "turns": r["turns"],
                }
                for k in ("today", "week", "total"):
                    cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
                    m[f"cost_{k}"] = cost
                    if m[k]:
                        if cost is None:
                            summary[f"unpriced_{k}"] += 1
                        else:
                            summary[f"cost_{k}"] += cost
                summary["today_input"] += r["today_input"] or 0
                summary["today_output"] += r["today_output"] or 0
                summary["week_input"] += r["week_input"] or 0
                summary["week_output"] += r["week_output"] or 0
                summary["today"] += m["today"]
                summary["week"] += m["week"]
                summary["total"] += m["total"]
                summary["turns"] += m["turns"]
                models.append(m)

            for k in ("today", "week", "total"):
                if summary[f"unpriced_{k}"] > 0 and summary[k] > 0 and summary[f"cost_{k}"] == 0.0:
                    summary[f"cost_{k}"] = None

            daily_rows = {r["day"]: r for r in usage_repo.user_daily_usage(ctx.conn, clean_name, start_ts, end_ts)}
            daily = []
            for i in range(days):
                day = start_day + timedelta(days=i)
                row = daily_rows.get(day.isoformat())
                daily.append({"day": day.isoformat(), "label": day.strftime("%m-%d"),
                              **{k: (row[k] or 0) if row else 0 for k in ("total", "input", "cached", "output", "turns")}})
            daily_max = max((d["total"] for d in daily), default=0)
            for i, d in enumerate(daily):
                d["x"] = round(45 + i * 900 / days, 2)
                d["width"] = round(max(2, 900 / days - 2), 2)
                d["height"] = round(max(0, d["total"]) / (daily_max or 1) * 150, 2)
                d["y"] = round(165 - d["height"], 2)
                d["show_label"] = i == 0 or i == days - 1 or (days <= 7 or (days <= 30 and i % 5 == 0) or (days > 30 and i % 15 == 0))
                d["center_x"] = round(d["x"] + d["width"] / 2, 2)
            daily_line_path = " ".join(f"{'M' if i == 0 else 'L'} {point['center_x']} {point['y']}"
                                       for i, point in enumerate(daily))
            filter_models, filter_accounts = usage_repo.user_session_filters(ctx.conn, clean_name)
            filtered_sessions = usage_repo.user_session_count(ctx.conn, clean_name, model, account_filter)
            pages = max(1, math.ceil(filtered_sessions / 20))
            page = min(page, pages)
            session_rows = usage_repo.user_recent_sessions(ctx.conn, clean_name, limit=20, offset=(page - 1) * 20, model=model, account=account_filter)
            def page_url(n: int) -> str:
                return "?" + urlencode({"days": days, "page": n, "model": model, "account_filter": account_filter}) + "#sessions"

            session_files = [r["session_file"] for r in session_rows]
            session_titles = _session_titles(user.home, user.uid, session_files) if user else {}
            breakdown = usage_repo.user_sessions_model_breakdown(ctx.conn, clean_name, session_files)
            costs: dict[str, tuple[float | None, int]] = {}
            for b in breakdown:
                c = book.cost(b["model"], b["input"], b["cached"], b["output"])
                curr_cost, curr_unpriced = costs.setdefault(b["session_file"], (0.0, 0))
                if c is None:
                    costs[b["session_file"]] = (curr_cost, curr_unpriced + 1)
                else:
                    costs[b["session_file"]] = ((curr_cost or 0.0) + c, curr_unpriced)

            sessions = []
            user_home = user.home if user else None
            user_uid = user.uid if user else None
            for r in session_rows:
                sf = r["session_file"]
                cost, unpriced = costs.get(sf, (0.0, 0))
                if unpriced > 0 and (r["total_tokens"] or 0) > 0 and cost == 0.0:
                    cost = None
                meta = extract_session_meta(sf, user_uid) if user_uid is not None or is_admin else {}
                started_at = r["started_at"]
                last_active_at = r["last_active_at"]
                duration = max(0, last_active_at - started_at)
                models_list = [clean_text(m) for m in (r["models"] or "").split(",") if m]
                accounts_list = [clean_text(a) for a in (r["accounts"] or "").split(",") if a]
                cwd = meta.get("cwd")
                sessions.append({
                    "session_file": sf,
                    "session_id": _format_session_id(sf),
                    "started_at": started_at,
                    "started_at_fmt": fmt_ts(started_at),
                    "last_active_at": last_active_at,
                    "last_active_at_fmt": fmt_ts(last_active_at),
                    "duration": duration,
                    "duration_fmt": fmt_duration(duration),
                    "turns": r["turns"],
                    "total_tokens": r["total_tokens"],
                    "input_tokens": r["input_tokens"],
                    "cached_input_tokens": r["cached_input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "reasoning_output_tokens": r["reasoning_output_tokens"],
                    "models": models_list,
                    "accounts": accounts_list,
                    "cost": cost,
                    "unpriced": unpriced > 0,
                    "cwd": cwd,
                    "cwd_short": _shorten_cwd(cwd, user_home),
                    "first_prompt": meta.get("first_prompt"),
                    "title": session_titles.get(sf) or meta.get("first_prompt") or "未命名会话",
                })

        return render(
            request, "user_detail.html", admin=admin[0], csrf=admin[1],
            username=clean_name, user=user, account=account, link=link, stale=stale,
            api_key=key, show_api_key=show_api_key,
            summary=summary, models=models, sessions=sessions, total_sessions=total_sessions,
            daily=daily, daily_max=daily_max, daily_line_path=daily_line_path,
            current_account_quota=current_account_quota, days=days, page=page, pages=pages,
            filtered_sessions=filtered_sessions, filter_models=filter_models, filter_accounts=filter_accounts,
            selected_model=model, selected_account=account_filter, page_url=page_url,
        )

