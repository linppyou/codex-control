# codexctl 部署与运维

## 前提

- Ubuntu 22.04（自带 Python 3.10）或更新版本，root 权限。codexctl 需要 Python ≥ 3.11，见下文用 `uv` 安装。
- 已完成 `docs/server-validation.md` 中的前置验证且全部通过。
- 部署后还需处理的账号事项见 [docs/server-validation.md 的「账号清单」](server-validation.md#账号清单部署-codexctl-后处理)。

## 安装

```bash
sudo -i
apt-get install -y acl
cd /path/to/codex_control   # 服务器上的仓库检出目录；下文 deploy/... 相对路径均基于此目录
```

### 1. Python 3.11+ 环境（uv）

服务器自带 Python 3.10，不满足要求，用 `uv` 创建 3.12 虚拟环境：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
. ~/.local/bin/env   # 或重新登录，使 uv 进入 PATH
export UV_PYTHON_INSTALL_DIR=/opt/codexctl/python   # 解释器装到 /opt 下，避免虚拟环境依赖 /root/.local
uv venv --python 3.12 /opt/codexctl/venv
uv pip install --python /opt/codexctl/venv/bin/python /path/to/codex_control
```

备选：加 deadsnakes PPA 安装 `python3.12 python3.12-venv`，再 `python3.12 -m venv /opt/codexctl/venv && /opt/codexctl/venv/bin/pip install /path/to/codex_control`。

### 2. codex 二进制与包装脚本

使用完整的同版本 Codex 安装包，保留包中的目录结构。仅复制 `codex` 主程序会遗漏 `codex-code-mode-host` 等辅助程序，导致执行工具无法启动。以下示例安装服务器当前版本；升级时将版本号改为目标版本，并先备份现有安装：

```bash
mkdir -p /opt/codex
curl -fL https://github.com/openai/codex/releases/download/rust-v0.167.0/codex-package-x86_64-unknown-linux-musl.tar.gz -o /tmp/codex-package.tar.gz
tar -xzf /tmp/codex-package.tar.gz -C /opt/codex
/opt/codex/bin/codex --version
test -x /opt/codex/bin/codex-code-mode-host
install -m 755 deploy/codex-wrapper.sh /usr/local/bin/codex
install -d -m 755 /etc/codex && install -m 644 deploy/requirements.toml /etc/codex/requirements.toml
```

备选（全新服务器，尚未安装 codex）：先用 NodeSource 安装 Node 18+（不要用 apt 自带的旧版 nodejs），再执行：

```bash
npm install -g --prefix /opt/codex @openai/codex
```

### 3. 配置与启动

```bash
install -d -m 700 /etc/codexctl
[ -e /etc/codexctl/config.toml ] || install -m 600 deploy/config.example.toml /etc/codexctl/config.toml   # 已有配置则不覆盖
/opt/codexctl/venv/bin/codexctl init
install -m 644 deploy/codexctl.service /etc/systemd/system/codexctl.service
systemctl daemon-reload && systemctl enable --now codexctl
```

确认 `/usr/local/bin` 在用户 `PATH` 中排在其它 codex 安装位置之前：`sudo -u <user> -i which codex` 应输出 `/usr/local/bin/codex`。

systemd 单元设置了 `TimeoutStopSec=120` 与 `KillMode=mixed`：`systemctl stop` 时给进行中的 token 刷新最多 120 秒完成，避免刷新到一半被杀导致 refresh token 作废。

## 迁移现有登录

服务器上 linpp、zdy（以及 root）已经各自在 `~/.codex` 登录过。接入 codexctl 时：

- `codexctl assign` 分配账号给用户时，会把该用户已有的普通 `~/.codex/auth.json` 备份为 `auth.json.bak.<时间戳>`，再换成指向发布文件的软链接。
- **正在运行的 codex 进程仍持有内存中的旧 token，分配后必须重启**。例如 zdy 常驻的 `codex app-server` 需要重启后才会使用新账号。面板「用户」页会标出「仍有旧进程（仍用旧账号）」的用户并给出「重启 codex」按钮（会中断其正在运行的任务，请先通知对方）；也可以执行 `codexctl restart-codex <user>`。
- **永远不要**执行 `codex logout` 来「清理」旧登录：logout 会在服务端吊销 token，若该 token 与某个共享账号相同，会让所有共用者掉线。直接让 codexctl 备份并替换即可。
- 备份文件含旧凭证，确认迁移无误后由管理员自行决定是否删除。

## 创建新用户

在面板「用户」页底部的「添加用户」填写用户名、密码（至少 8 位，输入两次）后提交，会自动：

1. 用 `useradd --create-home --shell /bin/bash` 创建用户和家目录；
2. 用 `chpasswd` 经 stdin 设置密码（密码不出现在命令行参数、日志或审计里）；
3. 确保系统级 codex 可用：`/opt/codex/bin/codex` 缺失时按 `codex_version` 自动下载官方安装包，并补齐 `/usr/local/bin/codex` 包装脚本与 `/etc/codex/requirements.toml`；
4. 初始化 `~/.codex`（属主为该用户、0700），供随后分配账号使用。

创建完成后仍需在用户页勾选该用户并「分配 / 切换到所选账号」。CLI 等价命令：`codexctl user add <用户名>`（两次输入密码）。自动安装首次会下载较大的安装包；若服务器访问 GitHub 较慢，可在 `/etc/codexctl/config.toml` 中通过 `codex_package_url` 指向镜像，或设 `codex_auto_install = false` 后按「安装」一节手动安装。已有部署请同步更新 nginx 配置中的 `proxy_read_timeout 300s`（见 `deploy/nginx/codex-control.ppspace.xyz.conf`），否则首次安装可能被代理超时中断。

## 日常操作（`codexctl` 即 `/opt/codexctl/venv/bin/codexctl`）

| 操作 | 命令 |
|---|---|
| 创建 Linux 用户并安装/初始化 codex | `codexctl user add alice` |
| 添加账号（终端显示验证码，在浏览器批准） | `codexctl account add acc1` |
| 查看账号 | `codexctl account list` |
| 分配 / 切换（可多个用户） | `codexctl assign --account acc1 alice bob` |
| 回收 | `codexctl unassign alice` |
| 查看用户分配与链接状态 | `codexctl users` |
| 查看额度 | `codexctl quota` |
| 查看近 7 天用量 | `codexctl usage --days 7` |
| 立即巡检 | `codexctl check` |
| 修复某用户链接 | `codexctl fix alice` |
| 结束用户仍用旧账号的 codex 进程 | `codexctl restart-codex alice` |
| 账号显示 needs_relogin 时重新登录 | `codexctl account relogin acc1` |
| 发布文件与保险库不一致时 | `codexctl account republish acc1` |
| 查看服务日志 | `journalctl -u codexctl -f` |

注意：服务账号 www、mysql、redis、memcached 的 uid ≥ 1000（`min_uid`），因此也会出现在 `codexctl users` 里。它们不是真实使用者，不要给它们分配账号。

## 重要注意事项

- **永远不要**在服务器上以任何身份对已分配账号执行 `codex logout`：会在服务端吊销 token，所有共用该账号的用户掉线，只能 `account relogin` 恢复。
- 切换账号后，用户正在运行的 codex 进程仍使用内存中的旧 token，需重启 codex；面板「用户」页可一键结束这些旧进程（`codexctl restart-codex <user>`）。
- 用户可以读取自己被分配账号的凭证。怀疑泄露时对该账号执行 `account relogin`，使旧凭证作废。
- 多人共用一个账号会平摊额度，并可能触发 OpenAI 风控。

## 已知限制

- 包装脚本只是辅助手段，不是强制边界：用户仍可用 `codex -c k=v logout` 之类的写法绕过，IDE / app-server 也可以通过协议直接登出。
- 长时间运行的 codex 进程（超过 6 天，例如 zdy 的 app-server）可能在内存中持有过期 token，分配账号后需重启这些进程。
- 用量数字来自用户可写的会话日志，不具备防篡改能力。

## 备份

- `/var/lib/codexctl/codexctl.db`（含加密凭证）与 `/etc/codexctl/master.key` **分开**备份。
- 丢失主密钥：所有账号需重新 `account add`（先删除数据库或使用新状态目录）。

## 升级 Codex 后

重新执行 `docs/server-validation.md` 中的 Step 2–4，确认软链接写入行为与额度接口格式未变。

## Web 面板

域名 `codex-control.ppspace.xyz`。面板进程监听 Unix socket `/run/codexctl/web.sock`（属组 `www`，权限 0660），由 nginx 反向代理；不监听 TCP 端口。按顺序执行：

1. 重新安装包（带上面板依赖）：
   ```
   UV_PYTHON_INSTALL_DIR=/opt/codexctl/python uv pip install --python /opt/codexctl/venv/bin/python /opt/codexctl/src
   ```
2. 创建管理员（交互式输入密码，由管理员本人完成）：`codexctl admin create <名字>`。
3. 安装并启动 systemd 单元：
   ```
   cp deploy/codexctl-web.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now codexctl-web
   ```
4. 检查 socket：`ls -l /run/codexctl/web.sock`，应为 `srw-rw---- root www`。
5. 创建站点目录：`mkdir -p /www/wwwroot/codex-control.ppspace.xyz`。
6. 证书签发前先只放 80 端口配置，并重载 nginx：
   ```
   cp deploy/nginx/codex-control.ppspace.xyz.http-only.conf /www/server/panel/vhost/nginx/codex-control.ppspace.xyz.conf
   nginx -t && nginx -s reload
   ```
7. 如未安装 acme.sh：`curl https://get.acme.sh | sh -s email=<管理员邮箱>`。
8. 签发证书并安装到指定位置：
   ```
   acme.sh --issue -d codex-control.ppspace.xyz -w /www/wwwroot/codex-control.ppspace.xyz --webroot --server letsencrypt
   mkdir -p /www/server/panel/vhost/cert/codex-control.ppspace.xyz
   acme.sh --install-cert -d codex-control.ppspace.xyz \
     --fullchain-file /www/server/panel/vhost/cert/codex-control.ppspace.xyz/fullchain.pem \
     --key-file /www/server/panel/vhost/cert/codex-control.ppspace.xyz/privkey.pem \
     --reloadcmd "nginx -t && nginx -s reload"
   ```
9. 换成完整配置（80 跳转 + 443 反向代理）并重载：
   ```
   cp deploy/nginx/codex-control.ppspace.xyz.conf /www/server/panel/vhost/nginx/codex-control.ppspace.xyz.conf
   nginx -t && nginx -s reload
   ```
10. 验证：`curl -I https://codex-control.ppspace.xyz/login`，应返回 200；`curl -I http://codex-control.ppspace.xyz/` 应 301 到 https。

说明：

- 证书由 acme.sh 的 cron 自动续期，续期后由 `--reloadcmd` 重载 nginx。
- 这个站点是手工放进 `vhost/nginx` 的，不会出现在宝塔面板的网站列表里；不要在宝塔里为该域名再建站点。
- nginx 只向面板传 `X-Real-IP`（取自 `$remote_addr`），不转发客户端自带的 `X-Real-IP` / `X-Forwarded-For`。
- 登录限速保存在进程内存中，`systemctl restart codexctl-web` 会重置计数。
- 面板「用户」页可隐藏服务账号，在 `/etc/codexctl/config.toml` 中配置：`hidden_users = ["www", "mysql", "redis", "memcached"]`，改后重启 `codexctl-web`。

