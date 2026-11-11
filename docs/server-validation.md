# 服务器前置验证记录

- 日期：2026-11-11
- 服务器：Ubuntu 22.04.5 LTS，x86_64，ext4（`/var/lib` 所在分区支持 ACL）
- codex 版本：0.167.0（服务器已有的 `/usr/local/bin/codex` 静态二进制；测试未安装新版本）
- 测试账号：ChatGPT Plus 账号，在临时 `CODEX_HOME=/root/cxv/home` 中执行 `codex login --device-auth` 登录
- 对服务器的改动：安装了 `acl` 包；测试用户与测试文件已全部删除

## Step 2：刷新会跟随软链接写入 —— 通过

布局：`/root/cxv/home/auth.json -> /var/lib/cxv/auth.json`。

1. 只把 `last_refresh` 改成 9 天前，然后执行 `codex exec`：执行成功，但 **Codex 没有刷新**，`last_refresh` 和文件哈希都没变。
2. 把 `access_token` 的 `exp` 改成过去时间（签名随之失效），再执行 `codex exec`：Codex 触发了刷新，执行成功。
   - `ls -l` 显示链接仍然是 `auth.json -> /var/lib/cxv/auth.json`。
   - 目标文件内容变了：`last_refresh` 更新为 `2026-11-11T06:42:36Z`，`access_token` 换成新的（exp 为 10 天后）。
   - 目标文件权限仍为 `600 root`。

**结论**：ChatGPT 模式下刷新会顺着软链接写入目标文件。
**修正设计假设**：在 0.167.0 中，Codex 并不是"`last_refresh` 超过 8 天就刷新"，而是在 **access_token 过期**（或请求被拒）时才刷新。access_token 有效期 10 天；面板每 4 天刷新一次，access_token 剩余有效期始终不少于 6 天，所以 Codex 正常情况下不会自己刷新。

## Step 3：只读凭证下正常使用不写文件 —— 通过

- 测试用户 `cxtest`。ACL 为 `user::rw- user:cxtest:r-- group::--- mask::r-- other::---`；软链接 `/home/cxtest/.codex/auth.json -> /var/lib/cxv/auth.json`，属主为 cxtest。
- `cxtest` 尝试写入链接：`Permission denied`。
- `cxtest` 执行 `codex exec`：回复 OK，`EXIT=0`。
- `cxtest` 执行 `codex login status`：`Logged in using ChatGPT`。
- 执行前后 sha256 相同（`08a77de67b47cd41…`）。

## Step 4：额度接口响应结构 —— 与计划一致

`GET https://chatgpt.com/backend-api/wham/usage`，带 `Authorization: Bearer <access_token>` 和 `ChatGPT-Account-Id`，返回 200（身份字段已隐去）：

```json
{
  "plan_type": "plus",
  "rate_limit": {
    "allowed": true,
    "limit_reached": false,
    "primary_window":   {"used_percent": 0,  "limit_window_seconds": 18000,  "reset_after_seconds": 17789,  "reset_at": 1794397273},
    "secondary_window": {"used_percent": 22, "limit_window_seconds": 604800, "reset_after_seconds": 507251, "reset_at": 1794886736}
  },
  "code_review_rate_limit": null,
  "additional_rate_limits": null,
  "credits": {"has_credits": false, "unlimited": false, "balance": "0"},
  "rate_limit_reached_type": null
}
```

primary 为 5 小时窗口，secondary 为 7 天窗口，与计划 Task 13 中 `parse_usage` 的预期字段完全一致，无需修改。

## 部署时需要注意的服务器现状

- **Python 版本**：系统自带 Python 3.10，计划要求 ≥ 3.11（需要 `tomllib`）。部署时需要另装 Python 3.11+，例如用 uv 或 deadsnakes 源。
- **已有的 codex**：`/usr/local/bin/codex` 已被占用。用户 `linpp`、`zdy` 和 root 各有自己的 `~/.codex`，`zdy` 有常驻的 `codex app-server` 进程。部署包装脚本前，要先把真实二进制移到 `/opt/codex/bin/codex`，并确定如何处理这些用户现有的登录。
- **服务账号**：`www`、`mysql`、`redis`、`memcached` 的 uid 都 ≥ 1000，`codexctl users` 列表会把它们列出来。需要在配置里排除，或者在 Web 面板计划中增加用户白名单。
- **执行 codex exec 时**：stdin 必须重定向（`< /dev/null`），否则 Codex 会一直等待 stdin 输入。

## 账号清单（部署 codexctl 后处理）

| 标签 | 账号 | 套餐 | 状态 | 位置 / 处理方式 |
|---|---|---|---|---|
| `acc-root` | 483185561@qq.com | prolite | 失效（refresh token 已在服务端被吊销，2026-11-11 发现） | 凭证保存在服务器 `/root/codexctl-import/acc-root/auth.json`，同目录有 `STATUS` 说明；`/root/.codex/auth.json` 是指向它的软链接 |
| 待定 | linpp227@gmail.com | plus | 未保存（Task 0 测试后凭证已删除，账号本身正常） | 部署后用 `codexctl account add` 重新登录 |

部署后的处理顺序：

```bash
codexctl account import acc-root /root/codexctl-import/acc-root/auth.json
codexctl account relogin acc-root          # device-auth，需要在浏览器批准
codexctl account add <标签>                # linpp227@gmail.com，需要在浏览器批准
```

`acc-root` 导入后，即使暂时不执行 relogin，第一次定时刷新也会因为 refresh token 已被吊销而失败，系统会自动把它标记为 `needs_relogin` 并发出 critical 告警，不会被误当成可用账号。relogin 成功后：

- 把 root 的链接改为由 codexctl 管理（如果 root 也要用这个账号，执行 `codexctl assign`）；
- 删除 `/root/codexctl-import/acc-root/`。

注意：root 的 uid 是 0，低于 `min_uid`，所以不能直接 `assign` 给 root。如果 root 需要使用，需在 Web 面板计划中另行设计。
