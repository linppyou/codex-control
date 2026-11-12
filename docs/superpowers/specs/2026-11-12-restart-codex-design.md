# 旧 codex 进程提示与一键重启 设计文档

- 日期：2026-11-12
- 状态：设计已确认，待编写实现计划

## 1. 背景与目标

codex 进程只在启动时读取一次 `~/.codex/auth.json`，之后一直使用内存中的 token，不会感知软链接指向的变化。因此管理员分配或切换账号后，用户已在运行的 codex 进程（尤其是常驻的 `codex app-server` 守护进程）仍使用旧账号。

实际案例（2026-11-12）：zdy 于 11:18 从 `zhangzhanshuai` 切换到 `acc-root`（显示名 pro100-1），链接与 ACL 均正确，但其 app-server（前一天 16:58 启动）仍在使用旧账号，只能由管理员 SSH 登录服务器手动 `kill`。

目标：

1. 在 Web 面板「用户」页标出仍在使用旧账号的 codex 进程。
2. 提供「重启 codex」按钮，由管理员在通知用户后一键结束这些进程，无需登录服务器。
3. CLI 提供同等能力。

非目标：

- 分配时自动结束进程（会在用户不知情时中断其任务）。
- 修改 codex 本身，让其热加载凭证。
- 识别用户自行安装在 `codex_bin` 所在目录之外的 codex。

## 2. 旧进程的判定（新模块 `src/codexctl/procs.py`）

### 2.1 codex 进程

满足以下全部条件的进程视为该用户的 codex 进程：

- 真实 uid 等于该用户 uid（`/proc/<pid>/status` 的 `Uid:` 第一列）。
- `/proc/<pid>/exe` 的链接目标位于 `settings.codex_bin` 的父目录下（默认 `/opt/codex/bin/`），例如 `codex`、`codex-code-mode-host`。目标带 ` (deleted)` 后缀（二进制已被升级替换）时去掉后缀再判断，仍视为 codex 进程。
- 读取任一 `/proc` 文件时进程已退出或无权限：跳过该进程，不报错。

### 2.2 启动时间

进程启动的 Unix 时间 = `/proc/stat` 中的 `btime` + `/proc/<pid>/stat` 第 22 个字段（`starttime`，单位为时钟滴答）÷ `os.sysconf("SC_CLK_TCK")`。解析 `/proc/<pid>/stat` 时以最后一个 `)` 之后的内容切分字段，避免进程名含空格或括号导致错位。

### 2.3 「旧」的定义

该用户最近一次分配变化时间 `changed_at` = assignments 表中该用户所有记录的 `started_at` 与 `ended_at` 的最大值（新增 `repo.last_assignment_change(conn, username) -> int | None`）。

- `changed_at` 为空（从未分配过）：不判定，不显示旧进程。
- 启动时间 `< changed_at` 的 codex 进程为旧进程。之后启动的进程不受影响。
- 回收后同样适用：回收前启动的进程仍持有被回收账号的 token，也是旧进程。

### 2.4 接口

```python
@dataclass(frozen=True)
class CodexProc:
    pid: int
    exe: str
    started_at: int  # Unix 秒

def list_codex_procs(uid: int, codex_dir: Path, proc_root: Path = Path("/proc")) -> list[CodexProc]: ...
def stale_procs(procs: list[CodexProc], changed_at: int | None) -> list[CodexProc]: ...
def terminate(pids: list[int], grace: float = 5.0, kill=os.kill, sleep=time.sleep, alive=...) -> tuple[list[int], list[int]]: ...  # (已结束, 无法结束)
```

`proc_root` 可注入，测试用临时目录构造假的 `/proc`。`terminate` 的 `kill`、`sleep` 和存活检查可注入，测试不需要真实进程。

### 2.5 结束进程

`terminate`：先对全部 pid 发 `SIGTERM`；每 0.2 秒检查一次，最多等待 `grace` 秒；仍存活的发 `SIGKILL`。`ProcessLookupError` 视为已退出；`PermissionError` 计入「无法结束」。返回 `(已结束, 无法结束)` 两个 pid 列表。

为防止 pid 复用误杀：发信号前重新调用 `list_codex_procs`，只对 pid 与启动时间都与判定时一致的进程发信号。

业务入口 `restart_codex(ctx, username, actor) -> tuple[list[int], list[int]]`（放在 `assignments.py`，返回值同 `terminate`）：查用户 → 计算旧进程 → `terminate` → 写审计日志 `action="restart_codex"`，`target=username`，`detail` 为结束的 pid 列表（逗号分隔）。没有旧进程时直接返回两个空列表，不写审计。

## 3. Web 面板

### 3.1 用户页

- 表格新增一列「codex 进程」：
  - 有旧进程：红色标签「N 个旧进程（仍用旧账号）」，`title` 中列出每个进程的 pid、程序名和启动时间。
  - 无旧进程：显示 `-`。
- 「操作」列在「修复」旁新增「重启 codex」按钮，仅在有旧进程时显示。按钮所在表单带 `onsubmit` 确认框：「将结束 <用户> 的 N 个旧 codex 进程，会中断其正在运行的任务。确定？」（沿用页面现有 CSRF 处理方式）。

### 3.2 路由

`POST /users/{username}/restart-codex`：调用 `restart_codex`，成功后重定向到 `/users`，提示「已结束 <用户> 的 N 个 codex 进程」；无旧进程时提示「<用户> 没有旧 codex 进程」。用户不存在等错误沿用现有的错误重定向处理。

### 3.3 分配后的提示

`POST /users/assign` 成功后，对本次涉及的每个用户计算旧进程数。若有，在成功提示后追加：「<用户> 有 N 个旧 codex 进程仍在使用旧账号，通知对方后点『重启 codex』」。回收（`/users/unassign`）同样追加。

## 4. CLI

- `codexctl users` 增加一列，显示旧进程数（`0` 显示为 `-`）。
- 新命令 `codexctl restart-codex <user>`：输出结束的 pid 列表，或「没有旧 codex 进程」。
- `codexctl assign` / `unassign` 完成后，若有旧进程，打印与 Web 相同的提示（命令改为 `codexctl restart-codex <user>`）。

## 5. 错误处理

- `/proc` 读不到（非 Linux 环境）：`list_codex_procs` 返回空列表，页面显示 `-`，功能静默失效。
- 发信号遇到 `PermissionError`：计入 `terminate` 返回的「无法结束」列表，Web 与 CLI 提示「部分进程无法结束：pid…」，审计中只记录实际结束的 pid。
- 进程判定在每次页面渲染时实时计算，不落库。

## 6. 测试

- `tests/test_procs.py`：用临时目录构造 `/proc`，覆盖 uid 过滤、exe 目录过滤、` (deleted)` 后缀、进程名含空格和括号、进程中途消失、启动时间计算、旧进程判定边界（等于 `changed_at` 不算旧）、`terminate` 的 TERM→KILL 升级与 `ProcessLookupError`、pid 复用保护。
- `tests/test_repo.py`：`last_assignment_change` 覆盖从未分配、分配中、已回收。
- `tests/test_web_pages.py` / `tests/test_web.py`：用户页显示旧进程标签与按钮；无旧进程时不显示按钮；重启路由写审计并重定向；分配后提示文案。
- `tests/test_cli.py`：`restart-codex` 命令与 `users` 新增列。

## 7. 文档

`docs/deploy.md` 中「迁移现有登录」「重要注意事项」「已知限制」里关于「需要重启 codex 进程」的说明，改为：在面板「用户」页通知用户后点「重启 codex」，或执行 `codexctl restart-codex <user>`。日常操作表格新增该命令。
