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

普通用户登录后进入「我的数据」，可以查看本人的账号分配、用量和会话。所有已登录用户均可查看「服务器」模块中的服务器、同步状态与站点巡检。添加、编辑、删除服务器及生成接入命令仅供 `admin` 操作。全站总览、全量导出和审计仅供 `admin` 访问。已有面板账号（例如 `linpp`）继续使用原有面板密码。

Web 面板启动时，如不存在 `admin` 登录账号，会以密码 `root@root.root` 创建该账号。首次部署后通过 `codexctl admin passwd admin` 修改初始密码。

## 服务器与网站巡检

安装包含服务器面板的版本后，重启 `codexctl` 和 `codexctl-web`。控制端需要 OpenSSH 客户端、`getent` 和 curl。以 `admin` 登录面板，在「服务器」页面登记名称、公网 IP 和 SSH 端口。

### 一条命令接入

1. 添加服务器时，SSH 主机公钥可留空。保存后，详情页会显示一条有效期为 30 分钟的接入命令。
2. 点击「复制接入命令」，在目标服务器的宝塔终端或 SSH 终端中以 root 执行。目标服务器需要 Python 3、curl 和可用的 root 公钥 SSH 登录，且能够访问面板网址。
3. 命令会下载并安装随面板发布的采集脚本，写入该服务器专用的受限采集公钥，回传主机公钥。控制端通过 SSH 实际采集成功后，令牌立即失效并触发首次巡检。
4. 返回详情页刷新同步状态，点击「查看站点」。后续每 5 分钟自动巡检。若命令过期、执行失败或需要更新采集器，在详情页点击「重新生成接入命令」。安装可重复执行，同一采集公钥只保留一条授权。

接入令牌在数据库中加密保存，并通过哈希校验。重新生成命令或修改服务器连接信息会使旧命令失效。目标服务器已有主机公钥与回传值不一致时，接入会提示核对。

### 手动配置

1. 从站点服务器的可信控制台查看 `/etc/ssh/ssh_host_ed25519_key.pub`，在服务器表单中保存该主机公钥。
2. 将仓库的 `deploy/collect-sites.py` 安装到站点服务器。确认安装路径和文件均由 root 管理，并且 SSH 服务允许 root 使用公钥认证。
   ```bash
   install -D -o root -g root -m 0755 collect-sites.py /usr/local/libexec/codexctl-site-inventory
   ```
3. 打开服务器详情，把「采集授权公钥」原样追加到站点服务器的 `/root/.ssh/authorized_keys`。该行的 `restrict` 和 `command` 限制密钥只能执行只读采集脚本。每台服务器的独立私钥由面板生成，并使用现有主密钥加密保存。
4. 等待下一轮巡检，在详情页确认同步成功。守护进程启动后立即采集，之后每 5 分钟一次。建站日期取宝塔记录并转换为北京时间。删除服务器后，可按详情中公钥末尾的 `codexctl-site-<id>` 注释移除远端授权行。

已有站点服务器也需用第 2 步更新采集脚本。脚本从宝塔记录的站点根目录读取 WordPress 的静态数据库配置，通过服务器上的 MySQL 或 MariaDB 客户端只读查询本机数据库。「产品」统计已发布的 WooCommerce 产品，另行显示状态为 `importing` 的记录数。「菜单」统计含站点新增菜单项的经典导航菜单和含导航链接的已发布区块菜单。经典菜单查询按菜单标识和菜单项 ID 排除两种共享模板的预置项。旧采集器、缺少 WordPress 配置和查询失败会显示「导入情况待确认」。SSH 同步失败或超过巡检有效期时，面板会标明保留的上次数据。

### Linux 用户 SSH 登录站点

启用 `site_ssh_enabled = true` 和 `site_ssh_shared = true` 后，所有普通执行用户共用一把部署密钥。总控在 `{state_dir}/site-access/shared/` 保存 root 管理的原始密钥，并在创建用户时把同一私钥写入该用户的专用目录，权限为该用户所有、`0400`。已有用户通过 `codexctl user site-ssh <用户名...>` 迁移。共享模式优先于 CA 配置，不签发逐用户证书。

共享模式的服务器接入命令会同时安装受限采集公钥和共享部署公钥。执行一次接入命令后，已开通的用户即可使用同一公钥授权。已有服务器需要重新执行接入命令，或由管理员通过已有 SSH 权限追加共享公钥。服务器登记、修改、删除和接入完成时，自动刷新 `/etc/ssh/ssh_config.d/10-codexctl-shared-sites.conf` 及已知主机文件。各用户可直接运行 `ssh <服务器 IP>`，任务执行沿用用户目录中的私钥。

共享公钥允许远端 root 登录。持有私钥的用户共享这些服务器的访问权限。迁移前应备份原有用户密钥和远端授权文件，以便恢复。

在托管模式中启用 `site_ssh_enabled = true` 后，通过面板或 `codexctl user add` 创建的普通 Linux 用户会自动获得独立的 Ed25519 密钥。以 root 运行 `codexctl user site-ssh <用户名>` 可为已有用户开通站点 SSH 并显示其公钥。

如需由总控自动签发 SSH 证书，以 root 运行 `ssh-keygen -t ed25519 -N '' -C codexctl-site-access-ca -f /etc/codexctl/site-access-ca`。在总控的配置中设置 `site_ssh_ca_key = "/etc/codexctl/site-access-ca"`。在需要信任该 CA 的站点，把下面的授权追加到 `/root/.ssh/authorized_keys`，使用实际的总控出站 IP 和完整 CA 公钥。

```text
restrict,pty,from="<总控出站 IP>",cert-authority,principals="codexctl-site-root" <CA 公钥>
```

CA 私钥留在总控，仅 root 可读。证书的 Key ID 包含用户名和 UID。未启用 CA 的部署可逐台添加用户公钥并使用 `restrict,pty,from="<总控出站 IP>"` 限定来源。站点可通过 sshd 的 `RevokedKeys` 吊销证书或公钥。已有逐用户授权时，撤销访问还需移除对应用户公钥行。

在总控的 SSH 客户端配置中为这些站点设置 `User root`、登记端口，以及 `IdentityFile /var/lib/codexctl/site-access/%u-%i/id_ed25519`。如设置了 `state_dir`，该路径使用对应的状态目录。把登记的主机公钥写入 SSH 已知主机文件，并设置 `StrictHostKeyChecking yes`。各用户从总控运行 `ssh <站点 IP 或别名>` 即可使用其独立密钥。

## WP 服务中心

管理员可从服务器列表和详情页打开 WP 建站中心。总控使用共享 SSH 密钥读取目标机的 `ai_center` 页面，在隔离的 iframe 内显示，并将插件请求转发到同一台服务器。目标服务器需要启用共享站点 SSH 接入，并安装 `ai_center` 插件。旧的 `/wp-center` 地址会进入服务器列表。

数据中心的商品 CSV 和菜单 JSON 上传也经过这条 SSH 代理，不调用目标机的宝塔 `/files` 上传接口。嵌入页面会使用可恢复的分片上传，每片由总控限制在安全请求体范围内，因此不依赖目标服务器宝塔登录或外部反向代理的文件上传限制。

已有配置中的 `wp_ssh_key` 兼容忽略。

## API 网关

### 启用

仅为本地客户端提供 API 的服务器可以跳过 ACL、Codex 二进制、包装脚本及服务器用户迁移步骤。先安装 Python 包和两个 systemd 服务。编辑 `/etc/codexctl/config.toml`。

```toml
deployment_mode = "gateway"
gateway_enabled = false
gateway_client_version = "0.167.0"
gateway_base_url = "https://codex-control.ppspace.xyz/v1"
gateway_http2 = true
gateway_max_keepalive_connections = 128
gateway_keepalive_expiry = 90.0
gateway_transport = "http"
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

网关支持 `GET /v1/models`、版本化的 `GET /v1/codex/models` 和 `POST /v1/responses`。请求统一使用 Bearer Key。对话回放和上下文压缩走 Responses，请使用当前 Codex 版本。客户端把会话项随请求发送，密码、OAuth 凭证及工具执行权限由各自所在的客户端和服务器管理。服务器转发工具调用和客户端回传的结果。HTTP/2、上游连接池和凭证解密缓存默认启用。`gateway_transport = "auto"` 会优先尝试上游 Responses WebSocket，失败后回退到 HTTP SSE。切换前应完成同一账号的两轮 `previous_response_id` 续接测试。

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
2. 成员在「任务」上传 CSV 或 XLSX，最大 512 KiB，须包含 `网站,新域名,备注,折扣` 四列表头，顺序不限，其他列自动忽略。CSV 支持 UTF-8 或 GB18030 编码，XLSX 读取首个工作表，公式单元格需先粘贴为值。系统按新域名查询所有服务器最近一次成功巡检的绑定域名。结果同时显示导入内容与逐行匹配结果，全部行有效且站点匹配唯一时生成一份批次提示词。出现未匹配、重复或无效数据时，修正并重新上传后生成和执行；旧批次中的未匹配站点也需修正后重新上传。备注可留空。折扣可填写 `3` 或 `8.5折` 等数值，留空默认 `3`（3 折）。
3. 生成的提示词列出文件中新域名对应的服务器和端口，并要求 Codex 先读取 `capabilities` 与 `api.describe`，通过 `site.probe` 核对 WordPress 实况，在已授权范围内连续执行装修、队列部署及归档。过程与备份放在本站 `zdy-AI`，通过 `context.update` 更新接手摘要；断线沿用原任务和请求标识，资源繁忙时退避重试。部署接口和认证沿用工作目录及执行用户已有的配置。

管理员可在任务面板的「提示词生成代码」中编辑 `prompt = (...)`，先用两个虚拟站点预览，再保存或恢复默认代码。支持 f-string、字符串相加、变量索引、`len()`、`json.dumps(..., ensure_ascii=False)`；可用变量为 `dispatcher`、`migration`、`task_id`、`snapshot`、`rows`、`original`、`plan`。代码经语法、允许的表达式和示例输出校验后保存到数据库，修改会记录审计日志，仅影响之后导入的批次，不改写已有提示词。相同文件在模板修改后重新导入会生成新批次；发送时仍自动附加实际交接路径及 Skill 输入。
4. 确认页可修改工作目录、模型 ID 和思考程度。工作目录默认是执行用户家目录下的 `wp_front`，相对路径和 `~/` 按该用户家目录解析。模型默认 `gpt-6.1-terra`，思考程度默认 `medium`，发送时传入 Codex 的 `model` 与 `effort`。
5. Worker 在指定工作目录创建一个 Codex 会话，将原始 `upload.csv` 或 `upload.xlsx`、标准化 `task.csv`、服务器清单 `targets.json` 和两个 Skill 放入 `<工作目录>/.codexctl-tasks/<任务ID>`，以绝对路径交接。不同任务使用独立文件目录。面板记录真实会话 ID、轮次 ID、发送状态和 Codex 回复，轮次结束后显示“Codex 轮次已结束”。
6. 成员可在任务列表或详情点击「取消任务」。待确认和排队任务立即取消，执行中的任务先显示「正在取消」，Worker 请求中断当前 Codex 轮次并停止本机任务进程后显示「已取消」。已完成的操作保留，结果未知的原会话需要按页面提示核对。

管理员发布的 Skill 在 Worker 启动及发送任务前同步到各普通用户的 `~/.agents/skills` 和 `~/.codex/skills`。旧发布版本保留供已有会话继续读取，跨主机子会话仍需传递任务 Skill 文件。

### Codex 执行环境

任务以接收人的 Linux 用户身份执行，权限为 `danger-full-access`，批准策略为 `never`。分发器负责发送文件、Skill 和提示词，站点核对、采集、部署、等待、重试及归档由 Codex 按任务要求和 Skill 执行。

Worker 与 Codex 桌面端连接同一用户的共享 App Server，使用用户现有配置。运行中可在桌面端查看、补充指令或中断同一会话。结束后 Worker 取消订阅并关闭自己的连接，保留共享服务和其他客户端。

Worker 通过 App Server 的 `dynamicTools` 为总控会话注册 `list_projects`、`create_thread`、`send_message_to_thread`、`wait_threads`、`read_thread` 和 `set_thread_title`，并处理 `item/tool/call` 回调。项目范围是本次执行用户的工作目录，工具返回真实子会话和轮次 ID。子会话自动接收选定的迁移 Skill，状态及结果写入 `dispatch_children`。工具回执写入 `dispatch_tool_calls`，重复调用复用原回执；创建结果未知时保留记录供核对。取消批次会中断其运行中的子会话，总控先结束时 Worker 继续跟踪已启动的子会话。

`dispatch_browser_python` 和 `dispatch_chrome_binary` 分别通过 `BROWSER_PYTHON`、`HTW_CHROME_BINARY` 及 `CHROME_BIN` 传入会话，浏览器依赖检查由 Codex 执行。连接桥接使用 `websockets>=15,<16`，通过用户 `.codex/app-server-control/app-server-control.sock` 连接共享服务，首次连接时尝试启动该用户的 App Server daemon。
