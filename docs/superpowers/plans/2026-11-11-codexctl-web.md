# codexctl Web 面板实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 codexctl 增加只供管理员使用的 Web 面板，并通过宝塔 nginx 以 `https://codex-control.ppspace.xyz` 对外提供服务。

**Architecture:**
- 新进程 `codexctl web`（FastAPI + uvicorn），只监听 `127.0.0.1:8700`，由 systemd 单元 `codexctl-web.service` 以 root 运行。
- 它复用核心模块（accounts / assignments / refresher / repo / watcher / locks），与守护进程共用同一个 SQLite 数据库（WAL，busy_timeout 30s）和账号文件锁。
- 所有请求处理用一把进程内全局锁串行执行，避免多线程共用 SQLite 连接。
- 页面由 Jinja2 服务端渲染，使用普通表单，提交后 POST→重定向→GET。只有两处少量内联 JS：device-auth 状态轮询，以及用户列表的全选。不引入 htmx，也不引入任何 CDN 资源。

**Tech Stack:** FastAPI、uvicorn、Jinja2、python-multipart、argon2-cffi；测试使用 fastapi.testclient。

**设计依据：** 核心设计见 `docs/superpowers/specs/2026-11-11-codexctl-design.md` 第 7、8 节；本次已确认的决定：
- 面板独立成一个进程。
- 管理员账号用命令行 `codexctl admin create` 创建，密码由管理员交互输入。
- 公网访问只靠登录保护：argon2、服务端会话、CSRF、登录限速。
- 域名、证书和 nginx 配置全部由部署方完成。

**与"完整代码"约定的差异：** 本计划不贴出完整实现代码，只给出精确的接口、行为和必须覆盖的测试。原因是核心层已经稳定，Web 层主要是胶水代码。实现者必须先写测试（TDD）。

---

## 文件结构

```
src/codexctl/
  config.py              追加 web_host / web_port / web_cookie_secure / hidden_users
  db.py                  追加 admins、web_sessions 表
  webauth.py             管理员、密码、会话、CSRF、登录限速（不依赖 FastAPI）
  devicelogin.py         网页内 device-auth 登录的后台进程管理（不依赖 FastAPI）
  web/
    __init__.py
    app.py               create_app(ctx_factory)；鉴权依赖、全局锁、错误处理
    views.py             各页面路由
    templates/           base.html, login.html, dashboard.html, accounts.html,
                         account_login.html, users.html, usage.html, audit.html
    static/style.css
  cli.py                 追加 `admin create|passwd|list`、`web`
deploy/
  codexctl-web.service
  nginx/codex-control.ppspace.xyz.conf
docs/deploy.md           追加「Web 面板」一节
tests/
  test_webauth.py  test_devicelogin.py  test_web.py
```

---

### Task W1：配置、表结构、webauth、admin 命令

**Files:** Modify `pyproject.toml`, `src/codexctl/config.py`, `src/codexctl/db.py`, `src/codexctl/cli.py`; Create `src/codexctl/webauth.py`; Test `tests/test_webauth.py`, `tests/test_cli.py`

- [ ] **依赖**：`pyproject.toml` 的 dependencies 追加 `fastapi>=0.110`、`uvicorn>=0.29`、`jinja2>=3.1`、`python-multipart>=0.0.9`、`argon2-cffi>=23.1`；dev 追加 `httpx`（已在主依赖中）。用 `.venv/bin/pip install -e '.[dev]'` 重新安装。hatch 打包时要包含 `src/codexctl/web/templates` 和 `static`：因为 packages 已经是 `src/codexctl`，非 py 文件默认就会被包含，安装后需要确认一下。
- [ ] **Settings 追加字段**：
  - `web_host: str = "127.0.0.1"`
  - `web_port: int = 8700`
  - `web_cookie_secure: bool = True`
  - `hidden_users: tuple[str, ...] = ()`
  - TOML 里的列表要转成 tuple，在 `load_settings` 里处理。
- [ ] **db.SCHEMA 追加两张表**：
  - `admins(username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL)`
  - `web_sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)`
- [ ] **webauth.py 公开接口**（全部接收 `conn` 和 `now: int`，不读系统时间）：
  - **常量**：`SESSION_TTL = 12 * 3600`。
  - **异常**：`class AuthError(Exception)`。
  - **`create_admin(conn, username, password, *, now)`**
    - 用户名必须匹配 `^[a-z_][a-z0-9_.-]{0,31}$`（fullmatch），密码至少 12 个字符，否则抛 `AuthError`。
    - 用户名已存在时抛 `AuthError`。
    - 使用 argon2-cffi 的 `PasswordHasher()` 生成哈希。
  - **`set_password(conn, username, password, *, now)`**
    - 用户不存在时抛 `AuthError`。
    - 修改密码后删除该用户的全部会话。
  - **`list_admins(conn) -> list[str]`**
  - **`verify_login(conn, username, password) -> bool`**
    - 用户不存在时，也要对一个固定的虚拟哈希执行一次 verify，抹平时间差，然后返回 False。
    - 校验成功且 `check_needs_rehash` 为真时，更新哈希。
  - **`create_session(conn, username, *, now) -> tuple[str, str]`**
    - 返回 `(token, csrf)`。两者都由 `secrets.token_urlsafe(32)` 生成。
    - 数据库只存 token 的 sha256 hex。
    - 同一个事务里清理所有 `expires_at < now` 的会话。
  - **`get_session(conn, token, *, now) -> tuple[str, str] | None`**：返回 `(username, csrf)`；会话过期或不存在时返回 None。
  - **`delete_session(conn, token)`**
  - **`class LoginRateLimiter(max_failures=5, window=900)`**
    - 方法：`allowed(ip, now) -> bool`、`record_failure(ip, now)`、`reset(ip)`，状态保存在内存。
- [ ] **CLI**：
  - `codexctl admin create <name>` 和 `codexctl admin passwd <name>`：用 `getpass.getpass` 读两次密码，两次不一致就报错。
  - `codexctl admin list`。
  - 所有错误都走现有的"错误：…"输出和返回 1 的处理。
  - 这些命令只需要数据库，可以直接用 `db.connect(settings.db_path)`，不必加载主密钥。
- [ ] **测试**（`tests/test_webauth.py`、`tests/test_cli.py`）：
  - 创建管理员后能校验成功；用错误密码校验失败；不存在的用户返回 False。
  - 用户名不合法、密码过短、用户重复，都抛 `AuthError`。
  - 会话往返：create 后 get 能取回；过期后取不到；delete 后取不到；数据库里存的不是明文 token。
  - `set_password` 会清除该用户的会话。
  - 限速：失败 5 次后不允许登录，过了窗口期恢复，`reset` 后恢复。
  - CLI `admin create` 用 monkeypatch 替换 getpass；两次输入不一致时返回 1。
  - `load_settings` 能把 `hidden_users` 的列表转成 tuple。
- [ ] 提交：`feat: 面板管理员、会话与限速`

### Task W2：devicelogin.py（网页内 device-auth）

**Files:** Create `src/codexctl/devicelogin.py`; Test `tests/test_devicelogin.py`

- [ ] **`class DeviceLoginManager(settings: Settings, clock=time.time)`**，内部用 `threading.Lock` 保护。对外接口：
  - **`start(label: str, mode: str) -> str`**
    - `mode` 只能是 `"add"` 或 `"relogin"`；标签用 `publisher.LABEL_RE.fullmatch` 校验。不合格抛 `ValueError`。
    - 同一个 label 已有进行中的登录时抛 `ValueError("该账号已有进行中的登录")`。
    - 用 `tempfile.mkdtemp(prefix=f"{label}-", dir=staging_dir)` 创建 CODEX_HOME。`staging_dir` 不存在则创建，并 chmod 0700。
    - 用 `subprocess.Popen([codex_bin, "login", "--device-auth"], env={**os.environ, "CODEX_HOME": home}, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True)` 启动子进程。
    - 启动一个守护线程逐行读取输出：先去掉 ANSI 转义 `\x1b\[[0-9;]*[A-Za-z]`，再保存最近 50 行。
    - 解析规则：网址取第一个匹配 `https://\S+` 的串；验证码取第一个匹配 `\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b` 的串。
    - 返回一个随机 `login_id`。
  - **`status(login_id) -> dict | None`**
    - 返回 `{"label", "mode", "state", "url", "code", "error", "started_at"}`。
    - `state` 取值：`pending`（还没解析到验证码）、`waiting`（已拿到网址和验证码）、`succeeded`、`failed`、`cancelled`、`expired`。
  - **`take_result(login_id) -> bytes`**
    - 只在 `succeeded` 时可调用，返回 `auth.json` 的字节内容，随后清理临时目录。
    - 只能取一次；再次调用抛 `ValueError`。
  - **`cancel(login_id)`**：terminate 子进程，2 秒后仍未退出就 kill；清理临时目录；状态改为 `cancelled`。
  - **`sweep()`**：超过 15 分钟仍未结束的登录按 cancel 处理，状态改为 `expired`；已结束超过 10 分钟的记录直接删除。
- [ ] **状态判定**：
  - 子进程退出码为 0 且 `auth.json` 存在 → `succeeded`。
  - 否则 → `failed`，`error` 为"codex login 退出码 N"。
  - 判定在读输出的线程里进行：读到 EOF 后 `wait()` 子进程，再判定。
- [ ] **测试**：codex 用临时目录里的假脚本代替。
  - 成功路径：脚本先打印网址和验证码，然后循环等待测试创建的标记文件；标记出现后写出 `$CODEX_HOME/auth.json` 并以 0 退出。断言：
    - 状态先是 `waiting`，网址和验证码解析正确；
    - 结束后是 `succeeded`；
    - `take_result` 返回内容，且临时目录已被删除。
  - 失败路径：脚本以退出码 3 结束，状态为 `failed`。
  - 取消：脚本 `sleep 30`，`cancel` 后状态为 `cancelled`，子进程已结束，临时目录已删除。
  - 同一个 label 重复 `start` 抛 `ValueError`。
  - `sweep` 能把超时的登录标为 `expired`：用假时钟。
  - 输出带 ANSI 颜色码时也能正确解析验证码。
  - 等待状态变化用轮询加超时（最多 5 秒），不要固定 sleep。
- [ ] 提交：`feat: 网页内 device-auth 登录管理`

### Task W3：FastAPI 应用骨架、登录与鉴权

**Files:** Create `src/codexctl/web/__init__.py`, `web/app.py`, `web/templates/base.html`, `login.html`, `web/static/style.css`; Test `tests/test_web.py`

- [ ] **`create_app(ctx: Context, *, devlogins: DeviceLoginManager | None = None, limiter: LoginRateLimiter | None = None) -> FastAPI`**
  - `docs_url`、`redoc_url`、`openapi_url` 全部设为 None。
  - 静态文件挂载在 `/static`。
  - 模板目录用 `Path(__file__).parent / "templates"`。
- [ ] **全局锁**：模块级 `threading.Lock`。所有路由都定义成同步函数（`def`），在函数体内用 `with app.state.lock:` 包住全部逻辑。
- [ ] **客户端 IP**：只有当 `request.client.host` 是 `127.0.0.1` 或 `::1` 时，才信任 `X-Real-IP` 请求头；否则直接用 `request.client.host`。
- [ ] **会话**：
  - cookie 名为 `codexctl_session`，设置 `httponly=True`、`samesite="strict"`、`secure=settings.web_cookie_secure`、`path="/"`、`max_age=SESSION_TTL`。
  - 依赖 `current_admin(request) -> tuple[username, csrf]`：未登录时，GET 请求重定向到 `/login`（303），其他请求返回 403。
- [ ] **CSRF**：所有 POST 路由（`/login` 除外）都必须带表单字段 `csrf`，且与会话中保存的一致，否则返回 403。`/login` 的表单用一个随机的预登录 CSRF：GET `/login` 时种一个 cookie `codexctl_prelogin`（随机值），表单回传的值必须与它相同（双提交）。
- [ ] **路由**：
  - `GET /login`：显示登录页。
  - `POST /login`：
    - 先查限速；超限时返回 429，页面提示"尝试过多，请 15 分钟后再试"。
    - 校验成功：`limiter.reset(ip)`，创建会话，写一条审计 `web.login`（actor=用户名，target=IP），然后 303 重定向到 `/`。
    - 校验失败：`record_failure`，写一条审计 `web.login_failed`（actor=`"web"`，target=IP，detail=用户名，需过滤掉不可打印字符），并统一提示"用户名或密码错误"。
  - `POST /logout`：删除会话，303 重定向到 `/login`。
- [ ] **安全响应头**（中间件，作用于所有响应）：
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'`
  - `Cache-Control: no-store`
- [ ] **base.html**：
  - 顶部导航：总览 / 账号 / 用户 / 用量 / 审计，右侧显示当前用户和"退出"按钮（POST 表单，带 csrf）。
  - flash 消息：通过查询参数 `?msg=` / `?err=` 显示，只显示纯文本，依赖 Jinja2 默认的 HTML 转义。
  - 全部模板开启 autoescape。
- [ ] **style.css**：简洁的中文后台样式，包括表格、卡片、进度条（`<div class="bar"><span style="width:NN%">`）和按钮。注意：内联 `style=` 属性受 CSP `style-src 'self'` 限制会失效。进度条宽度改用 `<progress max="100" value="NN">`，或者用 class 档位（例如 `w-0` 到 `w-100`，每 5% 一档）；推荐 `<progress>`。
- [ ] **测试**（`TestClient(create_app(ctx))`，ctx 用 conftest 的 fixture，并设置 `web_cookie_secure=False`；可以用 `dataclasses.replace(ctx.settings, ...)` 构造新的 Settings 再赋给 ctx）：
  - 未登录访问 `/` → 303 重定向到 `/login`。
  - 登录成功 → 拿到 cookie，`/` 返回 200。
  - 密码错误 → 页面提示，且生成一条审计记录。
  - 连续 5 次失败后，第 6 次返回 429。
  - 不带 csrf 的 POST（例如 `/logout`）→ 403。
  - 响应头里有 CSP 和 X-Frame-Options。
  - 来自非本机的 `X-Real-IP` 不被信任：TestClient 的 client.host 是 `testclient`，这时应以 `testclient` 作为 IP 计入限速。
  - `/docs` 返回 404。
- [ ] 提交：`feat: 面板骨架、登录与鉴权`

### Task W4：页面

**Files:** Create `src/codexctl/web/views.py` and templates `dashboard.html`, `accounts.html`, `account_login.html`, `users.html`, `usage.html`, `audit.html`; Modify `web/app.py`（注册 views）; Test `tests/test_web.py`

所有页面都要求已登录，所有写操作都是 POST 并校验 csrf。写操作的 actor 记为 `web:<管理员名>`。写操作成功后 303 重定向回原页面，并带 `?msg=`；出错时带 `?err=`，错误文本来自 `AccountError`、`AssignError`、`UserError`、`AuthFileError`、`PublishError`、`LockBusy`、`CommandError` 的 str。其他异常交给全局处理器：返回 500 页面"内部错误"，不显示堆栈，并用 `log.exception` 记录。

- [ ] **`GET /` 总览**
  - 每个账号一张卡片，内容包括：标签、邮箱、套餐、状态（徽标：active 绿、needs_relogin 红、disabled 灰）、分配人数、上次刷新时间、token 到期时间。
  - 额度来自 `repo.latest_quota(conn, acc.id, api_fresh_since=now-600)`。每个窗口显示名称（5 小时 / 7 天，按 window_minutes 判断，复用 cli.window_name）、`<progress>`、百分比、重置倒计时（"2 小时 13 分后重置"）和来源。
  - 下方列出未解决告警（级别、类型、对象、消息、时间）。
- [ ] **`GET /accounts`**
  - 表格列：标签、邮箱、套餐、状态、last_error、用户数、上次刷新、token 到期，以及操作按钮：刷新、重新登录、停用 / 启用、重新发布。
  - 底部是"添加账号"表单，只需填写标签。
- [ ] **账号操作 POST 路由**：
  - `POST /accounts/{label}/refresh`：调用 `refresher.refresh_account(ctx, acc, actor, blocking=False)`。成功提示"刷新成功"；返回失败时把 `last_error` 放进 `err`。
  - `POST /accounts/{label}/disable`、`/enable`、`/republish`：分别对应 `accounts.set_disabled` 和 `accounts.republish_account`。
  - `POST /accounts/login`：
    - 表单字段：label、mode（add 或 relogin）。
    - mode=add 时先调用 `accounts.check_new_label`；mode=relogin 时先调用 `accounts.require_account`。
    - 然后调用 `devlogins.start`，303 重定向到 `/accounts/login/{login_id}`。
  - `GET /accounts/login/{login_id}`：渲染 `account_login.html`。
    - 显示网址（可点击，`target=_blank rel=noopener`）、大号验证码、"15 分钟内有效"、状态，以及取消按钮。
    - 页面带一小段内联 JS，每 3 秒请求一次 JSON 状态，只更新文本（textContent），状态到达终态时刷新整页。
  - `GET /accounts/login/{login_id}/status`（JSON）：返回 `devlogins.status` 中的 state、url、code、error。
  - **完成导入**：当 state 为 succeeded 且 GET 页面时，在锁内调用 `take_result`，再按 mode 调用 `accounts.import_account` 或 `relogin_account`（actor 为 web 管理员），然后 303 重定向到 `/accounts?msg=…`。
    - `take_result` 只能取一次，必须避免重复导入。
    - 导入失败（例如重复账号、登录的不是同一个账号）时带 `err` 重定向。
  - `POST /accounts/login/{login_id}/cancel`。
  - 每次请求处理开始时调用一次 `devlogins.sweep()`。
- [ ] **`GET /users`**
  - 用户列表来自 `ctx.list_users(min_uid)`。默认隐藏 `settings.hidden_users`，带 `?all=1` 时全部显示。
  - 每行显示：复选框、用户名、当前账号、链接状态（`publisher.read_link(...).kind`，需过滤不可打印字符），以及近 24 小时和 7 天的 token 合计（新增 `repo.usage_by_user(conn, since) -> dict[username, total]`）。
  - 单行操作：修复。
  - 批量表单：账号下拉框只列出非 disabled 的账号，按钮有"分配 / 切换到所选账号"和"回收"。
- [ ] **用户操作 POST 路由**：
  - `POST /users/assign`：表单字段 `users`（多个）和 `account`。逐个调用 `assignments.assign`，汇总成功和失败，失败的写进 `err`。
  - `POST /users/unassign`：表单字段 `users`。
  - `POST /users/{name}/fix`。
- [ ] **`GET /usage?days=1|7|30`**
  - 默认 7 天，days 只允许这三个值。
  - 内容为 `repo.usage_summary` 的表格。
  - `GET /usage.csv?days=` 导出 CSV，`Content-Disposition: attachment`。字段开头若为 `=+-@`，前面加 `'`，防止 CSV 公式注入。
- [ ] **`GET /audit`**：最近 200 条 `audit_log`（新增 `repo.recent_audit(conn, limit)`），以及最近 100 条告警（含已解决的，新增 `repo.recent_alerts(conn, limit)`）。
- [ ] **测试**（已登录的 TestClient 下，跳转一律校验 303 和 Location）：
  - 各页面返回 200，并包含关键文本：总览含账号标签；用户页隐藏 hidden_users，带 `?all=1` 时显示。
  - 批量分配两个用户：断言链接已创建、setfacl 已调用；然后回收。
  - 停用账号后再分配给该账号 → `err` 里有"已停用"。
  - 刷新：路由 mock 成功和失败两种情况，并测试锁被占用时提示"正在刷新"。
  - device-auth 完整流程：注入一个使用假 codex 脚本的 `DeviceLoginManager`；POST `/accounts/login`（add）→ 状态页显示验证码 → 创建标记文件让脚本完成 → GET 状态页 → 303 到 `/accounts` 且账号已导入；再次 GET 不会重复导入。
  - CSV 防注入：某个用户名以 `=` 开头时，前面加了 `'`。可以直接往 usage_events 插入数据来测试。
  - 写操作缺少 csrf 一律返回 403。
  - XSS：在 `alerts` 里插入一条含 `<script>` 的告警，总览页输出的是转义后的文本。
- [ ] 提交：`feat: 面板页面（总览、账号、用户、用量、审计）`

### Task W5：web 命令、systemd、nginx 配置与文档

**Files:** Modify `src/codexctl/cli.py`, `docs/deploy.md`; Create `deploy/codexctl-web.service`, `deploy/nginx/codex-control.ppspace.xyz.conf`; Test `tests/test_cli.py`

- [ ] **`codexctl web` 命令**：用 `open_context(settings)` 构造 ctx，再 `uvicorn.run(create_app(ctx), host=settings.web_host, port=settings.web_port, proxy_headers=False, access_log=True, log_level="info")`。测试时 monkeypatch `uvicorn.run`，断言 host 和 port 正确。
- [ ] **`deploy/codexctl-web.service`**：
  - 写法同 codexctl.service，`ExecStart=/opt/codexctl/venv/bin/codexctl -c /etc/codexctl/config.toml web`，`After=codexctl.service`。
  - 设置 `Restart=on-failure`、`UMask=0077`。
  - 不设 ExecStartPre（守护进程那边已有自检）。
- [ ] **`deploy/nginx/codex-control.ppspace.xyz.conf`**：
  - 80 端口的 server：保留 `location /.well-known/acme-challenge/ { root /www/wwwroot/codex-control.ppspace.xyz; }`，其余请求 301 跳转到 https。
  - 443 端口的 server：`ssl_certificate /www/server/panel/vhost/cert/codex-control.ppspace.xyz/fullchain.pem;` 和 `ssl_certificate_key …/privkey.pem;`，`ssl_protocols TLSv1.2 TLSv1.3;`，`add_header Strict-Transport-Security "max-age=31536000" always;`，`client_max_body_size 1m;`。
  - `location /` 的反向代理设置：
    - `proxy_pass http://127.0.0.1:8700;`
    - `proxy_set_header Host $host;`
    - `proxy_set_header X-Real-IP $remote_addr;`
    - `proxy_set_header X-Forwarded-Proto https;`
    - `proxy_read_timeout 60s;`
  - 日志写到 `/www/wwwlogs/codex-control.ppspace.xyz.log`。
- [ ] **docs/deploy.md 新增「Web 面板」一节**，按顺序写：
  1. 安装面板依赖：`uv pip install` 重新安装。
  2. 用 `codexctl admin create <名字>` 创建管理员。
  3. 安装 systemd 单元并 `enable --now codexctl-web`。
  4. 创建 `/www/wwwroot/codex-control.ppspace.xyz`，先只放 80 端口的配置，`nginx -t` 检查后 reload。
  5. 用 acme.sh 签发证书：`acme.sh --issue -d codex-control.ppspace.xyz -w /www/wwwroot/codex-control.ppspace.xyz`，然后 `acme.sh --install-cert … --reloadcmd "nginx -t && nginx -s reload"`。
  6. 放入完整的 443 配置，`nginx -t` 检查后 reload。
  7. 验证：`curl -I https://codex-control.ppspace.xyz/login`。
  8. 说明证书由 acme.sh 的 cron 自动续期，并说明这个站点不会出现在宝塔面板的网站列表里。
- [ ] 提交：`feat: web 命令、systemd 单元、nginx 配置与面板部署文档`

---

## 完成标准

- `.venv/bin/pytest -q` 全部通过，Docker 集成镜像内的单元测试全部通过。
- 部署后能打开 `https://codex-control.ppspace.xyz/login`，管理员可以登录；总览页能看到 acc-root 显示为 needs_relogin；可以在网页内完成 device-auth，重新登录 acc-root 并添加新账号。
