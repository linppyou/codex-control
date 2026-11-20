from __future__ import annotations

import asyncio
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import codexctl.accounts.repository as account_repo
import codexctl.events as events
import codexctl.sites.registry as site_registry
from codexctl.formatting import fmt_ts
from codexctl.usage.analytics import dashboard_comparison_chart, user_comparison
from codexctl.web import server_metrics
from codexctl.web.dependencies import clean_text, current_admin, current_user
from codexctl.web.presenters import _account_rows, _complete_finished_logins
from codexctl.web.responses import _flash, _redirect
from codexctl.web.site_analytics import site_comparison


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins
    metrics_lock = asyncio.Lock()
    metrics_cache = {"key": None, "sampled_at": 0.0, "value": None}

    def read_trends(days: int) -> dict:
        now = ctx.now()
        analytics = user_comparison(ctx, days, now=now)
        analytics["site_analytics"] = site_comparison(ctx.conn, analytics["days"], now)
        return analytics

    def trend_charts(analytics: dict) -> dict:
        sites = analytics["site_analytics"]
        return {
            "user_chart": dashboard_comparison_chart(analytics["daily"], [u["name"] for u in analytics["ranking"]]),
            "site_chart": dashboard_comparison_chart(sites["daily"], [s["name"] for s in sites["ranking"]]),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, days: int = 7, admin: tuple[str, str] = Depends(current_user)):
        if admin[0] != "admin":
            return _redirect(f"/users/{admin[0]}")
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            cards = _account_rows(ctx)
            available, pending = [], []
            for card in cards:
                if card["status"] == "active" and card["has_quota"] and card["remaining"] > 0:
                    available.append(card)
                else:
                    pending.append(card)
            only_disabled = not cards and bool(account_repo.list_accounts(ctx.conn))
            servers = site_registry.list_servers(ctx.conn)
            analytics = read_trends(days)
            alerts = [
                {
                    "level": clean_text(a["level"], 16), "kind": clean_text(a["kind"]),
                    "target": clean_text(a["target"]), "message": clean_text(a["message"], 300),
                    "ts": fmt_ts(a["ts"]),
                }
                for a in events.open_alerts(ctx.conn)
            ]
            severity = {"critical": 3, "error": 2, "warning": 1, "warn": 1}
            top_alert = max(alerts, key=lambda alert: severity.get(alert["level"], 0), default=None)
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1], available=available, pending=pending,
                      only_disabled=only_disabled, alerts=alerts, top_alert=top_alert, servers=servers,
                      **analytics, **trend_charts(analytics), **_flash(msg, err))

    @app.get("/dashboard/trends", response_class=HTMLResponse, dependencies=[Depends(current_admin)])
    def trends(request: Request, days: int = 7):
        with lock:
            analytics = read_trends(days)
        return render(request, "_dashboard_trends.html", flash=False, **analytics, **trend_charts(analytics))

    def read_metrics(refresh: bool, requested_at: float) -> dict:
        with lock:
            servers = site_registry.list_servers(ctx.conn)
        key = tuple((server.id, server.revision) for server in servers)
        if metrics_cache["key"] == key:
            sampled_at = metrics_cache["sampled_at"]
            if sampled_at >= requested_at or (not refresh and time.monotonic() - sampled_at < 30):
                return metrics_cache["value"]
        readings = server_metrics.collect(servers, ctx.settings)
        with lock:
            current = {server.id: server.revision for server in site_registry.list_servers(ctx.conn)}
        readings = [
            reading for server, reading in zip(servers, readings)
            if current.get(server.id) == server.revision
        ]
        value = {"servers": readings, "updated_at": fmt_ts(ctx.now())}
        metrics_cache.update(key=key, sampled_at=time.monotonic(), value=value)
        return value

    @app.get("/dashboard/server-metrics")
    async def server_metrics_page(refresh: bool = False, admin: tuple[str, str] = Depends(current_admin)):
        requested_at = time.monotonic()
        async with metrics_lock:
            value = await run_in_threadpool(read_metrics, refresh, requested_at)
        return JSONResponse(value)
