# 面板内创建 Linux 用户并安装 codex 设计文档

- 日期：2026-11-12
- 状态：已实现（2026-11-12）

## 1. 背景与目标

此前开通一个新用户需要管理员 SSH 登录服务器，依次执行 `useradd`、`passwd`、准备 `~/.codex`，再确认 codex 已安装。用户页只能管理已存在的系统用户，无法新增。

目标：

1. 在 Web 面板「用户」页填写用户名与密码后，一键完成：
   - 创建 Linux 用户（家目录、登录 shell）；
   - 用 `chpasswd` 通过 stdin 设置密码；
   - 确保系统级 codex 可用（缺失时自动安装）；
   - 初始化该用户的 `~/.codex`（属主为该用户、0700），交给 codexctl 后续分配账号。
2. CLI 提供同等能力：`codexctl user add <name>`。
3. 密码不进入命令行参数、日志、页面、审计记录。
4. 新用户仍由管理员在用户页手动分配 codex 账号（不自动分配）。

非目标：

- 不自动分配 codex 账号：创建后仍需管理员勾选后「分配」。
- 不管理 sudo 权限、SSH 公钥、用户组、密码过期策略。
- 不删除或修改已有系统用户。
- 不在每个用户家目录单独安装一份 codex；沿用全服务器唯一安装（`/opt/codex`），保证账号由 codexctl 统一分配。
- 不自动升级已存在的 codex；只在二进制缺失时安装。

## 2. 创建流程

新增 `src/codexctl/useradmin.py`：

```python
validate_new_user(ctx, name, password, confirm) -> None
create(ctx, name, password, confirm, actor) -> tuple[SysUser, str]
```

顺序：

1. 校验用户名（沿用 `sysusers._NAME_RE`：小写字母或下划线开头，最长 32 位）、密码（至少 8 位、最长 128 位、不含换行/NUL）、两次密码一致。
2. 用 `pwd.getpwnam` 确认用户不存在；已存在则报错，不执行任何系统改动。
3. `codexinstall.ensure_codex(settings, http)`：确保 codex 二进制、包装脚本、强制配置就绪（见第 3 节）。放在创建用户之前，避免 codex 安装失败后留下无法使用的用户。
4. `sysusers.create_user(name, password)`：
   - 执行 `useradd --create-home --shell /bin/bash -- <name>`；
   - 执行 `chpasswd`，把 `name:password\n` 写入其 stdin；
   - 重新用 `pwd.getpwnam` 读取 uid/gid/home，并校验 uid ≥ `min_uid`、不是 `nobody`。
5. `Publisher.ensure_codex_dir(user)`：创建/校验 `~/.codex` 为普通目录、0700、属主为该用户。沿用分配账号时已有的安全逻辑（拒绝软链接目录，不跟随）。
6. 写审计 `action="user.create"`，`target=用户名`，`detail` 只包含 uid、家目录和 codex 状态，**不包含密码**。

Web 路由先校验输入，再把可能耗时的 codex 安装放在全局锁之外执行，最后在锁内创建用户并写审计，避免首次下载阻塞面板其它请求。

`chpasswd` 失败时用户已经创建：错误信息明确说明「用户已创建，但设置密码失败」，由管理员重试；不自动 `userdel -r`，避免误删已存在的家目录。

## 3. codex 安装（新增 `src/codexctl/codexinstall.py`）

`ensure_codex(settings, http) -> str` 幂等，返回给管理员看的中文说明：

1. 若 `settings.codex_bin` 不存在或不可执行：
   - `codex_auto_install = false` 时直接报错；
   - 否则按 `settings.codex_package_url`（默认 GitHub 官方 release）下载当前架构（x86_64 / aarch64）的 `codex-package-<arch>-unknown-linux-musl.tar.gz`；
   - 解压到临时目录，校验压缩包内路径（拒绝绝对路径、`..`、软链接/硬链接等非普通文件），再合并到 `codex_bin` 的安装目录；
   - 确认 `bin/codex` 可执行后返回「已安装 codex <版本>」。
2. 若 `settings.codex_wrapper`（默认 `/usr/local/bin/codex`）不存在，写入与 `deploy/codex-wrapper.sh` 相同的包装脚本（0755，`REAL` 指向 `settings.codex_bin`），拦截 `login` / `logout`。
3. 若 `settings.codex_requirements`（默认 `/etc/codex/requirements.toml`）不存在，写入 `allowed_login_methods = ["chatgpt"]`。
4. 已存在的包装脚本/强制配置不覆盖，避免覆盖管理员的定制。

新增配置项（均可省略）：

| 键 | 默认值 | 说明 |
|---|---|---|
| `codex_wrapper` | `/usr/local/bin/codex` | 包装脚本路径 |
| `codex_requirements` | `/etc/codex/requirements.toml` | 强制配置路径 |
| `codex_version` | `0.167.0` | 自动安装的版本 |
| `codex_package_url` | GitHub release 模板 | 支持 `{version}`、`{arch}` 占位符，可指向镜像 |
| `codex_auto_install` | `true` | 缺失时是否自动下载安装 |
| `codex_install_timeout` | `300` | 下载/安装超时（秒） |

自动安装首次会下载较大的安装包，可能持续数分钟。nginx 反向代理的 `proxy_read_timeout` 调整为 300s。

## 4. Web 面板

- 「用户」页表格下方新增「添加用户」面板，字段：用户名、密码、确认密码。
- `POST /users/create`（沿用全局 CSRF 校验）：
  - 成功：重定向 `/users`，提示「用户 X 已创建（uid …，家目录 …），codex 已就绪；请在列表勾选后分配账号」。
  - 失败：重定向并显示具体错误（用户已存在、密码太短/不一致、codex 安装失败等）。
- 密码输入框 `autocomplete="new-password"`，服务端只校验长度与一致性，不存明文。

## 5. CLI

- `codexctl user add <name>`：用 `getpass` 读取两次密码，不一致时报错；成功后打印 uid、家目录、codex 状态，并提示用 `codexctl assign --account <标签> <name>` 分配账号。
- 错误沿用 CLI 现有处理：打印 `错误：…` 并返回 1。

## 6. 安全

- 密码只通过 `chpasswd` 的 stdin 传递，不出现在 argv、错误信息、审计、HTML。
- 用户名先经正则校验再传给固定参数列表，不经过 shell。
- codex 安装包解压前校验成员类型与路径，防止路径穿越。
- 包装脚本和强制配置只在缺失时写入。
- 仅管理员可调用（面板已登录 + CSRF；CLI 需 root）。

## 7. 测试

- `tests/test_sysusers.py`：密码规则、用户名校验、已存在用户、`useradd`/`chpasswd` 参数与 stdin、失败路径。
- `tests/test_codexinstall.py`：用临时 tar.gz 与 httpx MockTransport 覆盖安装、幂等、已有包装脚本不覆盖、HTTP 失败、非法压缩包、架构映射、关闭自动安装。
- `tests/test_useradmin.py`：成功创建（codex 目录 0700、审计不含密码）、重复用户、密码不一致/过短、codex 安装失败时不创建用户。
- `tests/test_web_pages.py`：`POST /users/create` 成功与失败、CSRF（现有全路由测试自动覆盖）、页面表单渲染。
- `tests/test_cli.py`：`user add` 成功、两次密码不一致、非法输入。
- 集成测试：在 Docker（root）中真实执行 `sysusers.create_user`，验证用户可登录、家目录存在，测试后 `userdel -r` 清理。
