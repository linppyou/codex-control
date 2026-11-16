from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
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
    "cancelling": ("正在取消", "warn"), "cancelled": ("已取消", "info"),
}
TURN_STATUS = {"inProgress": "Codex 正在处理", "completed": "Codex 轮次已结束",
               "failed": "Codex 轮次失败", "interrupted": "Codex 轮次已中断", "unknown": "Codex 轮次待核对"}


def work_time(timestamp: int | None) -> str:
    return datetime.fromtimestamp(timestamp, sites.BEIJING).strftime("%Y-%m-%d %H:%M") if timestamp is not None else "未知"


def register(app) -> None:
    ctx = app.state.ctx
    render = app.state.render
    lock = app.state.lock

    def get_task(task_id: str, username: str):
        row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or username not in {"admin", row["owner"], row["recipient"]}:
            raise HTTPException(404, "任务不存在")
        return row

    def task_page(request, who, selections=None, *, rows=(), server_id="", selected_sites=(), selected_user="", status=200, err=""):
        user, csrf = who
        pending = []
        with lock:
            if user == "admin":
                inventories = sites.snapshots(ctx.conn)
                for server in site_registry.list_servers(ctx.conn):
                    choices = dispatch_tasks.pending_sites(ctx.conn, server, inventories.get(server.id))
                    if choices:
                        by_date = {}
                        for site in choices:
                            day = sites.deployment_date(site.get("created_at"))
                            by_date.setdefault(day, []).append(site)
                        days = sorted(by_date, key=lambda day: day or "", reverse=True)
                        pending.append((server, [(day, by_date[day]) for day in days]))
            assigned = dispatch_tasks.list_targets(ctx, user) if user != "admin" else []
            users = []
            if user == "admin":
                for item in repo.all_active(ctx.conn):
                    try:
                        dispatch_tasks.recipient(ctx, item.username)
                    except dispatch_tasks.TaskError:
                        continue
                    users.append(item.username)
            catalog = dispatch_skills.list_skills(ctx.conn)
            history = ctx.conn.execute(
                """SELECT id, owner, recipient, title, status, turn_status, created_at FROM dispatch_tasks
                   WHERE ?='admin' OR owner=? OR recipient=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 50""",
                (user, user, user),
            ).fetchall()
        if pending and not any(server.id == server_id for server, _ in pending):
            server_id = pending[0][0].id
        return render(
            request, "dispatch_tasks.html", status=status, admin=user, csrf=csrf,
            selections=selections or {}, catalog=catalog, rows=rows, columns=dispatch_tasks.IMPORT_COLUMNS,
            matched=sum(bool(row.server_name) for row in rows),
            batch_id=next((row.task_id for row in rows if row.task_id), ""),
            status_map=STATUS, history=history, time=_time, err=err,
            can_cancel=dispatch_tasks.can_cancel, pending=pending, assigned=assigned, users=users,
            selected_server=server_id, selected_sites=selected_sites, selected_user=selected_user,
            work_time=work_time,
        )

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request, server_id: str = "", who: tuple[str, str] = Depends(current_user)):
        return task_page(request, who, server_id=server_id)

    @app.post("/tasks/import", response_class=HTMLResponse)
    def import_tasks(
        request: Request, task_csv: UploadFile | None = File(None),
        dispatcher: str = Form(""), migration: str = Form(""),
        who: tuple[str, str] = Depends(current_user),
    ):
        if who[0] == "admin":
            raise HTTPException(403, "请使用成员账号导入任务")
        selections = dict(dispatcher=dispatcher, migration=migration)
        rows = ()
        try:
            if task_csv is None or not task_csv.filename:
                raise dispatch_tasks.TaskError("请选择要导入的 CSV 或 XLSX 文件")
            upload = task_csv.file.read(dispatch_tasks.MAX_CSV_UPLOAD + 1)
            rows = dispatch_tasks.parse_import(upload, task_csv.filename)
            with lock:
                dispatch_tasks.create_previews(ctx, who[0], rows, dispatcher, migration, task_csv.filename, upload)
        except dispatch_tasks.TaskError as exc:
            return task_page(request, who, selections, rows=rows, status=422, err=str(exc))
        return task_page(request, who, selections, rows=rows)

    @app.get("/tasks/assignments", response_class=HTMLResponse)
    def assignments(
        request: Request, day: str = "", status: str = "",
        admin: tuple[str, str] = Depends(current_admin),
    ):
        start = stop = None
        if day:
            try:
                selected = date.fromisoformat(day)
                midnight = datetime.combine(selected, time.min, sites.BEIJING)
                start = int(midnight.timestamp())
                stop = int((midnight + timedelta(days=1)).timestamp())
                day = selected.isoformat()
            except (ValueError, OverflowError) as exc:
                raise HTTPException(422, "分配日期格式无效") from exc
        if status not in {"", "pending", "completed"}:
            raise HTTPException(422, "完成状态无效")
        with lock:
            records = dispatch_tasks.site_work(ctx.conn, start, stop, status)
        return render(
            request, "dispatch_assignments.html", admin=admin[0], csrf=admin[1],
            records=records, day=day, filter_status=status, work_time=work_time,
        )

    @app.get("/tasks/sites/{target_id}/deployment", response_class=HTMLResponse)
    def deployment(request: Request, target_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            target = dispatch_tasks.deployment_target(ctx, who[0], target_id)
        if target is None:
            raise HTTPException(404, "站点分配不存在")
        return render(request, "dispatch_deployment.html", admin=who[0], csrf=who[1], target=target)

    @app.post("/tasks/sites/{target_id}/complete")
    def complete_site(target_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            if not dispatch_tasks.complete_site_work(ctx, who[0], target_id):
                raise HTTPException(404, "站点分配不存在")
        return _redirect("/tasks", msg="已标记完成")

    @app.post("/tasks/assign")
    def assign(
        request: Request, server_id: str = Form(""), site_ids: list[str] = Form(default=[]),
        site_id: str = Form(""), recipient: str = Form(""),
        admin: tuple[str, str] = Depends(current_admin),
    ):
        selected = []
        try:
            for choice in site_ids:
                choice_server, _, inventory_id = choice.partition(":")
                if choice_server != server_id or not inventory_id:
                    raise dispatch_tasks.TaskError("请选择当前服务器上的待分配站点")
                selected.append(inventory_id)
            if not site_ids and site_id:
                selected.append(site_id)
            with lock:
                domains = dispatch_tasks.assign_sites(ctx, server_id, selected, recipient)
        except dispatch_tasks.TaskError as exc:
            return task_page(
                request, admin, server_id=server_id, selected_sites=set(selected), selected_user=recipient,
                status=422, err=str(exc),
            )
        message = f"{domains[0]} 已分配给 {recipient}" if len(domains) == 1 else f"已将 {len(domains)} 个站点分配给 {recipient}"
        return _redirect("/tasks?" + urlencode({"server_id": server_id}), msg=message)

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
            return task_page(request, who, status=422, err=str(exc))
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)

    def task_detail(request, who, task, *, status=200, error=""):
        user, csrf = who
        target = json.loads(task["target_snapshot"])
        return render(
            request, "dispatch_detail.html", status=status, admin=user, csrf=csrf, task=task,
            target=target, batch_rows=target.get("rows", []), skills=json.loads(task["skills"]),
            status_map=STATUS, turn_status=TURN_STATUS, kinds=dispatch_skills.KINDS,
            can_confirm=task["status"] == "preview" and user == task["owner"] and user != "admin"
                        and dispatch_tasks.batch_complete(target),
            batch_complete=dispatch_tasks.batch_complete(target),
            can_cancel=user != "admin" and user in {task["owner"], task["recipient"]} and dispatch_tasks.can_cancel(task),
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
            if task["owner"] != who[0] or who[0] == "admin":
                raise HTTPException(403, "仅任务所属成员可以确认发送")
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

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            task = get_task(task_id, who[0])
            if who[0] == "admin" or who[0] not in {task["owner"], task["recipient"]}:
                raise HTTPException(403, "仅任务所属成员可以取消任务")
            dispatch_tasks.cancel(ctx, task_id, who[0])
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
                """SELECT t.id, t.server_id, t.domain, t.users, t.project, s.name AS server_name,
                          t.server_revision=s.revision AS current
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
        project: str = Form(""), users: str = Form(""), admin: tuple[str, str] = Depends(current_admin),
    ):
        try:
            with lock:
                dispatch_tasks.save_target(ctx, target_id or None, server_id, domain, project, users)
        except dispatch_tasks.TaskError as exc:
            return _redirect(f"/dispatch-targets?edit={target_id}" if target_id.isalnum() else "/dispatch-targets",
                             err=str(exc))
        return _redirect("/dispatch-targets", msg="目标站授权已保存")

    @app.post("/dispatch-targets/{target_id}/delete")
    def delete_target(target_id: str, admin: tuple[str, str] = Depends(current_admin)):
        with lock:
            dispatch_tasks.delete_target(ctx, target_id)
        return _redirect("/dispatch-targets", msg="目标站授权已删除")
