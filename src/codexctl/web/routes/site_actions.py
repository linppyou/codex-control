from __future__ import annotations

import json
import re

from fastapi import BackgroundTasks, Depends, Form, HTTPException

import codexctl.db as db
import codexctl.events as events
import codexctl.locks as locks
import codexctl.sites.access as site_access
import codexctl.sites.inventory as inventory
import codexctl.sites.panel as panel
import codexctl.sites.registry as registry
from codexctl.web.dependencies import current_admin, current_user


def bump_generation(conn, server_id: str) -> None:
    conn.execute(
        """INSERT INTO site_inventory_generations(server_id, generation) VALUES (?, 1)
           ON CONFLICT(server_id) DO UPDATE SET generation = generation + 1""",
        (server_id,),
    )


def register(app) -> None:
    ctx = app.state.ctx
    lock = app.state.lock

    def target(server_id: str, site_id: str):
        server = registry.get(ctx.conn, server_id)
        if server is None:
            raise HTTPException(404, "服务器不存在")
        snapshot = inventory.snapshots(ctx.conn).get(server_id)
        matches = [row for row in snapshot.rows if row["id"] == site_id] if snapshot else []
        if len(matches) != 1 or snapshot.revision != server.revision:
            raise HTTPException(404, "站点不存在，请刷新列表")
        return server, matches[0]

    def key_for(server):
        if not (server.host_key and ctx.settings.site_ssh_enabled and ctx.settings.site_ssh_shared):
            raise HTTPException(503, "该服务器尚未配置共享 SSH 管理接入")
        try:
            return site_access.shared_key(ctx.settings)
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "共享 SSH 管理接入不可用") from exc

    def update(server, original, operation, result, actor):
        with lock, db.transaction(ctx.conn):
            current = registry.get(ctx.conn, server.id)
            record = ctx.conn.execute("SELECT revision, snapshot FROM site_inventories WHERE server_id=?", (server.id,)).fetchone()
            if current is None or current.revision != server.revision or record is None or record["revision"] != server.revision:
                raise HTTPException(409, "服务器配置已变化，请刷新列表核对")
            try:
                rows = json.loads(record["snapshot"])
                matches = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == original["id"]]
            except (TypeError, ValueError):
                matches = []
            if len(matches) != 1 or matches[0].get("name") != original.get("name") or matches[0].get("domains") != original["domains"]:
                raise HTTPException(409, "站点清单已变化，请刷新列表核对")
            row = matches[0]
            if operation == "delete":
                rows.remove(row)
            elif operation == "probe":
                row.update(public=result["public"], origin=result["origin"])
            elif operation == "waf":
                row.update(waf_enabled=result["enabled"], waf_checked_at=ctx.now(), waf_stale=False)
            else:
                site = result["site"]
                row.update(enabled=site["enabled"], remark=site["remark"], path=site["path"], management_stale=False)
            bump_generation(ctx.conn, server.id)
            ctx.conn.execute("UPDATE site_inventories SET snapshot=? WHERE server_id=?", (json.dumps(rows), server.id))
            events.audit(ctx.conn, f"web:{actor}", f"site.{operation}", f"{server.id}/{original['id']}", "ok", now=ctx.now())

    def problem(server, site_id, operation, actor, error):
        with lock, db.transaction(ctx.conn):
            if registry.get(ctx.conn, server.id):
                bump_generation(ctx.conn, server.id)
                if operation == "waf":
                    record = ctx.conn.execute("SELECT snapshot FROM site_inventories WHERE server_id=?", (server.id,)).fetchone()
                    try:
                        rows = json.loads(record["snapshot"]) if record else []
                        for row in rows:
                            if isinstance(row, dict) and str(row.get("id")) == site_id:
                                row["waf_stale"] = True
                        if record and isinstance(rows, list):
                            ctx.conn.execute("UPDATE site_inventories SET snapshot=? WHERE server_id=?", (json.dumps(rows), server.id))
                    except (TypeError, ValueError):
                        pass
                if error.kind == "unknown":
                    ctx.conn.execute("UPDATE site_inventories SET error=? WHERE server_id=?", ("操作结果待核对，请刷新采集", server.id))
            events.audit(ctx.conn, f"web:{actor}", f"site.{operation}", f"{server.id}/{site_id}", error.kind, now=ctx.now())
        code = 409 if error.kind == "conflict" else 404 if error.kind == "not_found" else 502
        raise HTTPException(code, str(error))

    def refresh_server(server, generation):
        try:
            with locks.account_lock(ctx.settings.state_dir, f"site-{server.id}"):
                inventory.refresh(ctx, server.id)
        except Exception:
            with lock, db.transaction(ctx.conn):
                current = registry.get(ctx.conn, server.id)
                entry = ctx.conn.execute("SELECT generation FROM site_inventory_generations WHERE server_id=?", (server.id,)).fetchone()
                if current and current.revision == server.revision and entry and entry["generation"] == generation:
                    ctx.conn.execute("UPDATE site_inventories SET attempted_at=?, error=? WHERE server_id=?", (ctx.now(), "网站清单刷新失败", server.id))

    @app.post("/servers/{server_id}/sites/refresh")
    def site_refresh(server_id: str, background: BackgroundTasks, admin: tuple[str, str] = Depends(current_admin)):
        with lock, db.transaction(ctx.conn):
            server = registry.get(ctx.conn, server_id)
            if server is None:
                raise HTTPException(404, "服务器不存在")
            existing = ctx.conn.execute("SELECT 1 FROM site_inventories WHERE server_id=?", (server_id,)).fetchone()
            if existing is None:
                raise HTTPException(404, "该服务器尚无网站清单")
            bump_generation(ctx.conn, server_id)
            generation = ctx.conn.execute("SELECT generation FROM site_inventory_generations WHERE server_id=?", (server_id,)).fetchone()[0]
            ctx.conn.execute("UPDATE site_inventories SET attempted_at=0 WHERE server_id=?", (server_id,))
        background.add_task(refresh_server, server, generation)
        return {"ok": True}

    @app.get("/servers/{server_id}/sites/refresh")
    def site_refresh_status(server_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            record = ctx.conn.execute("SELECT attempted_at, error FROM site_inventories WHERE server_id=?", (server_id,)).fetchone()
        if record is None:
            raise HTTPException(404, "该服务器尚无网站清单")
        return {"done": bool(record["attempted_at"]), "error": record["error"]}

    @app.get("/servers/{server_id}/sites/{site_id}/{operation}")
    def site_read(server_id: str, site_id: str, operation: str, admin: tuple[str, str] = Depends(current_user)):
        if not re.fullmatch(r"[0-9a-f]{32}", server_id):
            raise HTTPException(404, "服务器不存在")
        if operation not in {"inspect", "waf"}:
            raise HTTPException(404, "操作不存在")
        try:
            with locks.account_lock(ctx.settings.state_dir, f"site-{server_id}", blocking=False):
                with lock:
                    server, row = target(server_id, site_id)
                if not site_id.isascii() or not site_id.isdecimal() or not row.get("name"):
                    raise HTTPException(409, "该站点尚未采集宝塔信息")
                result = panel.call(server, key_for(server), "waf_read" if operation == "waf" else "inspect", site_id=site_id, name=row["name"])
                if operation == "waf":
                    update(server, row, "waf", result, admin[0])
                return result
        except locks.LockBusy as exc:
            raise HTTPException(409, "服务器正在处理其他网站操作，请稍后重试") from exc
        except panel.PanelError as exc:
            problem(server, site_id, operation, admin[0], exc)

    @app.post("/servers/{server_id}/sites/{site_id}/{operation}")
    def site_action(
        server_id: str, site_id: str, operation: str, background: BackgroundTasks,
        name: str = Form(""), enabled: str = Form(""), expected: str = Form(""),
        remark: str = Form(""), path: str = Form(""), confirm: str = Form(""),
        delete_ftp: str = Form("false"), delete_database: str = Form("false"), delete_path: str = Form("false"),
        admin: tuple[str, str] = Depends(current_user),
    ):
        if operation == "delete" and admin[0] != "admin":
            raise HTTPException(403, "仅管理员可以删除网站")
        if not re.fullmatch(r"[0-9a-f]{32}", server_id):
            raise HTTPException(404, "服务器不存在")
        if operation not in {"waf", "state", "remark", "delete", "probe"}:
            raise HTTPException(404, "操作不存在")
        if operation in {"waf", "state"} and (enabled not in {"true", "false"} or expected not in {"true", "false"}):
            raise HTTPException(422, "状态参数无效")
        if operation == "remark" and len(remark) > 500:
            raise HTTPException(422, "备注最多 500 个字符")
        if operation == "delete" and (confirm != "true" or any(value not in {"true", "false"} for value in (delete_ftp, delete_database, delete_path))):
            raise HTTPException(422, "删除范围无效")
        if operation == "delete" and delete_path == "true" and not path:
            raise HTTPException(422, "无法确认网站目录")
        try:
            with locks.account_lock(ctx.settings.state_dir, f"site-{server_id}", blocking=False):
                with lock:
                    server, row = target(server_id, site_id)
                    if operation != "probe" and (not site_id.isascii() or not site_id.isdecimal() or not row.get("name") or name != row["name"]):
                        raise HTTPException(409, "站点名称已变化或尚未采集，请刷新列表")
                    with db.transaction(ctx.conn):
                        bump_generation(ctx.conn, server_id)
                if operation == "probe":
                    with lock:
                        snapshots = inventory.snapshots(ctx.conn)
                    aliases = {domain.removeprefix("www.") for domain in row["domains"]}
                    duplicates = sum(
                        bool(aliases & {domain.removeprefix("www.") for domain in item["domains"]})
                        for snapshot in snapshots.values() for item in snapshot.rows
                    ) > 1
                    domains = tuple(row["domains"])
                    public = inventory._probe(domains, None, ctx.now)
                    if public["state"] == "down" and public["reason"] in {"timeout", "connect"}:
                        public = inventory._probe(domains, None, ctx.now)
                    result = {"public": public, "origin": inventory._probe(domains, server.host, ctx.now) if duplicates else None}
                else:
                    params = {"site_id": site_id, "name": name}
                    if operation in {"waf", "state"}:
                        params.update(enabled=enabled == "true", expected=expected == "true")
                    elif operation == "remark":
                        params["remark"] = remark
                    elif operation == "delete":
                        params.update(path=path, confirm=True, delete_ftp=delete_ftp == "true", delete_database=delete_database == "true", delete_path=delete_path == "true")
                    result = panel.call(server, key_for(server), "waf_write" if operation == "waf" else operation, **params)
                try:
                    update(server, row, operation, result, admin[0])
                except HTTPException:
                    if operation == "probe":
                        raise
                    problem(server, site_id, operation, admin[0], panel.PanelError(
                        "宝塔已执行操作，网站清单已变化，请刷新后核对", "unknown",
                    ))
                if operation in {"state", "remark", "delete"}:
                    background.add_task(inventory.refresh, ctx, server.id)
                return {"ok": True, **result}
        except locks.LockBusy as exc:
            raise HTTPException(409, "服务器正在处理其他网站操作，请稍后重试") from exc
        except panel.PanelError as exc:
            problem(server, site_id, operation, admin[0], exc)
