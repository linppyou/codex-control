from __future__ import annotations

import asyncio
import time

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import codexctl.events as events
import codexctl.sites.registry as site_registry
from codexctl.formatting import fmt_ts
from codexctl.usage.analytics import user_comparison
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, days: int = 7, admin: tuple[str, str] = Depends(current_user)):
        if admin[0] != "admin":
            return _redirect(f"/users/{admin[0]}")
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            cards = _account_rows(ctx)
            cards.sort(key=lambda card: not (card["has_quota"] and card["remaining"] > 0))
            available_count = sum(card["has_quota"] and card["remaining"] > 0 for card in cards)
            servers = site_registry.list_servers(ctx.conn)
            analytics = user_comparison(ctx, days)
            site_analytics = site_comparison(ctx.conn, analytics["days"], ctx.now())
            alerts = [
                {
                    "level": clean_text(a["level"], 16), "kind": clean_text(a["kind"]),
                    "target": clean_text(a["target"]), "message": clean_text(a["message"], 300),
                    "ts": fmt_ts(a["ts"]),
                }
                for a in events.open_alerts(ctx.conn)
            ]
        return render(request, "dashboard.html", admin=admin[0], csrf=admin[1], cards=cards, alerts=alerts,
                      available_count=available_count, servers=servers, site_analytics=site_analytics,
                      **analytics, **_flash(msg, err))

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
