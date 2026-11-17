from __future__ import annotations

import json
from urllib.parse import urlencode, urlsplit

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


def _task_status(task, child_counts):
    if task["status"] != "sent":
        return STATUS[task["status"]]
    if child_counts.get("failed") or child_counts.get("blocked"):
        return ("部分失败或受阻", "bad")
    if task["turn_status"] == "inProgress" or any(
        child_counts.get(state) for state in ("starting", "sending", "running")
    ):
        return ("执行中", "warn")
    if task["turn_status"] == "completed":
        if not child_counts:
            return ("Codex 轮次已结束", "info")
        target = json.loads(task["target_snapshot"])
        expected = len(target.get("rows", [])) or 1
        if child_counts.get("completed", 0) == expected and sum(child_counts.values()) == expected:
            return ("已完成", "ok")
        return ("轮次已结束，结果待核对", "warn")
    return (TURN_STATUS.get(task["turn_status"], "已发送给 Codex"), "warn")


def register(app) -> None:
    ctx = app.state.ctx
    render = app.state.render
    lock = app.state.lock

    def get_task(task_id: str, username: str):
        row = ctx.conn.execute("SELECT * FROM dispatch_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or username not in {"admin", row["owner"], row["recipient"]}:
            raise HTTPException(404, "任务不存在")
        return row

    def back_url(request):
        value = request.query_params.get("back", "/tasks")
        parsed = urlsplit(value)
        return value if parsed.path == "/tasks" and not parsed.scheme and not parsed.netloc else "/tasks"

    def task_url(task_id, request):
        return f"/tasks/{task_id}?" + urlencode({"back": back_url(request)})

    def task_page(request, who, selections=None, *, rows=(), status=200, err=""):
        user, csrf = who
        state = request.query_params.get("state", "")
        if state not in STATUS:
            state = ""
        query = request.query_params.get("q", "").strip()[:200]
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1
        where = "(?='admin' OR owner=? OR recipient=?)"
        values = [user, user, user]
        if state:
            where += " AND status=?"
            values.append(state)
        if query:
            where += " AND instr(lower(title || ' ' || owner || ' ' || recipient || ' ' || id), lower(?)) > 0"
            values.append(query)
        with lock:
            catalog = dispatch_skills.list_skills(ctx.conn)
            total = ctx.conn.execute(f"SELECT COUNT(*) FROM dispatch_tasks WHERE {where}", values).fetchone()[0]
            pages = max(1, (total + 49) // 50)
            page = min(page, pages)
            history = ctx.conn.execute(
                f"""SELECT id, owner, recipient, title, status, turn_status, created_at, target_snapshot FROM dispatch_tasks
                    WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT 50 OFFSET ?""",
                [*values, (page - 1) * 50],
            ).fetchall()
            history = [dict(task) for task in history]
            counts_by_task = {}
            if history:
                placeholders = ",".join("?" for _ in history)
                child_counts = ctx.conn.execute(
                    f"SELECT task_id, status, COUNT(*) FROM dispatch_children "
                    f"WHERE task_id IN ({placeholders}) GROUP BY task_id, status",
                    [task["id"] for task in history],
                )
                for task_id, child_status, count in child_counts:
                    counts_by_task.setdefault(task_id, {})[child_status] = count
            for task in history:
                task["status_text"], task["status_class"] = _task_status(task, counts_by_task.get(task["id"], {}))

        def page_url(number):
            return "/tasks?" + urlencode({"state": state, "q": query, "page": number})

        return render(
            request, "dispatch_tasks.html", status=status, admin=user, csrf=csrf,
            selections=selections or {}, catalog=catalog, rows=rows, columns=dispatch_tasks.IMPORT_COLUMNS,
            matched=sum(bool(row.server_name) for row in rows),
            batch_id=next((row.task_id for row in rows if row.task_id), ""),
            status_map=STATUS, history=history, time=_time, err=err,
            can_cancel=dispatch_tasks.can_cancel, state=state, query=query, page=page, pages=pages, total=total,
            previous=page_url(page - 1) if page > 1 else None,
            next_page=page_url(page + 1) if page < pages else None, list_url=page_url(page),
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

    def task_detail(request, who, task, *, status=200, error="", execution=None):
        user, csrf = who
        target = json.loads(task["target_snapshot"])
        with lock:
            children = ctx.conn.execute(
                "SELECT * FROM dispatch_children WHERE task_id=? ORDER BY row_number", (task["id"],),
            ).fetchall()
        child_counts = {}
        for child in children:
            child_counts[child["status"]] = child_counts.get(child["status"], 0) + 1
        status_text, status_class = _task_status(task, child_counts)
        return render(
            request, "dispatch_detail.html", status=status, admin=user, csrf=csrf, task=task,
            target=target, batch_rows=target.get("rows", []), skills=json.loads(task["skills"]),
            children=children, child_counts=child_counts, status_text=status_text, status_class=status_class, back_url=back_url(request), task_url=task_url(task["id"], request),
            refreshing=task["status"] in {"queued", "sending", "cancelling"}
                       or task["turn_status"] == "inProgress"
                       or any(child["status"] in {"starting", "sending", "running"} for child in children),
            status_map=STATUS, turn_status=TURN_STATUS, kinds=dispatch_skills.KINDS,
            can_confirm=task["status"] == "preview" and user == task["owner"] and user != "admin"
                        and dispatch_tasks.batch_complete(target),
            batch_complete=dispatch_tasks.batch_complete(target),
            can_cancel=user != "admin" and user in {task["owner"], task["recipient"]} and dispatch_tasks.can_cancel(task),
            time=_time, err=error,
            working_directory=task["working_directory"] or f"~{task['recipient']}/wp_front",
            execution=execution if execution is not None else {
                "model": task["model"] or dispatch_tasks.DEFAULT_MODEL,
                "reasoning_effort": task["reasoning_effort"] or dispatch_tasks.DEFAULT_REASONING_EFFORT,
                "sandbox": dispatch_tasks.DEFAULT_SANDBOX,
                "working_directory": task["working_directory"] or "~/wp_front",
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def detail(request: Request, task_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            task = get_task(task_id, who[0])
        return task_detail(request, who, task)

    @app.post("/tasks/{task_id}/confirm")
    async def confirm(
        request: Request, task_id: str, confirmed: str = Form(""),
        model: str | None = Form(None), reasoning_effort: str | None = Form(None),
        sandbox: str | None = Form(None), working_directory: str | None = Form(None),
        who: tuple[str, str] = Depends(current_user),
    ):
        prompt = (await request.form()).get("prompt")
        if prompt is not None and not isinstance(prompt, str):
            raise HTTPException(422, "提示词须为文本")
        error = ""
        with lock:
            task = get_task(task_id, who[0])
            if task["owner"] != who[0] or who[0] == "admin":
                raise HTTPException(403, "仅任务所属成员可以确认发送")
            if confirmed != "1":
                error = "请先核对提示词并勾选确认"
            else:
                try:
                    dispatch_tasks.confirm(ctx, task, who[0], prompt, model=model, reasoning_effort=reasoning_effort, sandbox=sandbox, working_directory=working_directory)
                except dispatch_tasks.TaskError as exc:
                    error = str(exc)
        if error:
            if prompt is not None and task["status"] == "preview":
                task = dict(task, prompt=prompt)
            return task_detail(request, who, task, status=409, error=error,
                               execution={"model": model if model is not None else task["model"] or dispatch_tasks.DEFAULT_MODEL,
                                          "reasoning_effort": reasoning_effort if reasoning_effort is not None else task["reasoning_effort"] or dispatch_tasks.DEFAULT_REASONING_EFFORT,
                                          "sandbox": sandbox if sandbox is not None else task["sandbox"],
                                          "working_directory": working_directory if working_directory is not None else task["working_directory"] or "~/wp_front"})
        return RedirectResponse(task_url(task_id, request), status_code=303)

    @app.post("/tasks/{task_id}/prompt")
    def save_prompt(
        request: Request, task_id: str, prompt: str = Form(""),
        model: str | None = Form(None), reasoning_effort: str | None = Form(None),
        sandbox: str | None = Form(None), working_directory: str | None = Form(None),
        who: tuple[str, str] = Depends(current_user),
    ):
        error = ""
        with lock:
            task = get_task(task_id, who[0])
            if task["owner"] != who[0] or who[0] == "admin":
                raise HTTPException(403, "仅任务所属成员可以修改提示词")
            try:
                dispatch_tasks.save_prompt(ctx, task, who[0], prompt, model=model, reasoning_effort=reasoning_effort, sandbox=sandbox, working_directory=working_directory)
            except dispatch_tasks.TaskError as exc:
                error = str(exc)
                if task["status"] == "preview":
                    task = dict(task, prompt=prompt)
        if error:
            return task_detail(request, who, task, status=409, error=error,
                               execution={"model": model if model is not None else task["model"] or dispatch_tasks.DEFAULT_MODEL,
                                          "reasoning_effort": reasoning_effort if reasoning_effort is not None else task["reasoning_effort"] or dispatch_tasks.DEFAULT_REASONING_EFFORT,
                                          "sandbox": sandbox if sandbox is not None else task["sandbox"],
                                          "working_directory": working_directory if working_directory is not None else task["working_directory"] or "~/wp_front"})
        return RedirectResponse(task_url(task_id, request), status_code=303)

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(request: Request, task_id: str, who: tuple[str, str] = Depends(current_user)):
        with lock:
            task = get_task(task_id, who[0])
            if who[0] == "admin" or who[0] not in {task["owner"], task["recipient"]}:
                raise HTTPException(403, "仅任务所属成员可以取消任务")
            dispatch_tasks.cancel(ctx, task_id, who[0])
        return RedirectResponse(task_url(task_id, request), status_code=303)

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
