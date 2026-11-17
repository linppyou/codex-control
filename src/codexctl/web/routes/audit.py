from __future__ import annotations

import math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

import codexctl.sites.inventory as sites
from codexctl.web.dependencies import clean_text, current_admin


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock
    render = app.state.render
    devlogins = app.state.devlogins


    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(
        request: Request, audit_page: int = 1, alert_page: int = 1,
        actor: str = "", action: str = "", q: str = "", start: str = "", end: str = "", level: str = "",
        admin: tuple[str, str] = Depends(current_admin),
    ):
        filters = {key: value.strip()[:200] for key, value in
                   dict(actor=actor, action=action, q=q, start=start, end=end, level=level).items()}
        boundaries = {}
        for key in ("start", "end"):
            if filters[key]:
                try:
                    day = datetime.strptime(filters[key], "%Y-%m-%d").replace(tzinfo=sites.BEIJING)
                    boundaries[key] = int((day + timedelta(days=int(key == "end"))).timestamp())
                except (ValueError, OverflowError, OSError):
                    raise HTTPException(422, "请输入有效的 YYYY-MM-DD 日期") from None
        if "start" in boundaries and "end" in boundaries and boundaries["start"] >= boundaries["end"]:
            raise HTTPException(422, "结束日期不得早于开始日期")

        def query(table: str, page: int, page_key: str):
            conditions, values = [], []
            if "start" in boundaries:
                conditions.append("ts >= ?")
                values.append(boundaries["start"])
            if "end" in boundaries:
                conditions.append("ts < ?")
                values.append(boundaries["end"])
            exact = ("actor", "action") if table == "audit_log" else ("level",)
            for key in exact:
                if filters[key]:
                    conditions.append(f"{key} = ?")
                    values.append(filters[key])
            if filters["q"]:
                text = "actor || ' ' || action || ' ' || target || ' ' || COALESCE(detail, '')" if table == "audit_log" else "kind || ' ' || target || ' ' || COALESCE(message, '')"
                conditions.append(f"instr(lower({text}), lower(?)) > 0")
                values.append(filters["q"])
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            total = ctx.conn.execute(f"SELECT COUNT(*) FROM {table}{where}", values).fetchone()[0]
            pages = max(1, math.ceil(total / 50))
            page = min(max(1, page), pages)
            rows = ctx.conn.execute(f"SELECT * FROM {table}{where} ORDER BY ts DESC, id DESC LIMIT 50 OFFSET ?",
                                    [*values, (page - 1) * 50]).fetchall()
            params = dict(filters, audit_page=max(1, audit_page), alert_page=max(1, alert_page))
            def url(number: int):
                return "/audit?" + urlencode(dict(params, **{page_key: number})) + ("#audit-records" if page_key == "audit_page" else "#alert-records")
            return rows, dict(page=page, pages=pages, total=total,
                              previous=url(page - 1) if page > 1 else "", next=url(page + 1) if page < pages else "")

        with lock:
            devlogins.sweep()
            audit, audit_pagination = query("audit_log", audit_page, "audit_page")
            alerts, alert_pagination = query("alerts", alert_page, "alert_page")
            def audit_time(timestamp):
                return datetime.fromtimestamp(timestamp, sites.BEIJING).strftime("%m-%d %H:%M")
            audit_rows = [{"ts": audit_time(r["ts"]), "actor": clean_text(r["actor"]), "action": clean_text(r["action"]),
                           "target": clean_text(r["target"]), "detail": clean_text(r["detail"], 200)} for r in audit]
            alert_rows = [{"ts": audit_time(r["ts"]), "level": clean_text(r["level"], 16), "kind": clean_text(r["kind"]),
                           "target": clean_text(r["target"]), "message": clean_text(r["message"], 300),
                           "resolved": audit_time(r["resolved_at"]) if r["resolved_at"] else ""} for r in alerts]
        return render(request, "audit.html", admin=admin[0], csrf=admin[1], audit=audit_rows, alerts=alert_rows,
                      filters=filters, audit_pagination=audit_pagination, alert_pagination=alert_pagination)

