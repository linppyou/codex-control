from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

import codexctl.events as events
from codexctl.formatting import fmt_ts
from codexctl.usage.analytics import user_comparison
from codexctl.web.dependencies import clean_text, current_user
from codexctl.web.presenters import _account_rows, _complete_finished_logins
from codexctl.web.responses import _flash, _redirect
from codexctl.web.site_analytics import site_comparison


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins


    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, days: int = 7, admin: tuple[str, str] = Depends(current_user)):
        if admin[0] != "admin":
            return _redirect(f"/users/{admin[0]}")
        with lock:
            devlogins.sweep()
            msg, err = _complete_finished_logins(app, admin)
            cards = _account_rows(ctx)
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
                      site_analytics=site_analytics, **analytics, **_flash(msg, err))

