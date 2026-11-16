from __future__ import annotations

import json

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import dispatch_skills, dispatch_tasks
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


def register(app) -> None:
    ctx = app.state.ctx
    render = app.state.render
    lock = app.state.lock

    def get_task(task_id: str, username: str):
        row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or username not in {"admin", row["owner"], row["recipient"]}:
            raise HTTPException(404, "任务不存在")
        return row

    def task_page(request, who, selections=None, *, rows=(), status=200, err=""):
        user, csrf = who
        with lock:
            catalog = dispatch_skills.list_skills(ctx.conn)
            history = ctx.conn.execute(
                """SELECT id, owner, recipient, title, status, turn_status, created_at FROM dispatch_tasks
                   WHERE ?='admin' OR owner=? OR recipient=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 50""",
                (user, user, user),
            ).fetchall()
        return render(
            request, "dispatch_tasks.html", status=status, admin=user, csrf=csrf,
            selections=selections or {}, catalog=catalog, rows=rows, columns=dispatch_tasks.IMPORT_COLUMNS,
            matched=sum(bool(row.server_name) for row in rows),
            batch_id=next((row.task_id for row in rows if row.task_id), ""),
            status_map=STATUS, history=history, time=_time, err=err,
            can_cancel=dispatch_tasks.can_cancel,
        )

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request, who: tuple[str, str] = Depends(current_user)):
        return task_page(request, who)

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
