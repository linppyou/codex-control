# codexctl 设计文档

- 日期：2026-11-11
- 状态：设计已确认，待编写实现计划

## 1. 背景与目标

在一台 Ubuntu/Debian 服务器上，由管理员通过 Web 控制面板集中管理 3 个 ChatGPT（Codex）账号，并把账号分配给服务器上的 10–50 个 Linux 用户使用。用户以 ChatGPT 登录态原生运行 `codex`（不使用 API key），且全程不自行登录/登出。

目标：

1. 管理员在面板中对每个账号**一次性登录**，面板保存其登录凭证，之后不再重新登录（除非凭证失效）。
2. 通过面板把账号**分配 / 切换 / 回收**给 Linux 用户，分配方式为**软链接**：`~<user>/.codex/auth.json` → 面板发布的账号凭证文件。
3. **实时监控** 3 个账号的额度（5 小时窗口、每周窗口）和每个用户的 token 用量。
4. 由面板**集中刷新** token，避免多个 Codex 客户端各自刷新导致 refresh token 失效。

非目标（本期不做）：

- 用户自助面板、LDAP/PAM 登录面板（面板仅管理员使用）。
- 多服务器管理。
- 按用户强制限额（Codex 客户端侧无法强制）。
- 额度满时自动换号（只告警，由管理员手动切换）。
- 巡检发现异常后的自动修复（只告警）。

## 2. 关键事实与约束

以下事实决定了设计，部分已在本机（macOS，codex-cli 0.166.0）实测：

| 事实 | 来源 | 影响 |
|---|---|---|
| ChatGPT 登录凭证存于 `$CODEX_HOME/auth.json`，字段：`auth_mode`、`last_refresh`、`tokens.{access_token, refresh_token, id_token, account_id}` | 本机实测 | vault 存储结构 |
| `access_token` 有效期 240 小时；`id_token` 1 小时 | 本机解析 JWT | 刷新周期 |
| Codex（0.167.0 实测）在 access_token 过期或请求被拒时自行刷新，单纯 `last_refresh` 超过 8 天不会触发；401 时会先从磁盘重读 `auth.json` | 服务器实测（docs/server-validation.md）、issue 日志 | 面板必须在 access_token 过期前刷新（每 4 天一次） |
| refresh token 一次性使用，旋转后旧 token 失效（`refresh_token_reused`），可能导致整个 token 家族被吊销 | OpenAI 文档、多个 issue | 只能有一个刷新方：面板 |
| Codex 写 `auth.json` 时**跟随软链接原地写入**，链接保留、目标权限不变 | 本机实测（API key 写入路径） | 软链接分配可行 |
| 软链接目标只读时，Codex 写入报 `Permission denied`，链接与目标均不变 | 本机实测 | 用户无法通过 `codex login` 篡改共享凭证 |
| `codex logout` 只删除链接本身，但对 ChatGPT 登录会在服务端吊销 token | 本机实测 + 文档 | 必须拦截 logout，否则同账号所有用户掉线 |
| 会话日志 `~/.codex/sessions/**/rollout-*.jsonl` 中的 `event_msg/token_count` 事件含 `info.total_token_usage`、`info.last_token_usage` 和 `rate_limits.{primary, secondary}.{used_percent, window_minutes, resets_at}`、`plan_type` | 本机实测 | 用户维度用量来源 |
| `/etc/codex/requirements.toml` 为管理员强制配置，支持 `allowed_login_methods` | OpenAI Managed configuration 文档 | 禁止 API key 登录 |

**服务器验证（2026-11-11 已通过，见 `docs/server-validation.md`）**：ChatGPT 模式下的 token 刷新写入同样跟随软链接；凭证文件只读时，正常使用期间 Codex 不写文件；额度接口响应结构与实现一致。

## 3. 总体架构

单进程 Python 服务（方案 A），以 root 运行、由 systemd 托管，只监听 `127.0.0.1:8700`，前置 Caddy（HTTPS）或通过 SSH 隧道访问。

```
codexctl/
├── web/        FastAPI 路由 + Jinja2/HTMX 页面；管理员认证
├── vault/      加密存取账号 token bundle（AES-GCM）
├── oauth/      device-auth 首次登录；OAuth refresh 刷新
├── publisher/  唯一写系统文件的模块：发布凭证文件、ACL、用户软链接
├── usage/      账号额度轮询；用户会话日志增量解析
├── watcher/    inotify + 定时巡检，只告警
├── scheduler/  APScheduler 定时任务
└── db/         SQLite（WAL 模式）与数据访问
```

模块边界：

- 只有 `publisher` 写系统文件；只有 `oauth` 调用登录/刷新；只有 `vault` 接触明文 token。
- 其他模块通过函数接口调用，各模块可独立单元测试。

技术选型：Python 3.11+、FastAPI、Jinja2 + HTMX、SQLite、APScheduler、`cryptography`（AES-GCM）、`argon2-cffi`、`httpx`、`inotify_simple`（或 `watchdog`）。

## 4. 系统侧布局（Ubuntu）

| 路径 | 说明 |
|---|---|
| `/etc/codexctl/config.toml` | 面板配置（监听地址、周期、阈值、webhook） |
| `/etc/codexctl/master.key` | vault 主密钥，root 0400 |
| `/var/lib/codexctl/codexctl.db` | SQLite 数据库，root 0600 |
| `/var/lib/codexctl/staging/<id>/` | device-auth 首次登录的临时 `CODEX_HOME`，登录完成后删除 |
| `/var/lib/codexctl/accounts/<acc>/auth.json` | 发布出的账号凭证。root 0600 + ACL `u:<user>:r`；目录 root 0711 |
| `/home/<user>/.codex/auth.json` | 软链接 → `/var/lib/codexctl/accounts/<acc>/auth.json` |
| `/opt/codex/` | 真实 `codex` 二进制 |
| `/usr/local/bin/codex` | 包装脚本：拦截 `login`、`logout` 子命令并提示联系管理员，其余参数 `exec` 给真实二进制 |
| `/etc/codex/requirements.toml` | `allowed_login_methods = ["chatgpt"]` |

依赖：`acl` 包（`setfacl`/`getfacl`），文件系统需支持 ACL（ext4/xfs 默认支持）。

凭证文件对普通用户**只读**（已确认）。

## 5. 数据模型（SQLite）

- `accounts`：`id`、`label`、`email`、`account_id`（唯一）、`plan_type`、`status`（`active` / `refreshing` / `needs_relogin` / `disabled`）、`last_refresh`、`access_expires_at`、`last_error`、`created_at`。
- `account_secrets`：`account_id`、`nonce`、`ciphertext`（加密的完整 `auth.json` 内容）、`version`、`updated_at`。
- `users`：`username`、`uid`、`home`、`note`；由系统用户同步（uid ≥ 1000）。
- `assignments`：`id`、`username`、`account_id`、`started_at`、`ended_at`（NULL 表示当前生效）。每个用户最多一条生效记录。
- `quota_samples`：`account_id`、`ts`、`window`（`primary` / `secondary`）、`used_percent`、`window_minutes`、`resets_at`、`source`（`api` / `log`）。
- `usage_events`：`username`、`account_id`、`ts`、`input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens`、`total_tokens`、`session_file`。
- `log_offsets`：`path`、`inode`、`offset`。
- `alerts`：`id`、`ts`、`level`、`kind`、`target`、`message`、`acknowledged`。
- `audit_log`：`id`、`ts`、`actor`、`action`、`target`、`detail`（不含任何 token）。
- `admins`：`username`、`password_hash`（argon2）。

## 6. 关键流程

### 6.1 添加账号（一次性登录）

1. 管理员点击"添加账号"；后台以 `CODEX_HOME=/var/lib/codexctl/staging/<id>` 执行 `codex login --device-auth`。
2. 从输出解析验证网址和验证码，在面板显示，并显示 15 分钟倒计时。
3. 管理员在浏览器中用目标账号批准。
4. 进程成功退出后，读取 `staging/<id>/auth.json`，从 `id_token` 解析 email、`account_id`、plan。若 `account_id` 已存在则提示重复并中止。
5. 加密写入 `account_secrets`，发布到 `accounts/<acc>/auth.json`，删除 staging 目录，写审计日志。

"重新登录"复用同一流程，成功后覆盖该账号的 secret 并重新发布；用户软链接不变。

### 6.2 分配 / 切换 / 回收（支持多选批量）

分配：

1. `getent passwd <user>` 校验用户存在且 uid ≥ 1000。
2. 确保 `~/.codex` 存在，属主为该用户，0700。
3. 若已有 `~/.codex/auth.json` 是普通文件，改名为 `auth.json.bak.<时间戳>`。
4. `setfacl -m u:<user>:r accounts/<acc>/auth.json`。
5. `ln -sfn accounts/<acc>/auth.json ~/.codex/auth.json`，并 `lchown` 为该用户。
6. 写入 `assignments` 与审计日志。

切换：先把链接改指向新账号，再撤销旧账号文件上的 ACL。面板提示：用户正在运行的 codex 进程内存中仍持有旧 token，重启 codex 后生效。

回收：删除软链接，撤销 ACL，关闭 `assignments` 记录。**永不执行 `codex logout`。**

发布文件的 ACL 始终由 `assignments` 中该账号的生效记录推导：分配、切换、回收和每次重新发布都按这份名单完整重设 ACL（`setfacl --set`），不做增量累积。

### 6.3 token 刷新

触发条件（任一）：距 `last_refresh` 满 4 天；`access_token` 剩余有效期少于 3 天；管理员手动触发。

流程：

1. 获取该账号的进程内锁，状态置为 `refreshing`。
2. 从 vault 解密当前 bundle，调用 OAuth refresh 接口（参数与 Codex 自身刷新一致，实现时参照 codex-rs 源码）。
3. 成功：组装新 `auth.json`（新 tokens + 新 `last_refresh`），在**同一数据库事务**内更新 `account_secrets` 和 `accounts`，提交。
4. 发布：写同目录临时文件（root 0600）→ 按该账号当前分配名单对临时文件设置 ACL → `fsync` → `rename` 覆盖 `auth.json`。先设 ACL 再 rename，保证替换瞬间权限即正确，用户不会出现读取失败的窗口。发布失败时按退避重试，vault 为唯一权威来源。
5. 网络/5xx 错误：指数退避重试（1m、5m、15m、1h）。
6. `invalid_grant` / `refresh_token_reused` 等不可恢复错误：状态置为 `needs_relogin`，产生告警；管理员执行 6.1 的"重新登录"即可恢复。

### 6.4 用量监控

账号额度（每 60 秒）：用 vault 中的 `access_token` 请求 ChatGPT 后端 usage 接口（非官方，实现时参照 codex-rs 与现有开源工具），记录 `primary`/`secondary` 的 `used_percent`、`window_minutes`、`resets_at`。接口失败时，用会话日志中最新的 `rate_limits` 作为 `source=log` 的样本。该请求只读，不触发刷新。

用户用量（每 60 秒）：

1. 遍历当前及近期有分配记录的用户，扫描 `~/.codex/sessions/**/rollout-*.jsonl`（只看最近 8 天修改过的文件）。
2. 依据 `log_offsets` 从上次位置继续读；inode 变化或文件变短则从头读。
3. 只解析 `type=event_msg` 且 `payload.type=token_count` 的行，使用 `info.last_token_usage` 作为增量写入 `usage_events`。
4. 按事件时间匹配 `assignments` 的时间段，归属到当时的账号。

告警：任一窗口 `used_percent` ≥ 80%（warning）或 ≥ 95%（critical）时产生告警，面板横幅展示，可选 webhook 通知。

### 6.5 巡检（只告警，不自动修复）

触发：inotify 监听各用户 `~/.codex` 目录与 `accounts/` 目录；另每 5 分钟全量扫描一次。

检查项：

- 用户软链接存在、指向其当前分配账号的文件；未被替换为普通文件。
- 未分配用户的 `~/.codex/auth.json` 不指向任何账号文件。
- 账号文件 ACL 与分配记录一致。
- 发布文件内容哈希与 vault 一致。
- token 临近过期而刷新未成功。

异常写入 `alerts` 和 `audit_log`，面板展示；面板为每类异常提供"一键修复"按钮，由管理员手动执行。

## 7. 面板页面

- **总览**：3 张账号卡片（5h / 每周额度进度条、重置倒计时、状态、分配人数）；告警列表。
- **账号**：添加（device-auth 弹窗）、手动刷新、重新登录、停用；账号详情与额度曲线。
- **用户**：列出 uid ≥ 1000 的系统用户，显示当前账号、近 24h / 7d token 用量、链接状态；搜索、多选批量分配 / 切换 / 回收。
- **用量**：按用户 / 账号、按天 / 小时的趋势与排行，CSV 导出。
- **审计**：全部操作与巡检异常。

## 8. 安全

- 面板只监听 127.0.0.1；管理员账号首次通过 CLI（`codexctl admin create`）创建；密码 argon2 哈希。
- Session cookie：HttpOnly、Secure（经 Caddy）、SameSite=Strict；所有写操作校验 CSRF token；登录失败限速。
- 明文 token 只存在于 vault 解密后的内存与发布出的 `auth.json`；日志、页面、审计中不输出 token。
- 特权操作只调用固定命令（`codex`、`setfacl`），以参数列表方式执行、不拼接 shell；用户名必须先经 `getent` 校验。软链接与文件操作优先使用 Python 系统调用。
- 已知且接受的风险（在文档与面板中注明）：
  - 用户可读取自己被分配账号的凭证并拷走；只能靠审计，泄露后对该账号"重新登录"使旧凭证失效。
  - 包装脚本可被绕过；若用户直接运行真实二进制执行 `logout`，同账号所有用户掉线，由巡检告警发现。
  - 多人共用一个账号会平摊额度，并可能触发 OpenAI 风控或违反使用条款。

## 9. 错误处理

- 所有外部 HTTP 调用设置超时（10s），按指数退避重试；失败状态记录在账号上并在面板展示。
- usage 接口格式变化只导致额度监控降级为日志来源，不影响用户使用。
- 启动自检：`setfacl` 可用、`/var/lib/codexctl` 所在文件系统支持 ACL、真实 `codex` 二进制存在并记录版本、主密钥权限为 0400、数据库可写。自检失败则拒绝启动并输出原因。
- Codex 版本变化时在面板提示需回归验证第 2 节的"待服务器验证"项。

## 10. 测试

- 单元测试：vault 加解密与密钥错误处理；JSONL 增量解析（追加、截断、轮转、坏行）；用量按分配时间段归属；刷新状态机（成功、可重试失败、不可恢复失败）；refresh 与 usage 接口用假 HTTP 服务模拟。
- 集成测试：Docker 中的 Ubuntu 容器，创建多个测试用户，覆盖分配 / 切换 / 回收，验证 ACL、软链接、"被分配用户可读、不可写，未分配用户不可读"，以及发布文件替换后 ACL 仍正确。
- 手动验收（真实账号、目标服务器）：ChatGPT 模式刷新写入跟随软链接；只读凭证下正常使用期间 Codex 不自刷新；面板刷新后正在运行的 codex 会话不中断。

## 11. 部署

- 安装：`apt install acl python3-venv`；把 codexctl 安装到 `/opt/codexctl/venv`；把真实 codex 安装到 `/opt/codex/`，放置 `/usr/local/bin/codex` 包装脚本；写入 `/etc/codex/requirements.toml`。
- 初始化：`codexctl init`（生成主密钥、建库、自检）→ `codexctl admin create`。
- systemd 单元 `codexctl.service`，以 root 运行，`Restart=on-failure`。
- 备份：`codexctl.db` 与 `master.key` 分开备份；丢失主密钥需对所有账号重新登录。
