from __future__ import annotations

import ast
import json
import sqlite3

import codexctl.db as db
import codexctl.events as events

MAX_CODE = 32768
MAX_PROMPT = 131072


class PromptError(ValueError):
    pass


DEFAULT_CODE = r'''prompt = (
    f"使用 ${dispatcher['name']} 和 ${migration['name']} 执行本次文件中的全部任务。\n"
    f"批次标识为 {task_id}。\n"
    f"上传文件为 {json.dumps(snapshot['filename'], ensure_ascii=False)}，共 {len(rows)} 个任务。\n"
    f"{original}"
    f"task.csv 包含本次上传文件的全部 {len(rows)} 个数据行，按文件中的备注和折扣执行。"
    "导入时折扣留空的行已按 3 折填入 task.csv。\n"
    "交接文件和 Skill 的绝对路径随本条消息提供。"
    "targets.json 中 csv_row 是 task.csv 的数据行号，line 是上传文件的原始行号。\n"
    f"文件中新域名与服务器对应关系\n{plan}\n"
    "部署目标以文件中的新域名及上述服务器对应关系为准。\n"
    "先读取 capabilities 和 api.describe，核对站点，用 site.probe 读取 WordPress 实况。"
    "在已授权范围内连续执行，不必逐步征求确认。接续装修任务、提交队列部署和归档。"
    "过程与备份保存在本站 zdy-AI，使用 context.update 更新接手摘要。"
    "断线后沿用原任务和请求标识，资源繁忙时等待并退避重试。"
    "完成后用中文逐站汇报部署、验收和归档结果及具体阻塞。"
)'''


def get_code(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT code FROM dispatch_prompt_template WHERE id=1").fetchone()
    return row["code"] if row else DEFAULT_CODE


def _expression(code: str) -> ast.expr:
    if not code.strip() or len(code) > MAX_CODE:
        raise PromptError("代码不能为空，且最多 32768 个字符")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise PromptError(f"Python 语法错误：{exc}") from exc
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign)
            or len(tree.body[0].targets) != 1 or not isinstance(tree.body[0].targets[0], ast.Name)
            or tree.body[0].targets[0].id != "prompt"):
        raise PromptError("请只填写一条 prompt = (...) 赋值语句")
    expression = tree.body[0].value
    allowed = (ast.Constant, ast.Name, ast.Load, ast.JoinedStr, ast.FormattedValue,
               ast.BinOp, ast.Add, ast.Subscript, ast.Call, ast.Attribute, ast.keyword)
    names = {"dispatcher", "migration", "task_id", "snapshot", "rows", "original", "plan", "len", "json"}
    for node in ast.walk(expression):
        if not isinstance(node, allowed):
            raise PromptError("仅支持字符串、f-string、加法拼接、变量索引、len() 和 json.dumps()")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, bool, type(None))):
            raise PromptError("不支持此常量类型")
        if isinstance(node, ast.Name) and node.id not in names:
            raise PromptError(f"未知变量：{node.id}")
        if isinstance(node, ast.Attribute) and not (
            isinstance(node.value, ast.Name) and node.value.id == "json" and node.attr == "dumps"
        ):
            raise PromptError("仅允许调用 json.dumps，不支持其他属性访问")
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            raise PromptError("f-string 不支持格式宽度，请直接插入变量")
        if isinstance(node, ast.Call):
            is_len = isinstance(node.func, ast.Name) and node.func.id == "len"
            is_json = isinstance(node.func, ast.Attribute) and node.func.attr == "dumps"
            if (not (is_len or is_json) or len(node.args) != 1
                    or any(k.arg != "ensure_ascii" for k in node.keywords)
                    or is_len and node.keywords):
                raise PromptError("只允许 len(值) 和 json.dumps(值, ensure_ascii=False)")
    return expression


def render(code: str, **context) -> str:
    expression = _expression(code)
    # The AST allowlist excludes imports, arbitrary calls, loops and attribute traversal.
    try:
        prompt = eval(compile(ast.Expression(expression), "<dispatch-prompt>", "eval"),
                      {"__builtins__": {}, "len": len, "json": json}, context)
    except Exception as exc:
        raise PromptError(f"提示词生成失败：{type(exc).__name__}: {exc}") from exc
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptError("prompt 必须生成非空字符串")
    if len(prompt) > MAX_PROMPT:
        raise PromptError("生成的提示词不能超过 131072 个字符")
    return prompt


def preview(code: str) -> str:
    return render(
        code, dispatcher={"name": "task-dispatcher"}, migration={"name": "html-to-wp-engine-sol"},
        task_id="example-batch-id", snapshot={"filename": "示例任务.csv", "upload_file": "upload.csv"},
        rows=[{}, {}], original="原始上传文件为 upload.csv。\n",
        plan='- shop.example.com → "示例服务器"，SSH 主机 192.0.2.10，端口 22\n'
             '- store.example.com → "示例服务器"，SSH 主机 192.0.2.10，端口 22',
    )


def save(ctx, actor: str, code: str) -> None:
    if actor != "admin":
        raise PromptError("仅管理员可以修改生成代码")
    preview(code)
    with db.transaction(ctx.conn):
        ctx.conn.execute(
            "INSERT INTO dispatch_prompt_template(id, code, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET code=excluded.code, updated_at=excluded.updated_at",
            (code, ctx.now()),
        )
        events.audit(ctx.conn, actor, "dispatch.prompt_template", "global", "更新提示词生成代码", now=ctx.now())
