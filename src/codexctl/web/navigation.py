from __future__ import annotations

from urllib.parse import quote

from fastapi import Request


def site_view(request: Request) -> bool:
    view = request.query_params.get("view", "")
    if view in {"servers", "sites"}:
        return view == "sites"
    return any(key in request.query_params for key in ("server_id", "date", "state", "q", "page"))


def navigation(request: Request, user: str, is_admin: bool, managed: bool, gateway_enabled: bool) -> list[dict]:
    path = request.url.path
    module, tab = "", ""
    if path == "/":
        module, tab = "dashboard", "overview"
    elif path == "/usage/api":
        module, tab = "dashboard", "api_usage"
    elif path == "/usage":
        module, tab = "dashboard", "usage"
    elif path == "/audit":
        module, tab = "dashboard", "audit"
    elif path == "/servers" or path.startswith("/servers/"):
        module = "servers"
        tab = "sites" if path == "/servers" and site_view(request) else "server_list"
    elif path == "/accounts" or path.startswith("/accounts/"):
        module, tab = "codex", "accounts"
    elif path == "/tasks" or path.startswith("/tasks/"):
        module, tab = "codex", "tasks"
    elif path == "/skills" or path.startswith("/skills/"):
        module, tab = "codex", "skills"
    elif path == "/pricing":
        module, tab = "codex", "pricing"
    elif path == "/users" or path.startswith("/users/"):
        module, tab = "users", "users"

    groups = []
    if is_admin:
        dashboard = [("overview", "总览", "/"), ("usage", "会话用量", "/usage")]
        if gateway_enabled:
            dashboard.append(("api_usage", "API 用量", "/usage/api"))
        dashboard.append(("audit", "审计", "/audit"))
        groups.append(("dashboard", "数据面板", dashboard))

    groups.append(("servers", "服务器", [
        ("server_list", "服务器清单", "/servers"),
        ("sites", "网站", "/servers?view=sites"),
    ]))

    codex = []
    if is_admin:
        codex.append(("accounts", "账号管理", "/accounts"))
    codex.append(("tasks", "任务", "/tasks"))
    if is_admin:
        codex.extend([("skills", "Skill 管理", "/skills"), ("pricing", "价目表", "/pricing")])
    groups.append(("codex", "Codex", codex))

    if is_admin and (managed or gateway_enabled):
        groups.append(("users", "用户", [("users", "用户列表", "/users")]))
    elif not is_admin:
        groups.append(("users", "用户", [("users", "我的数据", f"/users/{quote(user, safe='')}")]))

    return [
        {
            "label": label,
            "href": tabs[0][2],
            "active": group_id == module,
            "tabs": [
                {"label": text, "href": href, "active": group_id == module and tab_id == tab}
                for tab_id, text, href in tabs
            ],
        }
        for group_id, label, tabs in groups
    ]
