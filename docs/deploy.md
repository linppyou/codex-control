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
4. 初始化 `~/.codex`（属主为该用户、0700），供随后分配账号使用；
5. 开通普通面板账号，用户用刚设置的 Linux 密码登录后即可查看本人数据。

创建完成后仍需在用户页勾选该用户并「分配 / 切换到所选账号」。可通过 `codexctl user add <用户名>` 在命令行创建 Linux 用户，再用下文的 `web_system_users` 为其开通面板登录。自动安装首次会下载较大的安装包；若服务器访问 GitHub 较慢，可在 `/etc/codexctl/config.toml` 中通过 `codex_package_url` 指向镜像，或设 `codex_auto_install = false` 后按「安装」一节手动安装。已有部署请同步更新 nginx 配置中的 `proxy_read_timeout 300s`（见 `deploy/nginx/codex-control.ppspace.xyz.conf`），否则首次安装可能被代理超时中断。

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
3. 安装 PAM 服务配置并启动 systemd 单元：
   ```
   install -o root -g root -m 0644 deploy/pam.d/codexctl /etc/pam.d/codexctl
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

### 开通现有 Linux 用户的面板登录

1. 安装包含此功能的面板代码，并将 PAM 服务配置安装到系统。
   ```bash
   install -o root -g root -m 0644 deploy/pam.d/codexctl /etc/pam.d/codexctl
   ```
2. 在 `/etc/codexctl/config.toml` 设置允许登录的系统账号。
   ```toml
   web_system_users = ["zdy", "wangcheng", "liyujie", "husongfa", "xxhmxw", "zhangshunhui", "xieshihao", "chenjunhao"]
   ```
3. 执行 `systemctl restart codexctl-web`。用户从面板登录页输入当前 Linux 用户名和密码。每次登录都由本机 PAM 验证密码及账号有效期。Linux 密码修改后，下次登录使用新密码。

普通用户登录后进入「我的数据」，可以查看本人的账号分配、用量和会话。全站总览、全量导出、审计和服务器页面仅供 `admin` 访问。已有面板账号（例如 `linpp`）继续使用原有面板密码，访问范围同样限于本人数据。

Web 面板启动时，如不存在 `admin` 登录账号，会以密码 `root@root.root` 创建该账号。首次部署后通过 `codexctl admin passwd admin` 修改初始密码。

## 服务器与网站巡检

安装包含服务器面板的版本后，重启 `codexctl` 和 `codexctl-web`。控制端需要 OpenSSH 客户端、`getent` 和 curl。以 `admin` 登录面板，在「服务器」页面登记名称、公网 IP 和 SSH 端口。

1. 从站点服务器的可信控制台查看 `/etc/ssh/ssh_host_ed25519_key.pub`，在服务器表单中保存该主机公钥。
2. 将仓库的 `deploy/collect-sites.py` 安装到站点服务器。确认安装路径和文件均由 root 管理，并且 SSH 服务允许 root 使用公钥认证。
   ```bash
   install -D -o root -g root -m 0755 collect-sites.py /usr/local/libexec/codexctl-site-inventory
   ```
3. 打开服务器详情，把「采集授权公钥」原样追加到站点服务器的 `/root/.ssh/authorized_keys`。该行的 `restrict` 和 `command` 限制密钥只能执行只读采集脚本。每台服务器的独立私钥由面板生成，并使用现有主密钥加密保存。
4. 等待下一轮巡检，在详情页确认同步成功。守护进程启动后立即采集，之后每 5 分钟一次。建站日期取宝塔记录并转换为北京时间。删除服务器后，可按详情中公钥末尾的 `codexctl-site-<id>` 注释移除远端授权行。

## API 网关

### 启用

仅为本地客户端提供 API 的服务器可以跳过 ACL、Codex 二进制、包装脚本及服务器用户迁移步骤。先安装 Python 包和两个 systemd 服务。编辑 `/etc/codexctl/config.toml`。

```toml
deployment_mode = "gateway"
gateway_enabled = false
gateway_client_version = "0.167.0"
gateway_base_url = "https://codex-control.ppspace.xyz/v1"
```

运行 `codexctl init`，再运行 `codexctl admin create admin` 设置管理员密码。已有管理员可用 `codexctl admin passwd admin` 更改密码，已有会话随之失效。安装 HTTPS Nginx 配置后运行 `nginx -t` 并重载。把 `gateway_enabled` 改为 `true`，运行 `codexctl selfcheck` 后启动或重启 `codexctl` 与 `codexctl-web`。现有服务器如果仍需独立管理既有 Linux 用户，可以保留 `deployment_mode = "managed"` 并启用网关，新增的 API 请求仍直接转发模型调用。

Nginx 对 `/v1/` 关闭请求与响应缓冲及共享缓存，请求体允许 16 MiB，流空闲读超时为 360 秒。网关不限制速率或活跃上游连接数。若站点通过宝塔等 WAF 继承了全局限流，需为 `/v1/` 覆写相关规则。绑定账号受到上游额度限制时，客户端收到上游的 HTTP `429` 和可用的 `Retry-After`。API 访问日志使用路径和请求 ID。

### 账号、模型与 Key

1. 在管理员自己的电脑以单独的临时 `CODEX_HOME` 执行 `codex login --device-auth`。将该目录的 `auth.json` 经 SSH 传入服务器权限为 `0600` 的临时文件。用 `codexctl account import <标签> <文件>` 导入，或用 `codexctl account relogin <标签> --file <文件>` 更新同一账号，然后清理临时文件。
2. 在「用户」勾选用户并分配 Codex 账号。启用网关后，每个用户有一把长期有效的 Key。Key 随当前分配切换账号，回收账号后暂停调用，重新分配即可恢复。可用模型取自该账号的启用目录，在「账号 → 模型」可刷新目录及停用模型。通过「测试 Fast」且上游返回 `priority` 后，管理员可在用户详情中允许该用户使用 Fast。
3. 管理员在「用户」查看各用户的 Key。普通用户用同名面板账号登录，在自己的用户详情中查看密钥。启用 Linux 用户面板登录时，沿用 Linux 用户名及密码。仅使用网关模式时，管理员在「用户」创建面板账号。密钥加密存储，管理员可重置，重置后旧密钥立即失效。「用户 → API 用量」保留按请求实际使用的账号归集的近 7 天汇总，明细保留 90 天。
4. 在用户详情点击「查看密钥和配置」。Codex CLI 用本地 profile。CC Switch 的 Codex 和 DeepSeek Harness 自定义供应商使用 OpenAI Responses、以 `/v1` 结尾的 Base URL、该用户的 Key 和可用模型 ID。在 Harness 获取模型后勾选模型并保存供应商，或手动添加模型 ID。网关透传有效的 `max_output_tokens`。

升级旧版时，迁移优先保留每位用户在当前账号使用的有效 Key，其余重复、过期或未绑定用户的 Key 停用，历史用量保留。保留的 Key 可以继续调用，旧版数据库只保存了哈希，若需在面板查看原文，请由管理员重置并更新客户端。

如果 CC Switch 获取模型后，Harness 里看不到第三方供应商，检查本地 Harness 配置中的 `models[].input`。`llm-pi-ai` 只接受 `text` 和 `image`，含 `pdf` 的模型会导致整个插件配置校验失败。CC Switch v4.0.24 起在写入时过滤这些值。升级后重新保存已有供应商，再完全退出并重启 Harness。 [CC Switch v4.0.24 发布说明](https://github.com/farion1231/cc-switch/releases/tag/v4.0.24)

暂时无法升级时，可在本地 `~/.dsh/profiles/web/cordis.patch.yml` 和 `~/.dsh/profiles/desktop/cordis.patch.yml` 的已有文件中，将受影响模型的 `input: [text, image, pdf]` 改为 `input: [text, image]`。已设置 `DSH_HOME` 时以该目录为准，已使用 `headless` 或 `acp` 时也检查对应 profile。保存后重启 Harness。 [CC Switch 问题 #9116](https://github.com/farion1231/cc-switch/issues/9116)

「账号 → 模型」分别显示账号目录里的默认上下文和上限。Codex 原生目录保留这两个值，客户端配置可显式选择所用模型的上下文上限。兼容模型列表将可用上限发给 Harness，并用 `default_context_window` 保留上游默认值。

Fast 测试按上游最终返回的档位判断。显示实际使用标准档时，请求已经完成，Fast 还未对该账号和模型通过验证。

网关支持 `GET /v1/models`、版本化的 `GET /v1/codex/models` 和 `POST /v1/responses`。请求统一使用 Bearer Key。对话回放和上下文压缩走 Responses，请使用当前 Codex 版本。客户端把会话项随请求发送，密码、OAuth 凭证及工具执行权限由各自所在的客户端和服务器管理。服务器转发工具调用和客户端回传的结果。

### 核验与回退

```bash
# 应返回 HTTP 401 和 X-Request-ID
curl -i https://codex-control.ppspace.xyz/v1/models
# 在客户端安全设置 CODEXCTL_API_KEY 后执行，应返回此 Key 的模型列表
curl -fsS -H "Authorization: Bearer ${CODEXCTL_API_KEY}" https://codex-control.ppspace.xyz/v1/models
```

用 SQLite backup API 备份现有库，主密钥单独备份。关闭 `gateway_enabled` 并重启两个服务即可停用线上 API。如需回退到按 Key 绑定账号的旧版本，先停止服务，再恢复上一版本软件包及此次迁移前的数据库备份。该版本的旧表结构依赖备份恢复。

## 网站迁移任务面板

任务面板在 managed 模式下使用本机 Linux 用户及其已有 Codex 账号绑定。任务预览和确认由 Web 服务处理，发送由独立的 `codexctl-dispatch.service` 处理。升级 Python 包和 Nginx 配置，校验 Nginx 后重新加载，再把 `deploy/codexctl-dispatch.service` 安装到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 及 `systemctl enable --now codexctl-dispatch`。升级后重启 Web 服务以初始化新表。

1. 管理员在「任务 → Skill 管理」分别上传分发 Skill 和迁移 Skill 的 ZIP。ZIP 可直接使用发布包，最多 8 MiB，面板从 `SKILL.md` 读取名称及版本。包作为该任务的工程文件交给 Codex，发布包的安装器由面板跳过。
2. 管理员在「任务 → 目标站授权」选择现有服务器，填写目标域名、宝塔 AI 装修 HTTPS 接口、凭据环境变量名、允许执行的用户名。远程分发时填写执行用户的 Codex 环境可访问的项目名称或 ID。站点清单的 SSH 密钥仅供采集。部署接口的凭据应限定在授权站点，并允许指定的 Linux 执行用户使用。
3. 在仅 root 可读的 `/etc/codexctl/dispatch.env` 中配置对应的专用凭据环境变量，修改后重启 `codexctl-dispatch`。Worker 只把当前任务明确引用的部署凭据传给执行用户的 Codex 进程。接口地址和提示词中只放凭据引用。
4. 用户在「任务」填写原站网址、选择服务器及目标站和两个 Skill。核对提示词并勾选确认后，Worker 启动该用户的 Codex App Server，用任务专属目录中的 `task.csv` 与两个明确的 Skill 输入启动会话。管理员代提交时选择一个已有账号绑定且获目标站授权的普通用户。Web 进程和 Worker 可以独立重启，Worker 中断时，状态未知的发送保留原会话 ID 供核对。
5. 面板显示实际的 Codex 会话 ID、轮次 ID 和发送回执。任务文件位于 `state_dir/dispatch/<任务ID>`，权限属于执行用户，由 Codex 使用。若所选分发 Skill 还需要创建远程项目子会话、访问浏览器或运行迁移引擎，执行用户的 Codex 环境应提供相应工具、工作目录和依赖。App Server 回执表示已交接，站点上线和验收以 Codex 后续结果为准。
