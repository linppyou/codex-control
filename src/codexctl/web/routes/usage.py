from __future__ import annotations

import csv
import io

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, Response

import codexctl.usage.pricing as pricing
import codexctl.usage.repository as usage_repo
from codexctl.usage.analytics import group_users, usage_user_rows, usage_windows
from codexctl.web.dependencies import clean_text, current_admin
from codexctl.web.responses import _csv_cell


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins


    @app.get("/usage", response_class=HTMLResponse)
    def usage_page(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        today, week = usage_windows(ctx)
        with lock:
            devlogins.sweep()
            book = pricing.PriceBook(usage_repo.list_prices(ctx.conn))
            periods = []
            for key, title, since in (("today", "今天", today), ("week", "近 7 天", week), ("all", "全部", 0)):
                models = []
                for r in usage_repo.usage_by_model(ctx.conn, since):
                    inp, cached, out = r["input"] or 0, r["cached"] or 0, r["output"] or 0
                    cost = book.cost(r["model"], inp, cached, out)
                    models.append({
                        "name": clean_text(r["model"]) if r["model"] else "未知模型", "known": bool(r["model"]),
                        "total": r["total"] or 0, "input": inp, "cached": cached, "output": out,
                        "turns": r["turns"], "cost": cost,
                    })
                total = sum(m["total"] for m in models)
                for m in models:
                    m["pct"] = round(m["total"] * 100 / total, 1) if total else 0
                priced = [m for m in models if m["cost"] is not None]
                periods.append({
                    "key": key, "title": title, "models": models, "total": total,
                    "input": sum(m["input"] for m in models), "cached": sum(m["cached"] for m in models),
                    "output": sum(m["output"] for m in models), "turns": sum(m["turns"] for m in models),
                    "cost": sum(m["cost"] for m in priced) if priced else None,
                    "unpriced": sum(1 for m in models if m["cost"] is None),
                })
            users = group_users(
                usage_repo.usage_by_user_model_windows(ctx.conn, today, week), usage_repo.usage_user_accounts(ctx.conn), book
            )
        return render(
            request, "usage.html", admin=admin[0], csrf=admin[1], periods=periods, users=users,
            prices=book.prices,
        )


    @app.get("/usage.csv")
    def usage_csv(request: Request, admin: tuple[str, str] = Depends(current_admin)):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "username", "model", "today_tokens", "7d_tokens", "total_tokens",
            "today_usd", "7d_usd", "total_usd", "turns",
        ])
        rows, book = usage_user_rows(ctx, lock)
        for r in rows:
            costs = []
            for k in ("today", "week", "total"):
                cost = book.cost(r["model"], r[f"{k}_input"], r[f"{k}_cached"], r[f"{k}_output"])
                costs.append("" if cost is None else f"{cost:.6f}".rstrip("0").rstrip(".") or "0")
            w.writerow([
                _csv_cell(r["username"]), _csv_cell(r["model"] or ""),
                r["today"] or 0, r["week"] or 0, r["total"] or 0, *costs, r["turns"],
            ])
        return Response(
            buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="usage.csv"'},
        )

