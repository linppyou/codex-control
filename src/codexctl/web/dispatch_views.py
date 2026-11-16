from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import dispatch_skills, dispatch_tasks, repo, site_registry, sites
from .app import current_admin, current_user
from .server_views import _time
from .views import _redirect

STATUS = {
    "preview": ("待人工确认", "info"), "queued": ("排队发送", "info"), "sending": ("正在发送", "warn"),
    "sent": ("已发送给 Codex", "ok"), "failed": ("发送失败", "bad"), "unknown": ("发送结果待核对", "warn"),
}
TURN_STATUS = {"inProgress": "Codex 正在处理", "completed": "Codex 轮次已结束",
               "failed": "Codex 轮次失败", "interrupted": "Codex 轮次已中断", "unknown": "Codex 轮次待核对"}


def register(app) -> None:
    ctx = app.state.ctx
    render = app.state.render
    lock = app.state.lock

    def get_task(task_id: str, username: str):
        row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or username not in {"admin", row["owner"], row["recipient"]}:
            raise HTTPException(404, "任务不存在")
        return row

    def task_page(request, who, data=None, *, server_id="", status=200, err=""):
        user, csrf = who
        assigning = user == "admin" and data is None
        pending = []
        with lock:
            if assigning:
                inventories = sites.snapshots(ctx.conn)
                for server in site_registry.list_servers(ctx.conn):
                    choices = dispatch_tasks.pending_sites(ctx.conn, server, inventories.get(server.id))
                    if choices:
                        pending.append((server, choices))
            assigned = [] if assigning else dispatch_tasks.list_targets(ctx.conn, user)
            catalog = [] if assigning else dispatch_skills.list_skills(ctx.conn)
            users = []
            if user == "admin":
                for item in repo.all_active(ctx.conn):
                    try:
                        dispatch_tasks.recipient(ctx, item.username)
                    except dispatch_tasks.TaskError:
                        continue
                    users.append(item.username)
            history = ctx.conn.execute(
                """SELECT id, recipient, source_url, title, status, created_at FROM dispatch_tasks
                   WHERE ?='admin' OR owner=? OR recipient=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 50""",
                (user, user, user),
            ).fetchall()
        targets = [row for row in assigned if row["endpoint"] and row["credential_env"]]
        servers = {row["server_id"]: row["server_name"] for row in targets}
        if pending and not any(server.id == server_id for server, _ in pending):
            server_id = pending[0][0].id
        return render(
            request, "dispatch_tasks.html", status=status, admin=user, csrf=csrf, servers=servers,
            targets=targets, users=users, data=data or {}, catalog=catalog,
            assigning=assigning, pending=pending, selected_server=server_id, assigned=assigned,
            status_map=STATUS, history=history, time=_time, err=err,
        )

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request, draft: str = "", server_id: str = "", who: tuple[str, str] = Depends(current_user)):
        data = None
        if draft:
            with lock:
                task = get_task(draft, who[0])
            target = json.loads(task["target_snapshot"])
            dispatcher, migration = json.loads(task["skills"])
            data = dict(
                server_id=target["server_id"], target=target["domain"], source=task["source_url"],
                recipient=task["recipient"], dispatcher=dispatcher["id"], migration=migration["id"],
            )
        return task_page(request, who, data, server_id=server_id)

    @app.post("/tasks/assign")
    def assign(
        request: Request, server_id: str = Form(""), site_id: str = Form(""), recipient: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        try:
            with lock:
                domain = dispatch_tasks.assign_site(ctx, server_id, site_id, recipient)
        except dispatch_tasks.TaskError as exc:
            return task_page(request, admin, server_id=server_id, status=422, err=str(exc))
        return _redirect("/tasks?" + urlencode({"server_id": server_id}), msg=f"{domain} 已分配给 {recipient}")

    @app.post("/tasks/preview")
    def preview(
        request: Request, server_id: str = Form(""), source: str = Form(""), target: str = Form(""),
        dispatcher: str = Form(""), migration: str = Form(""), recipient: str = Form(""),
        who: tuple[str, str] = Depends(current_user),
    ):
        try:
            with lock:
                task_id = dispatch_tasks.create_preview(
                    ctx, who[0], recipient, server_id, source, target, dispatcher, migration,
                )
        except dispatch_tasks.TaskError as exc:
            data = dict(server_id=server_id, target=target, source=source, recipient=recipient,
                        dispatcher=dispatcher, migration=migration)
            return task_page(request, who, data, status=422, err=str(exc))
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    def task_detail(request, who, task, *, status=200, error=""):
        user, csrf = who
        return render(
            request, "dispatch_detail.html", status=status, admin=user, csrf=csrf, task=task,
            target=json.loads(task["target_snapshot"]), skills=json.loads(task["skills"]),
            status_map=STATUS, turn_status=TURN_STATUS, kinds=dispatch_skills.KINDS,
            can_confirm=task["status"] == "preview" and user == task["owner"],
            time=_time, err=error,
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def detail(request: Request, task_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            task = get_task(task_id, who[0])
        return task_detail(request, who, task)

    @app.post("/tasks/{task_id}/confirm")
    def confirm(
        request: Request, task_id: str, confirmed: str = Form(""),
        who: tuple[str, str] = Depends(current_user),
    ):
        error = ""
        with lock:
            task = get_task(task_id, who[0])
            if task["owner"] != who[0]:
                raise HTTPException(403, "仅生成此预览的用户可以确认发送")
            if confirmed != "1":
                error = "请先核对提示词并勾选确认"
            else:
                try:
                    dispatch_tasks.confirm(ctx, task, who[0])
                except dispatch_tasks.TaskError as exc:
                    error = str(exc)
        if error:
            return task_detail(request, who, task, status=409, error=error)
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    @app.get("/skills", response_class=HTMLResponse)
    def skills(request: Request, edit: str = "", admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            catalog = dispatch_skills.list_skills(ctx.conn)
            entry = dispatch_skills.get(ctx.conn, edit) if edit else None
        if edit and entry is None:
            raise HTTPException(404, "Skill 不存在")
        return render(request, "dispatch_skills.html", admin=admin[0], csrf=admin[1],
                      catalog=catalog, entry=entry, kinds=dispatch_skills.KINDS)

    @app.post("/skills/save")
    def save_skill(
        skill_id: str = Form(""), kind: str = Form(""), label: str = Form(""),
        package: UploadFile | None = File(None), admin: tuple[str, str] = Depends(current_admin),
    ):
        try:
            data = package.file.read(dispatch_skills.MAX_UPLOAD + 1) if package and package.filename else None
            metadata = dispatch_skills.inspect(data) if data is not None else None
            with lock:
                dispatch_skills.save(ctx, skill_id or None, kind, label, data, metadata)
        except dispatch_skills.SkillError as exc:
            return _redirect(f"/skills?edit={skill_id}" if skill_id.isalnum() else "/skills", err=str(exc))
        return _redirect("/skills", msg="Skill 已保存")

    @app.get("/skills/{skill_id}/download")
    def download(skill_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            skill = dispatch_skills.get(ctx.conn, skill_id, with_archive=True)
        if skill is None:
            raise HTTPException(404, "Skill 不存在")
        version = f"-{skill['version']}" if skill["version"] else ""
        filename = skill["name"] + version + ".zip"
        return Response(skill["archive"], media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/skills/{skill_id}/delete")
    def delete_skill(skill_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            dispatch_skills.delete(ctx, skill_id)
        return _redirect("/skills", msg="Skill 已删除")

    @app.get("/dispatch-targets", response_class=HTMLResponse)
    def targets(request: Request, edit: str = "", admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            servers = site_registry.list_servers(ctx.conn)
            entries = [dict(row, users=" ".join(json.loads(row["users"]))) for row in ctx.conn.execute(
                """SELECT t.*, s.name AS server_name, t.server_revision=s.revision AS current
                   FROM dispatch_targets t JOIN site_servers s ON s.id=t.server_id ORDER BY s.name, t.domain"""
            )]
        entry = next((row for row in entries if row["id"] == edit), None)
        if edit and not entry:
            raise HTTPException(404, "目标站授权不存在")
        return render(request, "dispatch_targets.html", admin=admin[0], csrf=admin[1],
                      servers=servers, entries=entries, entry=entry)

    @app.post("/dispatch-targets/save")
    def save_target(
        target_id: str = Form(""), server_id: str = Form(""), domain: str = Form(""),
        endpoint: str = Form(""), credential_env: str = Form(""), project: str = Form(""),
        users: str = Form(""), admin: tuple[str, str] = Depends(current_admin),
    ):
        try:
            with lock:
                dispatch_tasks.save_target(ctx, target_id or None, server_id, domain, endpoint,
                                           credential_env, project, users)
        except dispatch_tasks.TaskError as exc:
            return _redirect(f"/dispatch-targets?edit={target_id}" if target_id.isalnum() else "/dispatch-targets",
                             err=str(exc))
        return _redirect("/dispatch-targets", msg="目标站授权已保存")

    @app.post("/dispatch-targets/{target_id}/delete")
    def delete_target(target_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            dispatch_tasks.delete_target(ctx, target_id)
        return _redirect("/dispatch-targets", msg="目标站授权已删除")
