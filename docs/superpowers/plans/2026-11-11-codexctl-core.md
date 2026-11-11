# codexctl 核心（后端 + 命令行）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 codexctl 的全部后端能力：账号一次性登录与加密保存、软链接分配 / 切换 / 回收、集中刷新 token、额度与用量监控、巡检告警，并通过 `codexctl` 命令行和 systemd 守护进程使用。

**Architecture:** 单进程 Python 服务，以 root 运行。SQLite 保存状态，AES-GCM 加密保存凭证。`publisher` 是唯一写系统文件的模块，负责发布 `/var/lib/codexctl/accounts/<label>/auth.json`、设置 ACL、管理用户 `~/.codex/auth.json` 软链接。APScheduler 单线程串行执行刷新、额度轮询、日志扫描、巡检四个定时任务。

**Tech Stack:** Python 3.11+、SQLite、cryptography（AESGCM）、httpx、APScheduler 3.x、pytest；部署目标 Ubuntu 24.04 / Debian 12，依赖 `acl` 包。

**设计文档：** `docs/superpowers/specs/2026-11-11-codexctl-design.md`

---

## 计划范围与差异说明

本计划是两份计划中的第一份：

- **本计划（核心）**：设计文档第 3–6、8–11 节的全部后端能力 + 命令行 + 部署文件 + Docker 集成测试。完成后即可在服务器上用命令行完整运营。
- **后续计划（Web 面板）**：FastAPI + HTMX 页面、管理员登录（argon2、CSRF、限速）、device-auth 网页弹窗、inotify 实时巡检、webhook 告警、面板上的"一键修复"。在本计划完成并上线验证后再写。

与设计文档的有意差异（均为简化，不改变行为）：

1. 不建 `users` 表：系统用户直接从 `pwd` 实时读取。
2. 账号状态只有 `active` / `needs_relogin` / `disabled`。不需要 `refreshing`，因为定时任务在单线程里串行执行，同一时刻只有一个刷新。
3. 告警表用 `resolved_at` 字段（NULL 表示未关闭）代替 `acknowledged`。
4. 巡检在本计划里只做定时全量扫描（每 5 分钟），inotify 放到 Web 面板计划。

## 文件结构

```
pyproject.toml                     包定义、依赖、pytest 配置
.gitignore / .dockerignore
src/codexctl/
  __init__.py                      版本号
  config.py                        Settings 与 TOML 加载
  db.py                            SQLite 连接、建表、事务
  events.py                        审计日志与告警（打开 / 更新 / 关闭）
  vault.py                         主密钥文件与 AES-GCM 加解密
  authfile.py                      auth.json 解析（AuthBundle）、JWT 声明、时间格式
  sysusers.py                      系统用户查询与校验
  runner.py                        外部命令执行（setfacl/getfacl）
  publisher.py                     唯一的特权文件操作：发布凭证、ACL、用户软链接
  repo.py                          全部 SQL 访问（账号、密文、分配、额度、用量、偏移）
  context.py                       Context：把配置、数据库、保险库、发布器、HTTP、时钟打包
  accounts.py                      账号服务：导入、device-auth 登录、重新登录、停用、发布
  assignments.py                   分配服务：分配、切换、回收、按记录修复
  oauth.py                         OAuth refresh 接口调用
  refresher.py                     刷新策略、退避、失败处理
  quota.py                         额度接口解析、记录、阈值告警
  logscan.py                       会话日志增量解析与用量归属
  watcher.py                       巡检
  selfcheck.py                     启动自检
  daemon.py                        定时任务调度
  cli.py                           命令行入口
tests/
  fakes.py                         测试替身：JWT/auth.json 工厂、假时钟、假命令执行器、假用户表
  conftest.py                      ctx fixture
  test_*.py                        各模块单元测试
  integration/Dockerfile           Ubuntu 容器
  integration/test_acl_integration.py  真实用户 + 真实 ACL 的集成测试
deploy/
  codex-wrapper.sh                 /usr/local/bin/codex 包装脚本
  requirements.toml                /etc/codex/requirements.toml
  codexctl.service                 systemd 单元
  config.example.toml              /etc/codexctl/config.toml 示例
docs/
  server-validation.md             Task 0 的验证记录
  deploy.md                        部署与运维手册
```

开发机为 macOS，单元测试全部可在 macOS 上运行（ACL 命令用假执行器替代）；真实 ACL 与多用户行为由 Docker 集成测试（Task 19）覆盖。

---

### Task 0: 服务器前置验证（人工，必须先完成）

设计依赖两个尚未在 Linux 上用 ChatGPT 账号验证的前提，以及一个非官方接口的响应格式。本任务在目标服务器上用一个测试账号执行，**结果不符合预期时停止实施并回报**。

**Files:**
- Create: `docs/server-validation.md`

- [ ] **Step 1: 安装 codex 到 /opt/codex 并登录一个测试账号**

```bash
sudo -i
apt-get install -y acl nodejs npm
npm install -g --prefix /opt/codex @openai/codex
/opt/codex/bin/codex --version
mkdir -p /root/cxv/home && chmod 700 /root/cxv
CODEX_HOME=/root/cxv/home /opt/codex/bin/codex login --device-auth
```

Expected: 打印验证网址和验证码；在浏览器批准后显示登录成功，`/root/cxv/home/auth.json` 存在。

- [ ] **Step 2: 验证 ChatGPT 模式刷新会跟随软链接写入**

```bash
mkdir -p /var/lib/cxv && chmod 711 /var/lib/cxv
mv /root/cxv/home/auth.json /var/lib/cxv/auth.json
ln -s /var/lib/cxv/auth.json /root/cxv/home/auth.json
python3 - <<'EOF'
import json, datetime
p = '/var/lib/cxv/auth.json'
d = json.load(open(p))
d['last_refresh'] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=9)).strftime('%Y-%m-%dT%H:%M:%S.000000000Z')
json.dump(d, open(p, 'w'), indent=2)
EOF
cd /tmp && CODEX_HOME=/root/cxv/home /opt/codex/bin/codex exec --skip-git-repo-check "reply with OK"
ls -l /root/cxv/home/auth.json
python3 -c "import json;print(json.load(open('/var/lib/cxv/auth.json'))['last_refresh'])"
```

Expected: `codex exec` 正常回复；`ls -l` 显示 `auth.json -> /var/lib/cxv/auth.json`（仍是软链接）；`last_refresh` 变为当前时间。
**如果 `auth.json` 变成了普通文件：设计前提不成立，停止并回报。**

- [ ] **Step 3: 验证只读凭证下正常使用不会写文件**

```bash
useradd -m cxtest
chmod 600 /var/lib/cxv/auth.json && setfacl -m u:cxtest:r /var/lib/cxv/auth.json
install -d -o cxtest -g cxtest -m 700 /home/cxtest/.codex
ln -s /var/lib/cxv/auth.json /home/cxtest/.codex/auth.json && chown -h cxtest:cxtest /home/cxtest/.codex/auth.json
sha256sum /var/lib/cxv/auth.json
cd /tmp && sudo -u cxtest -H /opt/codex/bin/codex exec --skip-git-repo-check "reply with OK"
sudo -u cxtest -H /opt/codex/bin/codex login status
sha256sum /var/lib/cxv/auth.json
```

Expected: `codex exec` 正常回复，`login status` 显示已用 ChatGPT 登录，前后两次 `sha256sum` 相同。

- [ ] **Step 4: 记录额度接口的响应格式**

```bash
python3 - <<'EOF'
import json, urllib.request
t = json.load(open('/var/lib/cxv/auth.json'))['tokens']
req = urllib.request.Request('https://chatgpt.com/backend-api/wham/usage', headers={
    'Authorization': 'Bearer ' + t['access_token'],
    'ChatGPT-Account-Id': t['account_id'],
    'User-Agent': 'codexctl-validate'})
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2))
EOF
```

Expected: 输出 JSON 含 `rate_limit.primary_window.used_percent`、`limit_window_seconds`、`reset_at` 或 `reset_after_seconds`（`secondary_window` 可能为 null）。
**如果字段名不同：在 Task 13 实现前，按实际响应修改 `quota.parse_usage` 及其测试中的 `BODY`。** 如果被拦截（403/Cloudflare），记录下来；额度监控将依赖会话日志降级来源，不阻塞实施。

- [ ] **Step 5: 清理并记录结果**

```bash
userdel -r cxtest
rm -rf /var/lib/cxv /root/cxv
```

把 Step 2–4 的实际输出（去掉任何 token）写入 `docs/server-validation.md`，格式：

```markdown
# 服务器前置验证记录

- 日期 / 服务器 / codex 版本：
- Step 2 刷新跟随软链接：通过 / 不通过（ls -l 输出、last_refresh 前后值）
- Step 3 只读凭证正常使用：通过 / 不通过（sha256 前后值、login status 输出）
- Step 4 额度接口响应结构：（粘贴 JSON 结构）
```

```bash
git add docs/server-validation.md
git commit -m "docs: 记录服务器前置验证结果"
```

---

### Task 1: 项目骨架

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.dockerignore`, `src/codexctl/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: 写 pyproject.toml**

```toml
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = "codexctl"
version = "0.1.0"
description = "Centralized Codex (ChatGPT) account control for multi-user Linux servers"
requires-python = ">=3.11"
dependencies = [
    "cryptography>=42",
    "httpx>=0.27",
    "apscheduler>=3.10,<4",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
codexctl = "codexctl.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/codexctl"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: needs root, real users and setfacl (run in Docker)"]
addopts = "-m 'not integration'"
```

- [ ] **Step 2: 写 .gitignore、.dockerignore、包初始化文件**

`.gitignore`:

```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
dist/
```

`.dockerignore`:

```
.venv
.git
**/__pycache__
.pytest_cache
```

`src/codexctl/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 3: 写冒烟测试**

`tests/test_smoke.py`:

```python
import codexctl


def test_version():
    assert codexctl.__version__ == "0.1.0"
```

- [ ] **Step 4: 建虚拟环境并运行测试**

Run:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -v
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore .dockerignore src tests
git commit -m "chore: codexctl 项目骨架"
```

---

### Task 2: 配置（config.py）

**Files:**
- Create: `src/codexctl/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest

from codexctl.config import Settings, load_settings


def test_defaults_when_file_missing(tmp_path):
    s = load_settings(tmp_path / "missing.toml")
    assert s == Settings()
    assert s.db_path == Path("/var/lib/codexctl/codexctl.db")
    assert s.accounts_dir == Path("/var/lib/codexctl/accounts")
    assert s.staging_dir == Path("/var/lib/codexctl/staging")


def test_overrides_and_path_conversion(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('state_dir = "/srv/cx"\nmin_uid = 2000\n')
    s = load_settings(cfg)
    assert s.state_dir == Path("/srv/cx")
    assert s.min_uid == 2000
    assert s.staging_dir == Path("/srv/cx/staging")


def test_unknown_key_rejected(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("nope = 1\n")
    with pytest.raises(ValueError, match="unknown config key: nope"):
        load_settings(cfg)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'codexctl.config'`

- [ ] **Step 3: 实现**

`src/codexctl/config.py`:

```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

_PATH_KEYS = {"state_dir", "key_file", "codex_bin"}


@dataclass(frozen=True)
class Settings:
    state_dir: Path = Path("/var/lib/codexctl")
    key_file: Path = Path("/etc/codexctl/master.key")
    codex_bin: Path = Path("/opt/codex/bin/codex")
    setfacl_bin: str = "setfacl"
    getfacl_bin: str = "getfacl"
    min_uid: int = 1000
    refresh_interval_days: float = 4.0
    refresh_min_validity_days: float = 3.0
    quota_warn_percent: float = 80.0
    quota_crit_percent: float = 95.0
    oauth_token_url: str = "https://auth.openai.com/oauth/token"
    oauth_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    usage_url: str = "https://chatgpt.com/backend-api/wham/usage"
    http_timeout: float = 10.0
    log_lookback_days: int = 8

    @property
    def db_path(self) -> Path:
        return self.state_dir / "codexctl.db"

    @property
    def accounts_dir(self) -> Path:
        return self.state_dir / "accounts"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"


def load_settings(path: Path | None) -> Settings:
    if path is None or not path.exists():
        return Settings()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Settings)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {key}")
        kwargs[key] = Path(value) if key in _PATH_KEYS else value
    return Settings(**kwargs)
```

说明：`oauth_client_id` 只是兜底值；刷新时优先使用 access_token 声明里的 `client_id`（本机实测为同一值）。

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/config.py tests/test_config.py
git commit -m "feat: 配置加载"
```

---

### Task 3: 数据库（db.py）

**Files:**
- Create: `src/codexctl/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_db.py`:

```python
import sqlite3

import pytest

from codexctl import db


def test_schema_created():
    conn = db.connect(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "accounts", "account_secrets", "assignments", "quota_samples",
        "usage_events", "log_offsets", "alerts", "audit_log",
    } <= names


def test_transaction_rolls_back_on_error():
    conn = db.connect(":memory:")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO audit_log(ts, actor, action, target) VALUES (1, 'a', 'b', 'c')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_transaction_commits():
    conn = db.connect(":memory:")
    with db.transaction(conn):
        conn.execute("INSERT INTO audit_log(ts, actor, action, target) VALUES (1, 'a', 'b', 'c')")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_only_one_active_assignment_per_user():
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO accounts(label, account_id, last_refresh, access_expires_at, created_at) "
        "VALUES ('a', 'x', 0, 0, 0)"
    )
    conn.execute("INSERT INTO assignments(username, account_pk, started_at) VALUES ('alice', 1, 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO assignments(username, account_pk, started_at) VALUES ('alice', 1, 1)")


def test_file_database_uses_wal(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL，`cannot import name 'db'`

- [ ] **Step 3: 实现**

`src/codexctl/db.py`:

```python
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    email TEXT,
    account_id TEXT NOT NULL UNIQUE,
    plan_type TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_refresh INTEGER NOT NULL,
    access_expires_at INTEGER NOT NULL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_secrets (
    account_pk INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    blob BLOB NOT NULL,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS assignments_one_active
    ON assignments(username) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS quota_samples (
    id INTEGER PRIMARY KEY,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    win TEXT NOT NULL,
    used_percent REAL NOT NULL,
    window_minutes INTEGER,
    resets_at INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_samples_latest ON quota_samples(account_pk, win, ts);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    session_file TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_events_user_ts ON usage_events(username, ts);
CREATE TABLE IF NOT EXISTS log_offsets (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    last_totals TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open
    ON alerts(kind, target) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/db.py tests/test_db.py
git commit -m "feat: SQLite 结构与事务"
```

---

### Task 4: 审计与告警（events.py）

**Files:**
- Create: `src/codexctl/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_events.py`:

```python
from codexctl import db, events


def test_audit_inserts_row():
    conn = db.connect(":memory:")
    events.audit(conn, "cli", "assign", "alice", "acc1", now=10)
    row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert (row["ts"], row["actor"], row["action"], row["target"], row["detail"]) == (10, "cli", "assign", "alice", "acc1")


def test_raise_alert_updates_open_alert_instead_of_duplicating():
    conn = db.connect(":memory:")
    events.raise_alert(conn, "warning", "quota_high", "acc1", "81%", now=1)
    events.raise_alert(conn, "critical", "quota_high", "acc1", "97%", now=2)
    rows = events.open_alerts(conn)
    assert len(rows) == 1
    assert (rows[0]["level"], rows[0]["message"], rows[0]["ts"]) == ("critical", "97%", 2)


def test_resolve_then_raise_creates_new_alert():
    conn = db.connect(":memory:")
    events.raise_alert(conn, "warning", "quota_high", "acc1", "81%", now=1)
    events.resolve_alert(conn, "quota_high", "acc1", now=2)
    assert events.open_alerts(conn) == []
    events.raise_alert(conn, "warning", "quota_high", "acc1", "85%", now=3)
    assert len(events.open_alerts(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


def test_resolve_missing_alert_is_noop():
    conn = db.connect(":memory:")
    events.resolve_alert(conn, "quota_high", "nope", now=1)
    assert events.open_alerts(conn) == []
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: FAIL，`cannot import name 'events'`

- [ ] **Step 3: 实现**

`src/codexctl/events.py`:

```python
from __future__ import annotations

import sqlite3


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str, detail: str = "", *, now: int) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
        (now, actor, action, target, detail),
    )


def raise_alert(conn: sqlite3.Connection, level: str, kind: str, target: str, message: str, *, now: int) -> None:
    conn.execute(
        """
        INSERT INTO alerts(ts, level, kind, target, message) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind, target) WHERE resolved_at IS NULL
        DO UPDATE SET ts = excluded.ts, level = excluded.level, message = excluded.message
        """,
        (now, level, kind, target, message),
    )


def resolve_alert(conn: sqlite3.Connection, kind: str, target: str, *, now: int) -> None:
    conn.execute(
        "UPDATE alerts SET resolved_at = ? WHERE kind = ? AND target = ? AND resolved_at IS NULL",
        (now, kind, target),
    )


def open_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY ts DESC, id DESC").fetchall()
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/events.py tests/test_events.py
git commit -m "feat: 审计日志与告警"
```

---

### Task 5: 凭证保险库（vault.py）

**Files:**
- Create: `src/codexctl/vault.py`
- Test: `tests/test_vault.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_vault.py`:

```python
import stat

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl.vault import Vault, VaultError, generate_key_file, load_key


def test_roundtrip():
    v = Vault(AESGCM.generate_key(bit_length=256))
    blob = v.encrypt(b"secret", b"acc1")
    assert b"secret" not in blob
    assert v.decrypt(blob, b"acc1") == b"secret"


def test_wrong_aad_fails():
    v = Vault(AESGCM.generate_key(bit_length=256))
    blob = v.encrypt(b"secret", b"acc1")
    with pytest.raises(VaultError, match="解密失败"):
        v.decrypt(blob, b"acc2")


def test_wrong_key_fails():
    blob = Vault(AESGCM.generate_key(bit_length=256)).encrypt(b"secret", b"a")
    with pytest.raises(VaultError):
        Vault(AESGCM.generate_key(bit_length=256)).decrypt(blob, b"a")


def test_generate_and_load_key(tmp_path):
    path = tmp_path / "etc" / "master.key"
    generate_key_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert len(load_key(path)) == 32


def test_generate_refuses_to_overwrite(tmp_path):
    path = tmp_path / "master.key"
    generate_key_file(path)
    with pytest.raises(FileExistsError):
        generate_key_file(path)


def test_load_key_rejects_loose_permissions(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"x" * 32)
    path.chmod(0o644)
    with pytest.raises(VaultError, match="权限过宽"):
        load_key(path)


def test_load_key_rejects_wrong_length(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"short")
    path.chmod(0o400)
    with pytest.raises(VaultError, match="32 字节"):
        load_key(path)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_vault.py -v`
Expected: FAIL，`No module named 'codexctl.vault'`

- [ ] **Step 3: 实现**

`src/codexctl/vault.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class VaultError(Exception):
    pass


def generate_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as f:
        f.write(AESGCM.generate_key(bit_length=256))


def load_key(path: Path) -> bytes:
    if path.stat().st_mode & 0o077:
        raise VaultError(f"{path} 权限过宽，必须只有属主可读（chmod 400）")
    key = path.read_bytes()
    if len(key) != 32:
        raise VaultError(f"{path} 不是 32 字节的主密钥")
    return key


class Vault:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        try:
            return self._aead.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], aad)
        except InvalidTag as e:
            raise VaultError("解密失败：主密钥不匹配或数据被篡改") from e
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_vault.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/vault.py tests/test_vault.py
git commit -m "feat: AES-GCM 凭证保险库"
```

---

### Task 6: auth.json 解析（authfile.py）与测试替身

**Files:**
- Create: `src/codexctl/authfile.py`, `tests/fakes.py`
- Test: `tests/test_authfile.py`

- [ ] **Step 1: 写测试替身模块**

`tests/fakes.py`（后续所有任务共用；本任务先放 JWT 与 auth.json 工厂，其余替身在 Task 9 追加）：

```python
from __future__ import annotations

import base64
import json

T0 = 1_800_000_000  # 2027-01-15T08:00:00Z
T0_RFC3339 = "2027-01-15T08:00:00.000000000Z"


def make_jwt(claims: dict) -> str:
    def enc(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def make_auth_json(
    *,
    account_id: str = "acct-1",
    email: str = "a@example.com",
    plan: str = "pro",
    access_exp: int = T0 + 10 * 86400,
    last_refresh: str = T0_RFC3339,
    refresh_token: str = "rt-1",
    client_id: str = "app_test",
) -> bytes:
    auth_claim = {"chatgpt_account_id": account_id, "chatgpt_plan_type": plan}
    id_token = make_jwt({"email": email, "https://api.openai.com/auth": auth_claim, "exp": T0 + 3600})
    access_token = make_jwt({"client_id": client_id, "exp": access_exp, "https://api.openai.com/auth": auth_claim})
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "last_refresh": last_refresh,
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
        }
    ).encode()
```

- [ ] **Step 2: 写失败的测试**

`tests/test_authfile.py`:

```python
import json

import pytest

from codexctl.authfile import AuthBundle, AuthFileError, format_rfc3339, parse_rfc3339
from fakes import T0, make_auth_json, make_jwt


def test_parses_fields():
    b = AuthBundle.from_bytes(make_auth_json())
    assert b.account_id == "acct-1"
    assert b.email == "a@example.com"
    assert b.plan_type == "pro"
    assert b.access_expires_at == T0 + 10 * 86400
    assert b.client_id == "app_test"
    assert b.last_refresh == T0
    assert b.refresh_token == "rt-1"


def test_rejects_api_key_mode():
    data = json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}).encode()
    with pytest.raises(AuthFileError, match="auth_mode"):
        AuthBundle.from_bytes(data)


def test_rejects_missing_refresh_token():
    raw = json.loads(make_auth_json())
    del raw["tokens"]["refresh_token"]
    with pytest.raises(AuthFileError, match="refresh_token"):
        AuthBundle.from_bytes(json.dumps(raw).encode())


def test_rejects_invalid_json():
    with pytest.raises(AuthFileError, match="JSON"):
        AuthBundle.from_bytes(b"not json")


def test_with_refreshed_replaces_tokens_and_keeps_original():
    original = AuthBundle.from_bytes(make_auth_json())
    new_access = make_jwt({"client_id": "app_test", "exp": T0 + 20 * 86400})
    updated = original.with_refreshed(access_token=new_access, refresh_token=None, id_token=None, now=T0 + 5)
    assert updated.access_token == new_access
    assert updated.refresh_token == "rt-1"
    assert updated.last_refresh == T0 + 5
    assert updated.access_expires_at == T0 + 20 * 86400
    assert original.access_token != new_access


def test_with_refreshed_rotates_refresh_token():
    original = AuthBundle.from_bytes(make_auth_json())
    updated = original.with_refreshed(
        access_token=original.access_token, refresh_token="rt-2", id_token=None, now=T0
    )
    assert updated.refresh_token == "rt-2"


def test_to_bytes_roundtrip_is_stable():
    b = AuthBundle.from_bytes(make_auth_json())
    assert AuthBundle.from_bytes(b.to_bytes()).to_bytes() == b.to_bytes()


def test_rfc3339_roundtrip():
    assert format_rfc3339(T0) == "2027-01-15T08:00:00.000000000Z"
    assert parse_rfc3339(format_rfc3339(T0 + 7)) == T0 + 7
    assert parse_rfc3339("2026-11-10T14:01:42.571000000Z") == 1794319302
    assert parse_rfc3339("2026-11-11T05:44:28.201Z") == 1794375868
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_authfile.py -v`
Expected: FAIL，`No module named 'codexctl.authfile'`

- [ ] **Step 4: 实现**

`src/codexctl/authfile.py`:

```python
from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone

AUTH_CLAIM = "https://api.openai.com/auth"


class AuthFileError(ValueError):
    pass


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as e:
        raise AuthFileError("token 不是有效的 JWT") from e
    if not isinstance(claims, dict):
        raise AuthFileError("JWT payload 不是对象")
    return claims


def format_rfc3339(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"


def parse_rfc3339(value: str) -> int:
    dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@dataclass(frozen=True, eq=False)
class AuthBundle:
    raw: dict

    @classmethod
    def from_bytes(cls, data: bytes) -> "AuthBundle":
        try:
            raw = json.loads(data)
        except ValueError as e:
            raise AuthFileError("auth.json 不是有效的 JSON") from e
        if not isinstance(raw, dict) or raw.get("auth_mode") != "chatgpt":
            raise AuthFileError("auth.json 不是 ChatGPT 登录凭证（auth_mode 必须为 chatgpt）")
        tokens = raw.get("tokens")
        if not isinstance(tokens, dict):
            raise AuthFileError("auth.json 缺少 tokens")
        for key in ("access_token", "refresh_token", "id_token"):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise AuthFileError(f"auth.json 缺少 tokens.{key}")
        bundle = cls(raw)
        if not bundle.account_id:
            raise AuthFileError("无法确定 ChatGPT account_id")
        bundle.access_expires_at  # 校验 access_token 可解析且含 exp
        return bundle

    @property
    def tokens(self) -> dict:
        return self.raw["tokens"]

    @property
    def access_token(self) -> str:
        return self.tokens["access_token"]

    @property
    def refresh_token(self) -> str:
        return self.tokens["refresh_token"]

    @property
    def id_token(self) -> str:
        return self.tokens["id_token"]

    def _id_claims(self) -> dict:
        return jwt_claims(self.id_token)

    @property
    def account_id(self) -> str:
        return self.tokens.get("account_id") or self._id_claims().get(AUTH_CLAIM, {}).get("chatgpt_account_id") or ""

    @property
    def email(self) -> str | None:
        return self._id_claims().get("email")

    @property
    def plan_type(self) -> str | None:
        return self._id_claims().get(AUTH_CLAIM, {}).get("chatgpt_plan_type")

    @property
    def access_expires_at(self) -> int:
        exp = jwt_claims(self.access_token).get("exp")
        if not isinstance(exp, (int, float)):
            raise AuthFileError("access_token 缺少 exp")
        return int(exp)

    @property
    def client_id(self) -> str | None:
        return jwt_claims(self.access_token).get("client_id")

    @property
    def last_refresh(self) -> int:
        value = self.raw.get("last_refresh")
        return parse_rfc3339(value) if isinstance(value, str) else 0

    def with_refreshed(
        self, *, access_token: str, refresh_token: str | None, id_token: str | None, now: int
    ) -> "AuthBundle":
        raw = copy.deepcopy(self.raw)
        tokens = raw["tokens"]
        tokens["access_token"] = access_token
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        if id_token:
            tokens["id_token"] = id_token
        raw["last_refresh"] = format_rfc3339(now)
        return AuthBundle.from_bytes(json.dumps(raw).encode())

    def to_bytes(self) -> bytes:
        return (json.dumps(self.raw, indent=2) + "\n").encode()
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_authfile.py -v`
Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/authfile.py tests/fakes.py tests/test_authfile.py
git commit -m "feat: auth.json 解析与刷新后重组"
```

---

### Task 7: 系统用户与命令执行（sysusers.py, runner.py）

**Files:**
- Create: `src/codexctl/sysusers.py`, `src/codexctl/runner.py`
- Test: `tests/test_sysusers.py`, `tests/test_runner.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_sysusers.py`:

```python
import os
import pwd

import pytest

from codexctl.sysusers import UserError, list_users, lookup_user

ME = pwd.getpwuid(os.getuid())


def test_lookup_current_user():
    u = lookup_user(ME.pw_name, min_uid=0)
    assert u.uid == os.getuid()
    assert str(u.home) == ME.pw_dir


def test_rejects_uid_below_minimum():
    with pytest.raises(UserError, match="不在允许范围内"):
        lookup_user(ME.pw_name, min_uid=ME.pw_uid + 1)


def test_rejects_bad_name():
    with pytest.raises(UserError, match="非法用户名"):
        lookup_user("../etc", min_uid=0)


def test_rejects_missing_user():
    with pytest.raises(UserError, match="不存在"):
        lookup_user("no_such_user_cx", min_uid=0)


def test_list_users_includes_current_user():
    assert ME.pw_name in {u.name for u in list_users(0)}
```

`tests/test_runner.py`:

```python
import pytest

from codexctl.runner import CommandError, run


def test_run_returns_stdout():
    assert run(["sh", "-c", "echo hi"]) == "hi\n"


def test_run_raises_with_stderr():
    with pytest.raises(CommandError, match="退出码 3：bad"):
        run(["sh", "-c", "echo bad >&2; exit 3"])


def test_run_missing_binary():
    with pytest.raises(CommandError, match="执行失败"):
        run(["/nonexistent/binary"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_sysusers.py tests/test_runner.py -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现**

`src/codexctl/sysusers.py`:

```python
from __future__ import annotations

import pwd
import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_NOBODY_UID = 65534


class UserError(ValueError):
    pass


@dataclass(frozen=True)
class SysUser:
    name: str
    uid: int
    gid: int
    home: Path


def _from_pw(pw: pwd.struct_passwd) -> SysUser:
    return SysUser(pw.pw_name, pw.pw_uid, pw.pw_gid, Path(pw.pw_dir))


def lookup_user(name: str, min_uid: int) -> SysUser:
    if not _NAME_RE.match(name):
        raise UserError(f"非法用户名：{name!r}")
    try:
        pw = pwd.getpwnam(name)
    except KeyError as e:
        raise UserError(f"系统中不存在用户：{name}") from e
    if pw.pw_uid < min_uid or pw.pw_uid == _NOBODY_UID:
        raise UserError(f"用户 {name} 的 uid={pw.pw_uid} 不在允许范围内")
    return _from_pw(pw)


def list_users(min_uid: int) -> list[SysUser]:
    users = [
        _from_pw(pw)
        for pw in pwd.getpwall()
        if pw.pw_uid >= min_uid and pw.pw_uid != _NOBODY_UID and _NAME_RE.match(pw.pw_name)
    ]
    return sorted(users, key=lambda u: u.name)
```

`src/codexctl/runner.py`:

```python
from __future__ import annotations

import subprocess
from typing import Protocol


class CommandError(RuntimeError):
    pass


class Runner(Protocol):
    def __call__(self, args: list[str], input: bytes | None = None, timeout: float = 30) -> str: ...


def run(args: list[str], input: bytes | None = None, timeout: float = 30) -> str:
    try:
        proc = subprocess.run(args, input=input, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CommandError(f"{args[0]} 执行失败：{e}") from e
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise CommandError(f"{args[0]} 退出码 {proc.returncode}：{stderr}")
    return proc.stdout.decode(errors="replace")
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_sysusers.py tests/test_runner.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/sysusers.py src/codexctl/runner.py tests/test_sysusers.py tests/test_runner.py
git commit -m "feat: 系统用户查询与命令执行"
```

---

### Task 8: 发布器（publisher.py）

这是唯一写系统文件的模块。安全要点：用户可控的 `~/.codex` 一律通过 `O_NOFOLLOW` 打开的目录 fd 操作（`*at` 系统调用），防止用户把 `~/.codex` 换成指向 `/etc` 等位置的软链接，诱使 root 在别处建文件或改属主。

**Files:**
- Create: `src/codexctl/publisher.py`
- Modify: `tests/fakes.py`（追加 `FakeRunner`）
- Test: `tests/test_publisher.py`

- [ ] **Step 1: 在 tests/fakes.py 末尾追加 FakeRunner**

```python
class FakeRunner:
    """记录命令调用；按命令名返回预设输出；设置 fail_with 时抛出该异常。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.outputs: dict[str, str] = {}
        self.fail_with: Exception | None = None

    def __call__(self, args: list[str], input: bytes | None = None, timeout: float = 30) -> str:
        self.calls.append(list(args))
        if self.fail_with is not None:
            raise self.fail_with
        return self.outputs.get(args[0], "")
```

- [ ] **Step 2: 写失败的测试**

`tests/test_publisher.py`:

```python
import os
import stat

import pytest

from codexctl.publisher import Publisher, PublishError
from codexctl.runner import CommandError
from codexctl.sysusers import SysUser
from fakes import FakeRunner


def make_user(tmp_path, name="alice"):
    home = tmp_path / "home" / name
    home.mkdir(parents=True)
    return SysUser(name, os.getuid(), os.getgid(), home)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def pub(tmp_path, runner):
    return Publisher(tmp_path / "accounts", run=runner)


def test_publish_writes_file_and_sets_acl_before_rename(tmp_path, pub, runner):
    alice = make_user(tmp_path)
    pub.publish("acc1", b'{"x":1}', [alice])
    f = tmp_path / "accounts" / "acc1" / "auth.json"
    assert f.read_bytes() == b'{"x":1}'
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o711
    assert stat.S_IMODE((tmp_path / "accounts").stat().st_mode) == 0o711
    assert runner.calls == [
        ["setfacl", "--set", "u::rw-,g::---,o::---,u:alice:r--,m::r--", str(f.parent / ".auth.json.tmp")]
    ]
    assert not (f.parent / ".auth.json.tmp").exists()


def test_publish_without_readers_uses_minimal_acl(tmp_path, pub, runner):
    pub.publish("acc1", b"{}", [])
    assert runner.calls[0][2] == "u::rw-,g::---,o::---"


def test_publish_failure_keeps_previous_file(tmp_path, pub, runner):
    pub.publish("acc1", b"old", [])
    runner.fail_with = CommandError("setfacl boom")
    with pytest.raises(CommandError):
        pub.publish("acc1", b"new", [])
    d = tmp_path / "accounts" / "acc1"
    assert (d / "auth.json").read_bytes() == b"old"
    assert not (d / ".auth.json.tmp").exists()


def test_rejects_bad_label(pub):
    with pytest.raises(PublishError, match="非法账号标签"):
        pub.account_file("../etc")


def test_set_readers_sorts_and_deduplicates(tmp_path, pub, runner):
    alice, bob = make_user(tmp_path, "alice"), make_user(tmp_path, "bob")
    pub.set_readers("acc1", [bob, alice, bob])
    assert runner.calls[0][2] == "u::rw-,g::---,o::---,u:alice:r--,u:bob:r--,m::r--"


def test_link_user_creates_codex_dir_and_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    assert pub.link_user(alice, "acc1", now=100) is None
    codex_dir = alice.home / ".codex"
    assert stat.S_IMODE(codex_dir.stat().st_mode) == 0o700
    assert os.readlink(codex_dir / "auth.json") == str(pub.account_file("acc1"))
    state = pub.read_link(alice)
    assert (state.kind, state.target) == ("symlink", str(pub.account_file("acc1")))


def test_link_user_backs_up_regular_file(tmp_path, pub):
    alice = make_user(tmp_path)
    (alice.home / ".codex").mkdir(mode=0o700)
    (alice.home / ".codex" / "auth.json").write_text("mine")
    backup = pub.link_user(alice, "acc1", now=123)
    assert backup == alice.home / ".codex" / "auth.json.bak.123"
    assert backup.read_text() == "mine"
    assert os.path.islink(alice.home / ".codex" / "auth.json")


def test_link_user_replaces_existing_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    pub.link_user(alice, "acc1", now=1)
    pub.link_user(alice, "acc2", now=2)
    assert os.readlink(alice.home / ".codex" / "auth.json") == str(pub.account_file("acc2"))


def test_link_user_refuses_symlinked_codex_dir(tmp_path, pub):
    alice = make_user(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (alice.home / ".codex").symlink_to(elsewhere)
    with pytest.raises(PublishError, match="不是普通目录"):
        pub.link_user(alice, "acc1", now=1)
    assert list(elsewhere.iterdir()) == []


def test_unlink_user_removes_only_symlink(tmp_path, pub):
    alice = make_user(tmp_path)
    pub.link_user(alice, "acc1", now=1)
    assert pub.unlink_user(alice) is True
    assert pub.read_link(alice).kind == "missing"
    (alice.home / ".codex" / "auth.json").write_text("mine")
    assert pub.unlink_user(alice) is False
    assert (alice.home / ".codex" / "auth.json").read_text() == "mine"


def test_read_link_states(tmp_path, pub):
    alice = make_user(tmp_path)
    assert pub.read_link(alice).kind == "no_codex_dir"
    (alice.home / ".codex").mkdir()
    assert pub.read_link(alice).kind == "missing"
    (alice.home / ".codex" / "auth.json").write_text("x")
    assert pub.read_link(alice).kind == "file"


def test_read_readers_parses_getfacl(pub, runner):
    runner.outputs["getfacl"] = (
        "user::rw-\nuser:alice:r--\nuser:bob:r--\t#effective:r--\ngroup::---\nmask::r--\nother::---\n"
    )
    assert pub.read_readers("acc1") == {"alice", "bob"}
    assert runner.calls[0][:3] == ["getfacl", "--omit-header", "--absolute-names"]
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_publisher.py -v`
Expected: FAIL，`No module named 'codexctl.publisher'`

- [ ] **Step 4: 实现**

`src/codexctl/publisher.py`:

```python
from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from . import runner as runner_mod
from .runner import Runner
from .sysusers import SysUser

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_LINK = "auth.json"
_LINK_TMP = ".auth.json.codexctl.tmp"
_FILE_TMP = ".auth.json.tmp"


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class LinkState:
    kind: str  # "symlink" | "file" | "missing" | "no_codex_dir" | "unsafe"
    target: str | None = None


class Publisher:
    def __init__(
        self,
        accounts_dir: Path,
        setfacl: str = "setfacl",
        getfacl: str = "getfacl",
        run: Runner = runner_mod.run,
    ) -> None:
        self.accounts_dir = accounts_dir
        self._setfacl = setfacl
        self._getfacl = getfacl
        self._run = run

    # ---- 账号凭证文件 ----

    def account_file(self, label: str) -> Path:
        if not LABEL_RE.match(label):
            raise PublishError(f"非法账号标签：{label!r}")
        return self.accounts_dir / label / "auth.json"

    def publish(self, label: str, data: bytes, readers: list[SysUser]) -> None:
        target = self.account_file(label)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.accounts_dir, 0o711)
        target.parent.mkdir(exist_ok=True)
        os.chmod(target.parent, 0o711)
        tmp = target.parent / _FILE_TMP
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self._apply_acl(tmp, readers)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def set_readers(self, label: str, readers: list[SysUser]) -> None:
        self._apply_acl(self.account_file(label), readers)

    def read_readers(self, label: str) -> set[str]:
        out = self._run([self._getfacl, "--omit-header", "--absolute-names", str(self.account_file(label))])
        names = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split()[0].split(":")
            if len(parts) == 3 and parts[0] == "user" and parts[1] and "r" in parts[2]:
                names.add(parts[1])
        return names

    def _apply_acl(self, path: Path, readers: list[SysUser]) -> None:
        entries = ["u::rw-", "g::---", "o::---"]
        names = sorted({u.name for u in readers})
        if names:
            entries += [f"u:{n}:r--" for n in names] + ["m::r--"]
        self._run([self._setfacl, "--set", ",".join(entries), str(path)])

    # ---- 用户软链接 ----

    def _open_codex_dir(self, user: SysUser, create: bool) -> int:
        home_fd = os.open(user.home, os.O_RDONLY | os.O_DIRECTORY)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            try:
                fd = os.open(".codex", flags, dir_fd=home_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(".codex", 0o700, dir_fd=home_fd)
                fd = os.open(".codex", flags, dir_fd=home_fd)
                os.fchown(fd, user.uid, user.gid)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise PublishError(f"{user.home}/.codex 不是普通目录（可能是软链接），拒绝操作") from e
                raise
        finally:
            os.close(home_fd)
        if os.fstat(fd).st_uid != user.uid:
            os.close(fd)
            raise PublishError(f"{user.home}/.codex 的属主不是 {user.name}，拒绝操作")
        return fd

    def link_user(self, user: SysUser, label: str, now: int) -> Path | None:
        """把 ~/.codex/auth.json 原子地指向账号文件；原有普通文件改名备份并返回备份路径。"""
        target = str(self.account_file(label))
        fd = self._open_codex_dir(user, create=True)
        try:
            backup_name = None
            try:
                st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and not stat.S_ISLNK(st.st_mode):
                backup_name = f"{_LINK}.bak.{now}"
                os.rename(_LINK, backup_name, src_dir_fd=fd, dst_dir_fd=fd)
            try:
                os.unlink(_LINK_TMP, dir_fd=fd)
            except FileNotFoundError:
                pass
            os.symlink(target, _LINK_TMP, dir_fd=fd)
            os.chown(_LINK_TMP, user.uid, user.gid, dir_fd=fd, follow_symlinks=False)
            # POSIX rename 原子替换目标；用 os.rename 而非 os.replace，因为前者在 os.supports_dir_fd 中有明确支持。
            os.rename(_LINK_TMP, _LINK, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)
        return user.home / ".codex" / backup_name if backup_name else None

    def unlink_user(self, user: SysUser) -> bool:
        """只删除软链接；普通文件（用户自己的）保持不动。"""
        try:
            fd = self._open_codex_dir(user, create=False)
        except FileNotFoundError:
            return False
        try:
            try:
                st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISLNK(st.st_mode):
                return False
            os.unlink(_LINK, dir_fd=fd)
            return True
        finally:
            os.close(fd)

    def read_link(self, user: SysUser) -> LinkState:
        try:
            fd = self._open_codex_dir(user, create=False)
        except FileNotFoundError:
            return LinkState("no_codex_dir")
        except PublishError as e:
            return LinkState("unsafe", str(e))
        try:
            try:
                st = os.stat(_LINK, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return LinkState("missing")
            if stat.S_ISLNK(st.st_mode):
                return LinkState("symlink", os.readlink(_LINK, dir_fd=fd))
            return LinkState("file")
        finally:
            os.close(fd)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_publisher.py -v`
Expected: `12 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/publisher.py tests/fakes.py tests/test_publisher.py
git commit -m "feat: 发布器（凭证文件、ACL、用户软链接）"
```

---

### Task 9: 数据访问、Context 与测试 fixture

**Files:**
- Create: `src/codexctl/repo.py`, `src/codexctl/context.py`, `tests/conftest.py`
- Modify: `tests/fakes.py`（追加 `FakeClock`、`FakeUsers`）
- Test: `tests/test_repo.py`

- [ ] **Step 1: 在 tests/fakes.py 末尾追加 FakeClock 与 FakeUsers**

```python
import os
from pathlib import Path

from codexctl.sysusers import SysUser, UserError


class FakeClock:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeUsers:
    """假系统用户表：所有用户的 uid/gid 都是当前进程的，home 在临时目录下。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.names: set[str] = set()

    def add(self, name: str) -> SysUser:
        (self.root / name).mkdir(parents=True, exist_ok=True)
        self.names.add(name)
        return self.get(name)

    def get(self, name: str) -> SysUser:
        return SysUser(name, os.getuid(), os.getgid(), self.root / name)

    def lookup(self, name: str, min_uid: int) -> SysUser:
        if name not in self.names:
            raise UserError(f"系统中不存在用户：{name}")
        return self.get(name)

    def list(self, min_uid: int) -> list[SysUser]:
        return [self.get(n) for n in sorted(self.names)]
```

- [ ] **Step 2: 写 repo.py（账号与密文部分）**

`src/codexctl/repo.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Account:
    id: int
    label: str
    email: str | None
    account_id: str
    plan_type: str | None
    status: str
    last_refresh: int
    access_expires_at: int
    last_error: str | None
    retry_count: int
    next_retry_at: int | None
    created_at: int


def _account(row: sqlite3.Row | None) -> Account | None:
    return Account(**dict(row)) if row is not None else None


def insert_account(
    conn: sqlite3.Connection,
    *,
    label: str,
    email: str | None,
    account_id: str,
    plan_type: str | None,
    last_refresh: int,
    access_expires_at: int,
    now: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO accounts(label, email, account_id, plan_type, last_refresh, access_expires_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (label, email, account_id, plan_type, last_refresh, access_expires_at, now),
    )
    return int(cur.lastrowid)


def get_account(conn: sqlite3.Connection, label: str) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE label = ?", (label,)).fetchone())


def get_account_by_pk(conn: sqlite3.Connection, pk: int) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE id = ?", (pk,)).fetchone())


def find_by_account_id(conn: sqlite3.Connection, account_id: str) -> Account | None:
    return _account(conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone())


def list_accounts(conn: sqlite3.Connection) -> list[Account]:
    return [Account(**dict(r)) for r in conn.execute("SELECT * FROM accounts ORDER BY label")]


def store_secret(conn: sqlite3.Connection, account_pk: int, blob: bytes, now: int) -> None:
    conn.execute(
        """
        INSERT INTO account_secrets(account_pk, blob, version, updated_at) VALUES (?, ?, 1, ?)
        ON CONFLICT(account_pk) DO UPDATE SET
            blob = excluded.blob,
            version = account_secrets.version + 1,
            updated_at = excluded.updated_at
        """,
        (account_pk, blob, now),
    )


def load_secret(conn: sqlite3.Connection, account_pk: int) -> bytes:
    row = conn.execute("SELECT blob FROM account_secrets WHERE account_pk = ?", (account_pk,)).fetchone()
    if row is None:
        raise LookupError(f"账号 {account_pk} 没有保存凭证")
    return bytes(row["blob"])


def update_token_meta(
    conn: sqlite3.Connection,
    pk: int,
    *,
    email: str | None,
    plan_type: str | None,
    last_refresh: int,
    access_expires_at: int,
) -> None:
    conn.execute(
        "UPDATE accounts SET email = ?, plan_type = ?, last_refresh = ?, access_expires_at = ?, "
        "status = 'active', last_error = NULL, retry_count = 0, next_retry_at = NULL WHERE id = ?",
        (email, plan_type, last_refresh, access_expires_at, pk),
    )


def set_status(conn: sqlite3.Connection, pk: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE accounts SET status = ?, last_error = ? WHERE id = ?", (status, error, pk))


def mark_retry(conn: sqlite3.Connection, pk: int, error: str, next_retry_at: int) -> None:
    conn.execute(
        "UPDATE accounts SET retry_count = retry_count + 1, last_error = ?, next_retry_at = ? WHERE id = ?",
        (error, next_retry_at, pk),
    )
```

- [ ] **Step 3: 写 context.py**

`src/codexctl/context.py`:

```python
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from . import db, sysusers
from .config import Settings
from .publisher import Publisher
from .sysusers import SysUser
from .vault import Vault, load_key


@dataclass
class Context:
    settings: Settings
    conn: sqlite3.Connection
    vault: Vault
    publisher: Publisher
    http: httpx.Client
    lookup_user: Callable[[str, int], SysUser] = sysusers.lookup_user
    list_users: Callable[[int], list[SysUser]] = sysusers.list_users
    clock: Callable[[], float] = time.time

    def now(self) -> int:
        return int(self.clock())


def open_context(settings: Settings) -> Context:
    return Context(
        settings=settings,
        conn=db.connect(settings.db_path),
        vault=Vault(load_key(settings.key_file)),
        publisher=Publisher(settings.accounts_dir, settings.setfacl_bin, settings.getfacl_bin),
        http=httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": "codexctl/0.1"}),
    )
```

- [ ] **Step 4: 写 tests/conftest.py**

```python
from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl import db
from codexctl.config import Settings
from codexctl.context import Context
from codexctl.publisher import Publisher
from codexctl.vault import Vault
from fakes import FakeClock, FakeRunner, FakeUsers


@pytest.fixture
def ctx(tmp_path):
    """完整的测试 Context：内存数据库、随机主密钥、假命令执行器、假用户表、假时钟、HTTP 路由表。

    测试通过以下附加属性操控替身：
      ctx.test_runner  —— FakeRunner（记录 setfacl/getfacl 调用）
      ctx.test_users   —— FakeUsers（add("alice") 创建用户）
      ctx.test_clock   —— FakeClock（修改 .t 推进时间）
      ctx.test_routes  —— {(method, url): handler(request) -> httpx.Response}
    """
    settings = Settings(
        state_dir=tmp_path / "state",
        key_file=tmp_path / "master.key",
        codex_bin=tmp_path / "codex",
    )
    settings.state_dir.mkdir()
    runner = FakeRunner()
    users = FakeUsers(tmp_path / "home")
    clock = FakeClock()
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        fn = routes.get((request.method, str(request.url)))
        return fn(request) if fn else httpx.Response(404)

    c = Context(
        settings=settings,
        conn=db.connect(":memory:"),
        vault=Vault(AESGCM.generate_key(bit_length=256)),
        publisher=Publisher(settings.accounts_dir, run=runner),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        lookup_user=users.lookup,
        list_users=users.list,
        clock=clock,
    )
    c.test_runner = runner
    c.test_users = users
    c.test_clock = clock
    c.test_routes = routes
    return c
```

- [ ] **Step 5: 写测试**

`tests/test_repo.py`:

```python
import pytest

from codexctl import repo


def _insert(ctx, label="acc1", account_id="acct-1"):
    return repo.insert_account(
        ctx.conn, label=label, email="a@example.com", account_id=account_id,
        plan_type="pro", last_refresh=1, access_expires_at=2, now=3,
    )


def test_insert_and_get(ctx):
    pk = _insert(ctx)
    acc = repo.get_account(ctx.conn, "acc1")
    assert acc.id == pk and acc.status == "active" and acc.retry_count == 0
    assert repo.get_account_by_pk(ctx.conn, pk) == acc
    assert repo.find_by_account_id(ctx.conn, "acct-1") == acc
    assert repo.get_account(ctx.conn, "nope") is None
    assert [a.label for a in repo.list_accounts(ctx.conn)] == ["acc1"]


def test_secret_versioning(ctx):
    pk = _insert(ctx)
    repo.store_secret(ctx.conn, pk, b"v1", 10)
    repo.store_secret(ctx.conn, pk, b"v2", 11)
    assert repo.load_secret(ctx.conn, pk) == b"v2"
    assert ctx.conn.execute("SELECT version FROM account_secrets").fetchone()[0] == 2


def test_load_secret_missing(ctx):
    with pytest.raises(LookupError):
        repo.load_secret(ctx.conn, 99)


def test_retry_then_token_update_resets(ctx):
    pk = _insert(ctx)
    repo.mark_retry(ctx.conn, pk, "boom", 500)
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.retry_count, acc.last_error, acc.next_retry_at) == (1, "boom", 500)
    repo.update_token_meta(ctx.conn, pk, email="b@x", plan_type="plus", last_refresh=7, access_expires_at=8)
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.retry_count, acc.last_error, acc.next_retry_at, acc.status) == (0, None, None, "active")
    assert (acc.email, acc.plan_type, acc.last_refresh, acc.access_expires_at) == ("b@x", "plus", 7, 8)


def test_set_status(ctx):
    pk = _insert(ctx)
    repo.set_status(ctx.conn, pk, "needs_relogin", "reused")
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.status, acc.last_error) == ("needs_relogin", "reused")
```

- [ ] **Step 6: 运行确认通过**

Run: `.venv/bin/pytest -v`
Expected: 全部通过（新增 `test_repo.py` 5 个）

- [ ] **Step 7: Commit**

```bash
git add src/codexctl/repo.py src/codexctl/context.py tests/conftest.py tests/fakes.py tests/test_repo.py
git commit -m "feat: 账号数据访问与运行上下文"
```

---

### Task 10: 账号服务（accounts.py）与分配查询

**Files:**
- Create: `src/codexctl/accounts.py`
- Modify: `src/codexctl/repo.py`（追加分配相关查询）
- Test: `tests/test_accounts.py`

- [ ] **Step 1: 在 repo.py 末尾追加分配查询**

```python
@dataclass(frozen=True)
class Assignment:
    id: int
    username: str
    account_pk: int
    started_at: int
    ended_at: int | None


def active_assignment(conn: sqlite3.Connection, username: str) -> Assignment | None:
    row = conn.execute(
        "SELECT * FROM assignments WHERE username = ? AND ended_at IS NULL", (username,)
    ).fetchone()
    return Assignment(**dict(row)) if row is not None else None


def all_active(conn: sqlite3.Connection) -> list[Assignment]:
    return [
        Assignment(**dict(r))
        for r in conn.execute("SELECT * FROM assignments WHERE ended_at IS NULL ORDER BY username")
    ]


def active_usernames(conn: sqlite3.Connection, account_pk: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT username FROM assignments WHERE account_pk = ? AND ended_at IS NULL ORDER BY username",
            (account_pk,),
        )
    ]


def start_assignment(conn: sqlite3.Connection, username: str, account_pk: int, now: int) -> None:
    conn.execute(
        "INSERT INTO assignments(username, account_pk, started_at) VALUES (?, ?, ?)", (username, account_pk, now)
    )


def end_assignment(conn: sqlite3.Connection, username: str, now: int) -> None:
    conn.execute("UPDATE assignments SET ended_at = ? WHERE username = ? AND ended_at IS NULL", (now, username))


def account_at(conn: sqlite3.Connection, username: str, ts: int) -> int | None:
    row = conn.execute(
        "SELECT account_pk FROM assignments WHERE username = ? AND started_at <= ? "
        "AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at DESC LIMIT 1",
        (username, ts, ts),
    ).fetchone()
    return int(row[0]) if row is not None else None


def users_with_recent_assignments(conn: sqlite3.Connection, since: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT username FROM assignments WHERE ended_at IS NULL OR ended_at >= ? ORDER BY username",
            (since,),
        )
    ]
```

- [ ] **Step 2: 写失败的测试**

`tests/test_accounts.py`:

```python
import pytest

from codexctl import accounts, events, repo
from codexctl.authfile import AuthBundle, AuthFileError
from fakes import T0, make_auth_json


def test_import_encrypts_and_publishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    assert (acc.email, acc.plan_type, acc.status) == ("a@example.com", "pro", "active")
    assert acc.last_refresh == T0 and acc.access_expires_at == T0 + 10 * 86400
    assert b"rt-1" not in repo.load_secret(ctx.conn, acc.id)
    published = ctx.publisher.account_file("acc1").read_bytes()
    assert published == accounts.load_bundle(ctx, acc).to_bytes()
    assert AuthBundle.from_bytes(published).refresh_token == "rt-1"
    audit = ctx.conn.execute("SELECT action, target FROM audit_log").fetchall()
    assert [tuple(r) for r in audit] == [("account.add", "acc1")]


def test_import_rejects_duplicate_chatgpt_account(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="已登记为 acc1"):
        accounts.import_account(ctx, "acc2", make_auth_json(), "test")


def test_import_rejects_duplicate_label(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="标签已存在"):
        accounts.import_account(ctx, "acc1", make_auth_json(account_id="acct-2"), "test")


def test_import_rejects_bad_label(ctx):
    with pytest.raises(accounts.AccountError, match="账号标签"):
        accounts.import_account(ctx, "Bad Label", make_auth_json(), "test")


def test_import_rejects_api_key_file(ctx):
    with pytest.raises(AuthFileError):
        accounts.import_account(ctx, "acc1", b'{"auth_mode":"apikey"}', "test")


def test_relogin_requires_same_account(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    with pytest.raises(accounts.AccountError, match="另一个"):
        accounts.relogin_account(ctx, "acc1", make_auth_json(account_id="acct-2"), "test")


def test_relogin_resets_status_and_republishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    repo.set_status(ctx.conn, acc.id, "needs_relogin", "reused")
    events.raise_alert(ctx.conn, "critical", "refresh_failed", "acc1", "x", now=1)
    acc = accounts.relogin_account(ctx, "acc1", make_auth_json(refresh_token="rt-9"), "test")
    assert (acc.status, acc.last_error) == ("active", None)
    assert events.open_alerts(ctx.conn) == []
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-9"


def test_set_disabled_and_enable(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "test")
    accounts.set_disabled(ctx, "acc1", True, "test")
    assert repo.get_account(ctx.conn, "acc1").status == "disabled"
    accounts.set_disabled(ctx, "acc1", False, "test")
    assert repo.get_account(ctx.conn, "acc1").status == "active"


def test_require_account_missing(ctx):
    with pytest.raises(accounts.AccountError, match="账号不存在"):
        accounts.require_account(ctx, "nope")


def test_device_login_runs_codex_with_staging_home(ctx, monkeypatch):
    script = ctx.settings.codex_bin
    script.write_text(
        '#!/bin/sh\n'
        '[ "$1 $2" = "login --device-auth" ] || exit 2\n'
        'printf "%s" "$FAKE_AUTH" > "$CODEX_HOME/auth.json"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_AUTH", make_auth_json().decode())
    data = accounts.device_login(ctx, "acc1")
    assert AuthBundle.from_bytes(data).account_id == "acct-1"
    assert list(ctx.settings.staging_dir.iterdir()) == []


def test_device_login_failure(ctx):
    script = ctx.settings.codex_bin
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    with pytest.raises(accounts.AccountError, match="退出码 1"):
        accounts.device_login(ctx, "acc1")
    assert list(ctx.settings.staging_dir.iterdir()) == []
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: FAIL，`cannot import name 'accounts'`

- [ ] **Step 4: 实现**

`src/codexctl/accounts.py`:

```python
from __future__ import annotations

import os
import shutil
import subprocess

from . import events, repo
from .authfile import AuthBundle
from .context import Context
from .db import transaction
from .publisher import LABEL_RE
from .sysusers import SysUser, UserError


class AccountError(Exception):
    pass


def _aad(label: str) -> bytes:
    return f"codexctl:{label}".encode()


def _check_label(label: str) -> None:
    if not LABEL_RE.match(label):
        raise AccountError("账号标签只能包含小写字母、数字、- 和 _，以字母或数字开头，且不超过 32 个字符")


def require_account(ctx: Context, label: str) -> repo.Account:
    acc = repo.get_account(ctx.conn, label)
    if acc is None:
        raise AccountError(f"账号不存在：{label}")
    return acc


def readers_for(ctx: Context, account_pk: int) -> list[SysUser]:
    users = []
    for name in repo.active_usernames(ctx.conn, account_pk):
        try:
            users.append(ctx.lookup_user(name, ctx.settings.min_uid))
        except UserError:
            continue
    return users


def load_bundle(ctx: Context, acc: repo.Account) -> AuthBundle:
    plaintext = ctx.vault.decrypt(repo.load_secret(ctx.conn, acc.id), _aad(acc.label))
    return AuthBundle.from_bytes(plaintext)


def publish_account(ctx: Context, acc: repo.Account, bundle: AuthBundle | None = None) -> None:
    bundle = bundle or load_bundle(ctx, acc)
    ctx.publisher.publish(acc.label, bundle.to_bytes(), readers_for(ctx, acc.id))


def import_account(ctx: Context, label: str, data: bytes, actor: str) -> repo.Account:
    _check_label(label)
    bundle = AuthBundle.from_bytes(data)
    if repo.get_account(ctx.conn, label) is not None:
        raise AccountError(f"账号标签已存在：{label}")
    dup = repo.find_by_account_id(ctx.conn, bundle.account_id)
    if dup is not None:
        raise AccountError(f"该 ChatGPT 账号已登记为 {dup.label}")
    now = ctx.now()
    with transaction(ctx.conn):
        pk = repo.insert_account(
            ctx.conn,
            label=label,
            email=bundle.email,
            account_id=bundle.account_id,
            plan_type=bundle.plan_type,
            last_refresh=bundle.last_refresh or now,
            access_expires_at=bundle.access_expires_at,
            now=now,
        )
        repo.store_secret(ctx.conn, pk, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
        events.audit(ctx.conn, actor, "account.add", label, f"email={bundle.email} plan={bundle.plan_type}", now=now)
    acc = require_account(ctx, label)
    publish_account(ctx, acc, bundle)
    return acc


def relogin_account(ctx: Context, label: str, data: bytes, actor: str) -> repo.Account:
    acc = require_account(ctx, label)
    bundle = AuthBundle.from_bytes(data)
    if bundle.account_id != acc.account_id:
        raise AccountError(f"登录的是另一个 ChatGPT 账号（{bundle.email}），与 {label} 不一致")
    now = ctx.now()
    with transaction(ctx.conn):
        repo.store_secret(ctx.conn, acc.id, ctx.vault.encrypt(bundle.to_bytes(), _aad(label)), now)
        repo.update_token_meta(
            ctx.conn,
            acc.id,
            email=bundle.email,
            plan_type=bundle.plan_type,
            last_refresh=bundle.last_refresh or now,
            access_expires_at=bundle.access_expires_at,
        )
        for kind in ("refresh_failed", "refresh_retry"):
            events.resolve_alert(ctx.conn, kind, label, now=now)
        events.audit(ctx.conn, actor, "account.relogin", label, now=now)
    acc = require_account(ctx, label)
    publish_account(ctx, acc, bundle)
    return acc


def set_disabled(ctx: Context, label: str, disabled: bool, actor: str) -> None:
    acc = require_account(ctx, label)
    now = ctx.now()
    with transaction(ctx.conn):
        repo.set_status(ctx.conn, acc.id, "disabled" if disabled else "active")
        events.audit(ctx.conn, actor, "account.disable" if disabled else "account.enable", label, now=now)


def device_login(ctx: Context, label: str) -> bytes:
    """在临时 CODEX_HOME 中运行真实 codex 的 device-auth 登录（交互：验证码打印在终端），返回生成的 auth.json。"""
    _check_label(label)
    ctx.settings.staging_dir.mkdir(parents=True, exist_ok=True)
    home = ctx.settings.staging_dir / f"{label}-{ctx.now()}"
    home.mkdir(mode=0o700)
    try:
        env = {**os.environ, "CODEX_HOME": str(home)}
        result = subprocess.run([str(ctx.settings.codex_bin), "login", "--device-auth"], env=env, check=False)
        if result.returncode != 0:
            raise AccountError(f"codex login 失败，退出码 {result.returncode}")
        auth = home / "auth.json"
        if not auth.exists():
            raise AccountError("codex login 结束但没有生成 auth.json")
        return auth.read_bytes()
    finally:
        shutil.rmtree(home, ignore_errors=True)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: `11 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/accounts.py src/codexctl/repo.py tests/test_accounts.py
git commit -m "feat: 账号导入、device-auth 登录、重新登录与停用"
```

---

### Task 11: 分配服务（assignments.py）

**Files:**
- Create: `src/codexctl/assignments.py`
- Test: `tests/test_assignments.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_assignments.py`:

```python
import os

import pytest

from codexctl import accounts, assignments, repo
from codexctl.sysusers import UserError
from fakes import make_auth_json

BASE_ACL = "u::rw-,g::---,o::---"


@pytest.fixture
def setup(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(account_id="a1"), "t")
    accounts.import_account(ctx, "acc2", make_auth_json(account_id="a2"), "t")
    ctx.test_users.add("alice")
    ctx.test_users.add("bob")
    ctx.test_runner.calls.clear()
    return ctx


def link_of(ctx, name):
    return ctx.test_users.get(name).home / ".codex" / "auth.json"


def test_assign_links_user_and_grants_acl(setup):
    ctx = setup
    assert assignments.assign(ctx, "alice", "acc1", "t") is None
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc1"))
    assert ctx.test_runner.calls == [
        ["setfacl", "--set", BASE_ACL + ",u:alice:r--,m::r--", str(ctx.publisher.account_file("acc1"))]
    ]
    assert repo.active_assignment(ctx.conn, "alice").account_pk == repo.get_account(ctx.conn, "acc1").id


def test_shared_account_lists_all_readers(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    assignments.assign(ctx, "bob", "acc1", "t")
    assert ctx.test_runner.calls[-1][2] == BASE_ACL + ",u:alice:r--,u:bob:r--,m::r--"


def test_switch_links_new_then_revokes_old(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()
    ctx.test_clock.t += 60
    assignments.assign(ctx, "alice", "acc2", "t")
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc2"))
    calls = ctx.test_runner.calls
    assert [c[-1] for c in calls] == [str(ctx.publisher.account_file("acc2")), str(ctx.publisher.account_file("acc1"))]
    assert calls[1][2] == BASE_ACL
    rows = ctx.conn.execute("SELECT ended_at FROM assignments WHERE username='alice' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [ctx.test_clock.t, None]


def test_assign_same_account_is_idempotent(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    assignments.assign(ctx, "alice", "acc1", "t")
    assert ctx.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1


def test_assign_backs_up_existing_file(setup):
    ctx = setup
    codex_dir = ctx.test_users.get("alice").home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("mine")
    backup = assignments.assign(ctx, "alice", "acc1", "t")
    assert backup.read_text() == "mine"


def test_unassign_removes_link_and_acl(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.calls.clear()
    assert assignments.unassign(ctx, "alice", "t") is True
    assert not os.path.lexists(link_of(ctx, "alice"))
    assert ctx.test_runner.calls == [["setfacl", "--set", BASE_ACL, str(ctx.publisher.account_file("acc1"))]]
    assert repo.active_assignment(ctx.conn, "alice") is None
    assert assignments.unassign(ctx, "alice", "t") is False


def test_assign_rejects_disabled_account(setup):
    ctx = setup
    accounts.set_disabled(ctx, "acc1", True, "t")
    with pytest.raises(assignments.AssignError, match="已停用"):
        assignments.assign(ctx, "alice", "acc1", "t")


def test_assign_rejects_unknown_account(setup):
    with pytest.raises(assignments.AssignError, match="账号不存在"):
        assignments.assign(setup, "alice", "nope", "t")


def test_assign_rejects_unknown_user(setup):
    with pytest.raises(UserError):
        assignments.assign(setup, "mallory", "acc1", "t")


def test_reconcile_restores_deleted_link(setup):
    ctx = setup
    assignments.assign(ctx, "alice", "acc1", "t")
    link_of(ctx, "alice").unlink()
    assignments.reconcile_user(ctx, "alice", "t")
    assert os.readlink(link_of(ctx, "alice")) == str(ctx.publisher.account_file("acc1"))


def test_reconcile_removes_stale_link(setup):
    ctx = setup
    ctx.publisher.link_user(ctx.test_users.get("bob"), "acc1", now=1)
    assignments.reconcile_user(ctx, "bob", "t")
    assert not os.path.lexists(link_of(ctx, "bob"))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_assignments.py -v`
Expected: FAIL，`cannot import name 'assignments'`

- [ ] **Step 3: 实现**

`src/codexctl/assignments.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

from . import accounts, events, repo
from .context import Context
from .db import transaction
from .sysusers import UserError


class AssignError(Exception):
    pass


def _sync_acl(ctx: Context, account_pk: int) -> None:
    acc = repo.get_account_by_pk(ctx.conn, account_pk)
    ctx.publisher.set_readers(acc.label, accounts.readers_for(ctx, account_pk))


def assign(ctx: Context, username: str, label: str, actor: str) -> Path | None:
    """分配或切换。顺序：写数据库 → 给新账号加读权限 → 改链接 → 撤销旧账号读权限。返回备份文件路径（如有）。"""
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    acc = repo.get_account(ctx.conn, label)
    if acc is None:
        raise AssignError(f"账号不存在：{label}")
    if acc.status == "disabled":
        raise AssignError(f"账号 {label} 已停用，不能分配")
    current = repo.active_assignment(ctx.conn, username)
    switching = current is not None and current.account_pk != acc.id
    now = ctx.now()
    if current is None or switching:
        with transaction(ctx.conn):
            old_label = "-"
            if current is not None:
                old_label = repo.get_account_by_pk(ctx.conn, current.account_pk).label
                repo.end_assignment(ctx.conn, username, now)
            repo.start_assignment(ctx.conn, username, acc.id, now)
            events.audit(ctx.conn, actor, "assign", username, f"{old_label} -> {label}", now=now)
    _sync_acl(ctx, acc.id)
    backup = ctx.publisher.link_user(user, label, now)
    if switching:
        _sync_acl(ctx, current.account_pk)
    return backup


def unassign(ctx: Context, username: str, actor: str) -> bool:
    """回收：删除软链接并撤销读权限。绝不调用 codex logout（会吊销同账号所有用户共用的 token）。"""
    current = repo.active_assignment(ctx.conn, username)
    if current is None:
        return False
    now = ctx.now()
    with transaction(ctx.conn):
        repo.end_assignment(ctx.conn, username, now)
        label = repo.get_account_by_pk(ctx.conn, current.account_pk).label
        events.audit(ctx.conn, actor, "unassign", username, label, now=now)
    try:
        user = ctx.lookup_user(username, ctx.settings.min_uid)
    except UserError:
        user = None
    if user is not None:
        ctx.publisher.unlink_user(user)
    _sync_acl(ctx, current.account_pk)
    return True


def reconcile_user(ctx: Context, username: str, actor: str) -> None:
    """按数据库记录修复用户的链接与对应账号的 ACL（管理员的"一键修复"）。"""
    user = ctx.lookup_user(username, ctx.settings.min_uid)
    current = repo.active_assignment(ctx.conn, username)
    now = ctx.now()
    if current is not None:
        acc = repo.get_account_by_pk(ctx.conn, current.account_pk)
        _sync_acl(ctx, acc.id)
        ctx.publisher.link_user(user, acc.label, now)
    else:
        state = ctx.publisher.read_link(user)
        prefix = str(ctx.publisher.accounts_dir) + os.sep
        if state.kind == "symlink" and (state.target or "").startswith(prefix):
            ctx.publisher.unlink_user(user)
    with transaction(ctx.conn):
        events.audit(ctx.conn, actor, "fix", username, now=now)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_assignments.py -v`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/assignments.py tests/test_assignments.py
git commit -m "feat: 分配、切换、回收与按记录修复"
```

---

### Task 12: token 刷新（oauth.py, refresher.py）

**Files:**
- Create: `src/codexctl/oauth.py`, `src/codexctl/refresher.py`
- Test: `tests/test_refresher.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_refresher.py`:

```python
import dataclasses
import json

import httpx
import pytest

from codexctl import accounts, events, refresher, repo
from codexctl.authfile import AuthBundle, format_rfc3339
from fakes import T0, make_auth_json, make_jwt

TOKEN_URL = "https://auth.openai.com/oauth/token"


def ok_body(ctx, refresh_token="rt-2"):
    return {
        "access_token": make_jwt({"client_id": "app_test", "exp": ctx.test_clock.t + 10 * 86400}),
        "refresh_token": refresh_token,
        "id_token": make_jwt({
            "email": "a@example.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "pro"},
        }),
    }


def test_needs_refresh_rules(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    s = ctx.settings
    assert not refresher.needs_refresh(acc, T0 + 86400, s)
    assert refresher.needs_refresh(acc, T0 + 4 * 86400, s)
    assert refresher.needs_refresh(dataclasses.replace(acc, access_expires_at=T0 + 2 * 86400), T0 + 3600, s)
    assert not refresher.needs_refresh(dataclasses.replace(acc, status="disabled"), T0 + 5 * 86400, s)
    assert not refresher.needs_refresh(dataclasses.replace(acc, status="needs_relogin"), T0 + 5 * 86400, s)
    assert not refresher.needs_refresh(
        dataclasses.replace(acc, next_retry_at=T0 + 5 * 86400 + 10), T0 + 5 * 86400, s
    )


def test_refresh_success_rotates_and_publishes(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_body(ctx))

    ctx.test_routes[("POST", TOKEN_URL)] = handler
    ctx.test_clock.t += 5 * 86400
    assert refresher.refresh_account(ctx, acc, "t") is True
    assert seen == {
        "client_id": "app_test", "grant_type": "refresh_token",
        "refresh_token": "rt-1", "scope": "openid profile email",
    }
    updated = repo.get_account(ctx.conn, "acc1")
    assert updated.last_refresh == ctx.test_clock.t
    assert updated.access_expires_at == ctx.test_clock.t + 10 * 86400
    assert accounts.load_bundle(ctx, updated).refresh_token == "rt-2"
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-2"
    assert published.last_refresh == ctx.test_clock.t


def test_refresh_server_error_schedules_backoff(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(503)
    ctx.test_clock.t += 5 * 86400
    assert refresher.refresh_account(ctx, acc, "t") is False
    acc = repo.get_account(ctx.conn, "acc1")
    assert (acc.status, acc.retry_count, acc.next_retry_at) == ("active", 1, ctx.test_clock.t + 60)
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["refresh_retry"]
    ctx.test_clock.t = acc.next_retry_at
    refresher.refresh_account(ctx, acc, "t")
    assert repo.get_account(ctx.conn, "acc1").next_retry_at == ctx.test_clock.t + 300


def test_refresh_reused_token_needs_relogin(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(
        401, json={"error": {"code": "refresh_token_reused", "message": "already used"}}
    )
    assert refresher.refresh_account(ctx, acc, "t") is False
    acc = repo.get_account(ctx.conn, "acc1")
    assert acc.status == "needs_relogin"
    assert "refresh_token_reused" in acc.last_error
    alerts = events.open_alerts(ctx.conn)
    assert [(a["kind"], a["level"]) for a in alerts] == [("refresh_failed", "critical")]
    published = AuthBundle.from_bytes(ctx.publisher.account_file("acc1").read_bytes())
    assert published.refresh_token == "rt-1"


def test_refresh_success_clears_previous_alerts(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(503)
    refresher.refresh_account(ctx, acc, "t")
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(200, json=ok_body(ctx))
    assert refresher.refresh_account(ctx, repo.get_account(ctx.conn, "acc1"), "t") is True
    assert events.open_alerts(ctx.conn) == []
    assert repo.get_account(ctx.conn, "acc1").retry_count == 0


def test_refresh_due_only_refreshes_due_accounts(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.import_account(
        ctx, "acc2", make_auth_json(account_id="a2", last_refresh=format_rfc3339(T0 - 5 * 86400)), "t"
    )
    ctx.test_routes[("POST", TOKEN_URL)] = lambda r: httpx.Response(200, json=ok_body(ctx))
    assert refresher.refresh_due(ctx) == ["acc2"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_refresher.py -v`
Expected: FAIL，`cannot import name 'refresher'`

- [ ] **Step 3: 实现 oauth.py**

`src/codexctl/oauth.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

import httpx


class RefreshError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class RefreshResult:
    access_token: str
    refresh_token: str | None
    id_token: str | None


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("code") or err.get("message") or err)
    return str(err or body)[:200]


def refresh_tokens(http: httpx.Client, token_url: str, client_id: str, refresh_token: str) -> RefreshResult:
    """与 Codex CLI 自身的刷新请求一致：POST JSON 到 auth.openai.com/oauth/token。"""
    try:
        resp = http.post(
            token_url,
            json={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
        )
    except httpx.HTTPError as e:
        raise RefreshError(f"网络错误：{e}", retryable=True) from e
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RefreshError(f"服务端暂时不可用（HTTP {resp.status_code}）", retryable=True)
    if resp.status_code != 200:
        raise RefreshError(f"刷新被拒绝（HTTP {resp.status_code}）：{_error_code(resp)}", retryable=False)
    try:
        body = resp.json()
    except ValueError as e:
        raise RefreshError("刷新响应不是 JSON", retryable=False) from e
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise RefreshError("刷新响应缺少 access_token", retryable=False)
    return RefreshResult(access, body.get("refresh_token") or None, body.get("id_token") or None)
```

说明：200 但响应异常时标记为不可重试。此时服务端可能已经轮换了 refresh token，再用旧 token 重试只会触发 `refresh_token_reused`，应交给管理员重新登录。

- [ ] **Step 4: 实现 refresher.py**

`src/codexctl/refresher.py`:

```python
from __future__ import annotations

import logging

from . import accounts, events, oauth, repo
from .config import Settings
from .context import Context
from .db import transaction

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (60, 300, 900, 3600)


def needs_refresh(acc: repo.Account, now: int, settings: Settings) -> bool:
    if acc.status != "active":
        return False
    if acc.next_retry_at is not None and now < acc.next_retry_at:
        return False
    if now - acc.last_refresh >= settings.refresh_interval_days * 86400:
        return True
    return acc.access_expires_at - now < settings.refresh_min_validity_days * 86400


def refresh_account(ctx: Context, acc: repo.Account, actor: str = "scheduler") -> bool:
    now = ctx.now()
    bundle = accounts.load_bundle(ctx, acc)
    client_id = bundle.client_id or ctx.settings.oauth_client_id
    try:
        result = oauth.refresh_tokens(ctx.http, ctx.settings.oauth_token_url, client_id, bundle.refresh_token)
    except oauth.RefreshError as e:
        with transaction(ctx.conn):
            if e.retryable:
                delay = BACKOFF_SECONDS[min(acc.retry_count, len(BACKOFF_SECONDS) - 1)]
                repo.mark_retry(ctx.conn, acc.id, str(e), now + delay)
                events.raise_alert(
                    ctx.conn, "warning", "refresh_retry", acc.label, f"刷新失败，{delay} 秒后重试：{e}", now=now
                )
            else:
                repo.set_status(ctx.conn, acc.id, "needs_relogin", str(e))
                events.raise_alert(
                    ctx.conn, "critical", "refresh_failed", acc.label, f"刷新被拒绝，需要重新登录：{e}", now=now
                )
            events.audit(ctx.conn, actor, "account.refresh_failed", acc.label, str(e), now=now)
        log.warning("刷新 %s 失败：%s", acc.label, e)
        return False
    new = bundle.with_refreshed(
        access_token=result.access_token, refresh_token=result.refresh_token, id_token=result.id_token, now=now
    )
    # 先把新凭证持久化到保险库，再发布文件：服务端已轮换 token，保险库必须是权威来源。
    with transaction(ctx.conn):
        repo.store_secret(ctx.conn, acc.id, ctx.vault.encrypt(new.to_bytes(), accounts._aad(acc.label)), now)
        repo.update_token_meta(
            ctx.conn,
            acc.id,
            email=new.email,
            plan_type=new.plan_type,
            last_refresh=now,
            access_expires_at=new.access_expires_at,
        )
        for kind in ("refresh_retry", "refresh_failed"):
            events.resolve_alert(ctx.conn, kind, acc.label, now=now)
        events.audit(ctx.conn, actor, "account.refresh", acc.label, now=now)
    accounts.publish_account(ctx, repo.get_account(ctx.conn, acc.label), new)
    return True


def refresh_due(ctx: Context) -> list[str]:
    now = ctx.now()
    refreshed = []
    for acc in repo.list_accounts(ctx.conn):
        if needs_refresh(acc, now, ctx.settings) and refresh_account(ctx, acc):
            refreshed.append(acc.label)
    return refreshed
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_refresher.py -v`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/oauth.py src/codexctl/refresher.py tests/test_refresher.py
git commit -m "feat: 集中刷新 token（退避重试、不可恢复时要求重新登录）"
```

---

### Task 13: 额度监控（quota.py）

如果 Task 0 Step 4 记录的响应结构与下方 `BODY` 不同，先按实际结构修改测试里的 `BODY` 和 `parse_usage`。

**Files:**
- Create: `src/codexctl/quota.py`
- Modify: `src/codexctl/repo.py`（追加额度查询）
- Test: `tests/test_quota.py`

- [ ] **Step 1: 在 repo.py 末尾追加额度查询**

```python
def insert_quota_sample(
    conn: sqlite3.Connection,
    account_pk: int,
    ts: int,
    win: str,
    used_percent: float,
    window_minutes: int | None,
    resets_at: int | None,
    source: str,
) -> None:
    conn.execute(
        "INSERT INTO quota_samples(account_pk, ts, win, used_percent, window_minutes, resets_at, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_pk, ts, win, used_percent, window_minutes, resets_at, source),
    )


def latest_quota(conn: sqlite3.Connection, account_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT q.* FROM quota_samples q
        JOIN (SELECT win, MAX(ts) AS ts FROM quota_samples WHERE account_pk = ? GROUP BY win) m
          ON q.win = m.win AND q.ts = m.ts
        WHERE q.account_pk = ?
        GROUP BY q.win
        ORDER BY q.win
        """,
        (account_pk, account_pk),
    ).fetchall()
```

- [ ] **Step 2: 写失败的测试**

`tests/test_quota.py`:

```python
import httpx

from codexctl import accounts, events, quota, repo
from codexctl.quota import QuotaWindow
from fakes import T0, make_auth_json

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
BODY = {
    "plan_type": "pro",
    "rate_limit": {
        "primary_window": {
            "used_percent": 42, "limit_window_seconds": 18000,
            "reset_after_seconds": 600, "reset_at": T0 + 600,
        },
        "secondary_window": {
            "used_percent": 96, "limit_window_seconds": 604800, "reset_after_seconds": 86400,
        },
    },
}


def test_parse_usage():
    assert quota.parse_usage(BODY, now=T0) == [
        QuotaWindow("primary", 42.0, 300, T0 + 600),
        QuotaWindow("secondary", 96.0, 10080, T0 + 86400),
    ]


def test_parse_usage_tolerates_missing_parts():
    assert quota.parse_usage({}, now=0) == []
    assert quota.parse_usage({"rate_limit": {"primary_window": None}}, now=0) == []


def test_windows_from_session_log_rate_limits():
    rl = {"primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1794920554}, "secondary": None}
    assert quota.windows_from_rate_limits(rl) == [QuotaWindow("primary", 17.0, 10080, 1794920554)]


def test_poll_quotas_records_and_raises_critical_alert(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["account"] = request.headers["ChatGPT-Account-Id"]
        return httpx.Response(200, json=BODY)

    ctx.test_routes[("GET", USAGE_URL)] = handler
    quota.poll_quotas(ctx)
    assert seen["auth"].startswith("Bearer ") and seen["account"] == "acct-1"
    rows = repo.latest_quota(ctx.conn, acc.id)
    assert {r["win"]: (r["used_percent"], r["source"]) for r in rows} == {
        "primary": (42.0, "api"), "secondary": (96.0, "api"),
    }
    assert [(a["kind"], a["level"]) for a in events.open_alerts(ctx.conn)] == [("quota_high", "critical")]


def test_poll_quotas_survives_http_error(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: httpx.Response(500)
    quota.poll_quotas(ctx)
    assert repo.latest_quota(ctx.conn, acc.id) == []


def test_poll_quotas_skips_disabled_accounts(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    accounts.set_disabled(ctx, "acc1", True, "t")
    called = []
    ctx.test_routes[("GET", USAGE_URL)] = lambda r: called.append(1) or httpx.Response(200, json=BODY)
    quota.poll_quotas(ctx)
    assert called == []


def test_threshold_levels_and_resolution(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    quota.record(ctx, acc.id, T0, [QuotaWindow("primary", 85.0, 300, None)], "api")
    quota.evaluate_thresholds(ctx, acc)
    assert [a["level"] for a in events.open_alerts(ctx.conn)] == ["warning"]
    quota.record(ctx, acc.id, T0 + 60, [QuotaWindow("primary", 10.0, 300, None)], "api")
    quota.evaluate_thresholds(ctx, acc)
    assert events.open_alerts(ctx.conn) == []
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_quota.py -v`
Expected: FAIL，`cannot import name 'quota'`

- [ ] **Step 4: 实现**

`src/codexctl/quota.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from . import accounts, events, repo
from .context import Context

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaWindow:
    win: str  # "primary" | "secondary"；时长看 window_minutes，不要假设哪个是 5 小时
    used_percent: float
    window_minutes: int | None
    resets_at: int | None


def parse_usage(body: dict, now: int) -> list[QuotaWindow]:
    """解析 ChatGPT 后端 /wham/usage 响应（非官方接口，格式见 docs/server-validation.md）。"""
    rate_limit = body.get("rate_limit") if isinstance(body, dict) else None
    if not isinstance(rate_limit, dict):
        return []
    out = []
    for win, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = rate_limit.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        seconds = w.get("limit_window_seconds")
        resets = w.get("reset_at")
        if resets is None and w.get("reset_after_seconds") is not None:
            resets = now + int(w["reset_after_seconds"])
        out.append(
            QuotaWindow(
                win,
                float(w["used_percent"]),
                int(seconds) // 60 if seconds else None,
                int(resets) if resets is not None else None,
            )
        )
    return out


def windows_from_rate_limits(rate_limits: dict) -> list[QuotaWindow]:
    """解析会话日志 token_count 事件里的 rate_limits（降级来源）。"""
    out = []
    for win in ("primary", "secondary"):
        w = rate_limits.get(win)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        minutes = w.get("window_minutes")
        resets = w.get("resets_at")
        out.append(
            QuotaWindow(
                win,
                float(w["used_percent"]),
                int(minutes) if minutes is not None else None,
                int(resets) if resets is not None else None,
            )
        )
    return out


def record(ctx: Context, account_pk: int, ts: int, windows: list[QuotaWindow], source: str) -> None:
    for w in windows:
        repo.insert_quota_sample(ctx.conn, account_pk, ts, w.win, w.used_percent, w.window_minutes, w.resets_at, source)


def evaluate_thresholds(ctx: Context, acc: repo.Account) -> None:
    rows = repo.latest_quota(ctx.conn, acc.id)
    if not rows:
        return
    now = ctx.now()
    worst = max(r["used_percent"] for r in rows)
    if worst >= ctx.settings.quota_crit_percent:
        events.raise_alert(ctx.conn, "critical", "quota_high", acc.label, f"额度已用 {worst:.0f}%", now=now)
    elif worst >= ctx.settings.quota_warn_percent:
        events.raise_alert(ctx.conn, "warning", "quota_high", acc.label, f"额度已用 {worst:.0f}%", now=now)
    else:
        events.resolve_alert(ctx.conn, "quota_high", acc.label, now=now)


def fetch_usage(ctx: Context, acc: repo.Account) -> list[QuotaWindow]:
    bundle = accounts.load_bundle(ctx, acc)
    resp = ctx.http.get(
        ctx.settings.usage_url,
        headers={"Authorization": f"Bearer {bundle.access_token}", "ChatGPT-Account-Id": bundle.account_id},
    )
    resp.raise_for_status()
    return parse_usage(resp.json(), ctx.now())


def poll_quotas(ctx: Context) -> None:
    for acc in repo.list_accounts(ctx.conn):
        if acc.status != "active":
            continue
        try:
            windows = fetch_usage(ctx, acc)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("额度接口请求失败 %s：%s", acc.label, e)
            continue
        record(ctx, acc.id, ctx.now(), windows, "api")
        evaluate_thresholds(ctx, acc)
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_quota.py -v`
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/quota.py src/codexctl/repo.py tests/test_quota.py
git commit -m "feat: 账号额度轮询与阈值告警"
```

---

### Task 14: 会话日志用量（logscan.py）

要点：

- 用 `info.total_token_usage` 的**累计值求差**，不直接累加 `last_token_usage`，避免同一回合出现多条 `token_count` 事件时重复计数。
- `info` 为空的事件（只更新额度）不改变累计基线。

**Files:**
- Create: `src/codexctl/logscan.py`
- Modify: `src/codexctl/repo.py`（追加用量与偏移）
- Test: `tests/test_logscan.py`

- [ ] **Step 1: 在 repo.py 末尾追加用量与偏移**

```python
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def insert_usage(
    conn: sqlite3.Connection, username: str, account_pk: int | None, ts: int, delta: dict[str, int], session_file: str
) -> None:
    conn.execute(
        "INSERT INTO usage_events(username, account_pk, ts, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens, session_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (username, account_pk, ts, *(delta[f] for f in USAGE_FIELDS), session_file),
    )


def get_offset(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM log_offsets WHERE path = ?", (path,)).fetchone()


def set_offset(conn: sqlite3.Connection, path: str, inode: int, offset: int, last_totals: str | None) -> None:
    conn.execute(
        """
        INSERT INTO log_offsets(path, inode, offset, last_totals) VALUES (?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET inode = excluded.inode, offset = excluded.offset,
            last_totals = excluded.last_totals
        """,
        (path, inode, offset, last_totals),
    )


def usage_summary(conn: sqlite3.Connection, since: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT u.username, a.label, SUM(u.total_tokens) AS total, SUM(u.input_tokens) AS input,
               SUM(u.output_tokens) AS output, COUNT(*) AS turns
        FROM usage_events u LEFT JOIN accounts a ON a.id = u.account_pk
        WHERE u.ts >= ?
        GROUP BY u.username, a.label
        ORDER BY total DESC
        """,
        (since,),
    ).fetchall()
```

- [ ] **Step 2: 写失败的测试**

`tests/test_logscan.py`:

```python
import json
import os

import pytest

from codexctl import accounts, logscan, repo
from codexctl.authfile import format_rfc3339
from fakes import T0, make_auth_json


def event(ts, total, *, info=True, rate=None):
    payload = {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": total - 10, "cached_input_tokens": 0, "output_tokens": 10,
                "reasoning_output_tokens": 0, "total_tokens": total,
            }
        } if info else None,
    }
    if rate is not None:
        payload["rate_limits"] = rate
    return json.dumps({"timestamp": format_rfc3339(ts), "type": "event_msg", "payload": payload}) + "\n"


@pytest.fixture
def setup(ctx):
    acc = accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    alice = ctx.test_users.add("alice")
    repo.start_assignment(ctx.conn, "alice", acc.id, T0)
    ctx.test_clock.t = T0 + 3600
    path = alice.home / ".codex" / "sessions" / "2027" / "01" / "15" / "rollout-x.jsonl"
    path.parent.mkdir(parents=True)
    return ctx, acc, path


def write(path, text, mode="a", mtime=T0 + 3600):
    with open(path, mode) as f:
        f.write(text)
    os.utime(path, (mtime, mtime))


def usage_rows(ctx):
    return [tuple(r) for r in ctx.conn.execute(
        "SELECT ts, account_pk, input_tokens, output_tokens, total_tokens FROM usage_events ORDER BY id")]


def test_parse_line_filters_and_parses():
    assert logscan.parse_line("not json") is None
    assert logscan.parse_line(json.dumps({"type": "response_item", "payload": {}})) is None
    tc = logscan.parse_line(event(T0, 100))
    assert tc.ts == T0 and tc.totals["total_tokens"] == 100 and tc.rate_limits is None
    assert logscan.parse_line(event(T0, 0, info=False)).totals is None


def test_scan_counts_deltas_and_is_idempotent(setup):
    ctx, acc, path = setup
    write(path, event(T0 + 10, 100) + event(T0 + 20, 250))
    assert logscan.scan_file(ctx, "alice", path) == 2
    assert usage_rows(ctx) == [(T0 + 10, acc.id, 90, 10, 100), (T0 + 20, acc.id, 150, 0, 150)]
    assert logscan.scan_file(ctx, "alice", path) == 0
    write(path, event(T0 + 30, 300))
    assert logscan.scan_file(ctx, "alice", path) == 1
    assert usage_rows(ctx)[-1] == (T0 + 30, acc.id, 50, 0, 50)


def test_partial_line_waits_for_newline(setup):
    ctx, _, path = setup
    line = event(T0 + 10, 100)
    write(path, line[:20])
    assert logscan.scan_file(ctx, "alice", path) == 0
    write(path, line[20:])
    assert logscan.scan_file(ctx, "alice", path) == 1


def test_rate_limit_only_event_keeps_baseline_and_records_quota(setup):
    ctx, acc, path = setup
    rate = {"primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": T0 + 9999}, "secondary": None}
    write(path, event(T0 + 10, 100) + event(T0 + 15, 0, info=False, rate=rate) + event(T0 + 20, 250))
    logscan.scan_file(ctx, "alice", path)
    assert [r[4] for r in usage_rows(ctx)] == [100, 150]
    rows = repo.latest_quota(ctx.conn, acc.id)
    assert [(r["win"], r["used_percent"], r["source"]) for r in rows] == [("primary", 17.0, "log")]


def test_truncated_file_is_rescanned_from_start(setup):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100) + event(T0 + 20, 250))
    logscan.scan_file(ctx, "alice", path)
    write(path, event(T0 + 30, 40), mode="w")
    assert logscan.scan_file(ctx, "alice", path) == 1
    assert usage_rows(ctx)[-1][4] == 40


def test_event_before_assignment_has_no_account(setup):
    ctx, _, path = setup
    write(path, event(T0 - 100, 100))
    logscan.scan_file(ctx, "alice", path)
    assert usage_rows(ctx)[0][1] is None


def test_session_files_filters_by_mtime_and_name(setup):
    ctx, _, path = setup
    write(path, "")
    old = path.parent / "rollout-old.jsonl"
    write(old, "", mtime=T0 - 30 * 86400)
    (path.parent / "notes.txt").write_text("x")
    home = ctx.test_users.get("alice").home
    assert logscan.session_files(home, since=T0 - 86400) == [path]


def test_scan_all_only_scans_assigned_users(setup):
    ctx, _, path = setup
    write(path, event(T0 + 10, 100))
    bob = ctx.test_users.add("bob")
    bob_log = bob.home / ".codex" / "sessions" / "rollout-b.jsonl"
    bob_log.parent.mkdir(parents=True)
    write(bob_log, event(T0 + 10, 100))
    assert logscan.scan_all(ctx) == 1
    assert repo.usage_summary(ctx.conn, since=0)[0]["username"] == "alice"
```

- [ ] **Step 3: 运行确认失败**

Run: `.venv/bin/pytest tests/test_logscan.py -v`
Expected: FAIL，`cannot import name 'logscan'`

- [ ] **Step 4: 实现**

`src/codexctl/logscan.py`:

```python
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import quota, repo
from .authfile import parse_rfc3339
from .context import Context
from .db import transaction
from .repo import USAGE_FIELDS
from .sysusers import UserError


@dataclass(frozen=True)
class TokenCount:
    ts: int
    totals: dict[str, int] | None  # None 表示该事件不含用量（只更新额度）
    rate_limits: dict | None


def parse_line(line: str) -> TokenCount | None:
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "event_msg":
        return None
    payload = d.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    stamp = d.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        ts = parse_rfc3339(stamp)
    except ValueError:
        return None
    info = payload.get("info")
    usage = info.get("total_token_usage") if isinstance(info, dict) else None
    totals = {f: int(usage.get(f) or 0) for f in USAGE_FIELDS} if isinstance(usage, dict) else None
    rate_limits = payload.get("rate_limits")
    return TokenCount(ts, totals, rate_limits if isinstance(rate_limits, dict) else None)


def usage_delta(prev: dict[str, int] | None, cur: dict[str, int]) -> dict[str, int]:
    if prev is None or cur["total_tokens"] < prev.get("total_tokens", 0):
        return dict(cur)
    return {f: max(cur[f] - prev.get(f, 0), 0) for f in USAGE_FIELDS}


def scan_file(ctx: Context, username: str, path: Path) -> int:
    """从上次位置继续解析一个会话文件，返回写入的用量事件数。只消费以换行结尾的完整行。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return 0
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return 0
        row = repo.get_offset(ctx.conn, str(path))
        offset, prev = 0, None
        if row is not None and row["inode"] == st.st_ino and row["offset"] <= st.st_size:
            offset = row["offset"]
            prev = json.loads(row["last_totals"]) if row["last_totals"] else None
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return 0
    count = 0
    with transaction(ctx.conn):
        for raw in chunk[: end + 1].splitlines():
            tc = parse_line(raw.decode("utf-8", errors="replace"))
            if tc is None:
                continue
            account_pk = repo.account_at(ctx.conn, username, tc.ts)
            if tc.totals is not None:
                delta = usage_delta(prev, tc.totals)
                prev = tc.totals
                if delta["total_tokens"] > 0:
                    repo.insert_usage(ctx.conn, username, account_pk, tc.ts, delta, str(path))
                    count += 1
            if tc.rate_limits and account_pk is not None:
                for w in quota.windows_from_rate_limits(tc.rate_limits):
                    repo.insert_quota_sample(
                        ctx.conn, account_pk, tc.ts, w.win, w.used_percent, w.window_minutes, w.resets_at, "log"
                    )
        repo.set_offset(
            ctx.conn, str(path), st.st_ino, offset + end + 1, json.dumps(prev) if prev is not None else None
        )
    return count


def session_files(home: Path, since: int) -> list[Path]:
    base = home / ".codex" / "sessions"
    found = []
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for name in filenames:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            p = Path(dirpath) / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mtime >= since:
                found.append(p)
    return sorted(found)


def scan_all(ctx: Context) -> int:
    since = ctx.now() - ctx.settings.log_lookback_days * 86400
    total = 0
    for name in repo.users_with_recent_assignments(ctx.conn, since):
        try:
            user = ctx.lookup_user(name, ctx.settings.min_uid)
        except UserError:
            continue
        for path in session_files(user.home, since):
            total += scan_file(ctx, name, path)
    for acc in repo.list_accounts(ctx.conn):
        quota.evaluate_thresholds(ctx, acc)
    return total
```

- [ ] **Step 5: 运行确认通过**

Run: `.venv/bin/pytest tests/test_logscan.py -v`
Expected: `8 passed`

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/logscan.py src/codexctl/repo.py tests/test_logscan.py
git commit -m "feat: 会话日志增量解析与按用户用量归属"
```

---

### Task 15: 巡检（watcher.py）

**Files:**
- Create: `src/codexctl/watcher.py`
- Test: `tests/test_watcher.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_watcher.py`:

```python
import pytest

from codexctl import accounts, assignments, events, repo, watcher
from fakes import make_auth_json

ACL_ALICE = "user::rw-\nuser:alice:r--\ngroup::---\nmask::r--\nother::---\n"


@pytest.fixture
def setup(ctx):
    accounts.import_account(ctx, "acc1", make_auth_json(), "t")
    ctx.test_users.add("alice")
    ctx.test_users.add("bob")
    assignments.assign(ctx, "alice", "acc1", "t")
    ctx.test_runner.outputs["getfacl"] = ACL_ALICE
    return ctx


def kinds(findings):
    return [(f.kind, f.target) for f in findings]


def test_clean_state_has_no_findings(setup):
    assert watcher.run_checks(setup) == []


def test_deleted_link_detected_and_resolved_after_fix(setup):
    ctx = setup
    (ctx.test_users.get("alice").home / ".codex" / "auth.json").unlink()
    assert kinds(watcher.run_checks(ctx)) == [("link_mismatch", "alice")]
    assert [a["kind"] for a in events.open_alerts(ctx.conn)] == ["link_mismatch"]
    assignments.reconcile_user(ctx, "alice", "t")
    assert watcher.run_checks(ctx) == []
    assert events.open_alerts(ctx.conn) == []


def test_link_replaced_by_regular_file(setup):
    ctx = setup
    link = ctx.test_users.get("alice").home / ".codex" / "auth.json"
    link.unlink()
    link.write_text("{}")
    assert kinds(watcher.run_checks(ctx)) == [("link_mismatch", "alice")]


def test_stale_link_for_unassigned_user(setup):
    ctx = setup
    ctx.publisher.link_user(ctx.test_users.get("bob"), "acc1", now=1)
    assert kinds(watcher.run_checks(ctx)) == [("stale_link", "bob")]


def test_acl_mismatch(setup):
    ctx = setup
    ctx.test_runner.outputs["getfacl"] = "user::rw-\ngroup::---\nother::---\n"
    assert kinds(watcher.run_checks(ctx)) == [("acl_mismatch", "acc1")]


def test_published_file_mismatch(setup):
    ctx = setup
    ctx.publisher.account_file("acc1").write_bytes(b"{}")
    assert kinds(watcher.run_checks(ctx)) == [("published_mismatch", "acc1")]


def test_token_expiring(setup):
    ctx = setup
    acc = repo.get_account(ctx.conn, "acc1")
    ctx.test_clock.t = acc.access_expires_at - 3600
    findings = watcher.run_checks(ctx)
    assert [(f.kind, f.level) for f in findings] == [("token_expiring", "critical")]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: FAIL，`cannot import name 'watcher'`

- [ ] **Step 3: 实现**

`src/codexctl/watcher.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass

from . import accounts, events, repo
from .context import Context
from .db import transaction
from .runner import CommandError
from .sysusers import UserError


@dataclass(frozen=True)
class Finding:
    level: str
    kind: str
    target: str
    message: str


def _check_assigned_links(ctx: Context, checked: set, findings: list) -> None:
    for a in repo.all_active(ctx.conn):
        checked.add(("link_mismatch", a.username))
        try:
            user = ctx.lookup_user(a.username, ctx.settings.min_uid)
        except UserError:
            findings.append(Finding("warning", "link_mismatch", a.username, "系统中已找不到该用户，请回收分配"))
            continue
        acc = repo.get_account_by_pk(ctx.conn, a.account_pk)
        expected = str(ctx.publisher.account_file(acc.label))
        state = ctx.publisher.read_link(user)
        if state.kind != "symlink" or state.target != expected:
            actual = f"{state.kind} {state.target or ''}".strip()
            findings.append(Finding("critical", "link_mismatch", a.username, f"应链接到 {expected}，实际为 {actual}"))


def _check_stale_links(ctx: Context, checked: set, findings: list) -> None:
    assigned = {a.username for a in repo.all_active(ctx.conn)}
    prefix = str(ctx.publisher.accounts_dir) + os.sep
    for user in ctx.list_users(ctx.settings.min_uid):
        if user.name in assigned:
            continue
        checked.add(("stale_link", user.name))
        state = ctx.publisher.read_link(user)
        if state.kind == "symlink" and (state.target or "").startswith(prefix):
            findings.append(Finding("warning", "stale_link", user.name, f"未分配账号但仍链接到 {state.target}"))


def _check_accounts(ctx: Context, checked: set, findings: list) -> None:
    now = ctx.now()
    for acc in repo.list_accounts(ctx.conn):
        for kind in ("acl_mismatch", "published_mismatch", "token_expiring"):
            checked.add((kind, acc.label))
        expected = {u.name for u in accounts.readers_for(ctx, acc.id)}
        try:
            actual = ctx.publisher.read_readers(acc.label)
            if actual != expected:
                findings.append(
                    Finding("critical", "acl_mismatch", acc.label, f"ACL 读者为 {sorted(actual)}，应为 {sorted(expected)}")
                )
        except (CommandError, OSError) as e:
            findings.append(Finding("critical", "acl_mismatch", acc.label, f"无法读取 ACL：{e}"))
        try:
            published = ctx.publisher.account_file(acc.label).read_bytes()
        except OSError:
            published = None
        if published != accounts.load_bundle(ctx, acc).to_bytes():
            findings.append(
                Finding("critical", "published_mismatch", acc.label, "发布文件与保险库不一致，可执行 account republish")
            )
        if acc.status != "disabled" and acc.access_expires_at - now < 86400:
            findings.append(
                Finding("critical", "token_expiring", acc.label, "access_token 将在 24 小时内过期，刷新尚未成功")
            )


def run_checks(ctx: Context) -> list[Finding]:
    """执行全部巡检，只告警不修复：新问题打开告警，恢复正常的项关闭告警。"""
    checked: set[tuple[str, str]] = set()
    findings: list[Finding] = []
    _check_assigned_links(ctx, checked, findings)
    _check_stale_links(ctx, checked, findings)
    _check_accounts(ctx, checked, findings)
    now = ctx.now()
    problems = {(f.kind, f.target) for f in findings}
    with transaction(ctx.conn):
        for f in findings:
            events.raise_alert(ctx.conn, f.level, f.kind, f.target, f.message, now=now)
        for kind, target in checked - problems:
            events.resolve_alert(ctx.conn, kind, target, now=now)
    return findings
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_watcher.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/watcher.py tests/test_watcher.py
git commit -m "feat: 巡检链接、ACL、发布文件与 token 有效期"
```

---

### Task 16: 启动自检（selfcheck.py）

**Files:**
- Create: `src/codexctl/selfcheck.py`
- Test: `tests/test_selfcheck.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_selfcheck.py`:

```python
from codexctl.config import Settings
from codexctl.runner import CommandError
from codexctl.selfcheck import selfcheck
from codexctl.vault import generate_key_file
from fakes import FakeRunner


def good_settings(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    key = tmp_path / "master.key"
    generate_key_file(key)
    codex = tmp_path / "codex"
    codex.write_text("")
    return Settings(state_dir=state, key_file=key, codex_bin=codex)


def test_reports_missing_pieces(tmp_path):
    s = Settings(state_dir=tmp_path / "state", key_file=tmp_path / "k", codex_bin=tmp_path / "codex")
    joined = "\n".join(selfcheck(s, run=FakeRunner(), which=lambda t: None))
    assert "apt install acl" in joined
    assert "codex 可执行文件" in joined
    assert "主密钥不可用" in joined
    assert "codexctl init" in joined


def test_all_good(tmp_path):
    runner = FakeRunner()
    assert selfcheck(good_settings(tmp_path), run=runner, which=lambda t: "/usr/bin/" + t) == []
    assert runner.calls[0][:3] == ["setfacl", "-m", "u:root:r"]
    assert not (tmp_path / "state" / ".acl-probe").exists()


def test_acl_unsupported(tmp_path):
    runner = FakeRunner()
    runner.fail_with = CommandError("Operation not supported")
    problems = selfcheck(good_settings(tmp_path), run=runner, which=lambda t: "/usr/bin/" + t)
    assert len(problems) == 1 and "不支持 ACL" in problems[0]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_selfcheck.py -v`
Expected: FAIL，`No module named 'codexctl.selfcheck'`

- [ ] **Step 3: 实现**

`src/codexctl/selfcheck.py`:

```python
from __future__ import annotations

import shutil
import sqlite3
from typing import Callable

from . import db
from . import runner as runner_mod
from .config import Settings
from .runner import CommandError, Runner
from .vault import VaultError, load_key


def selfcheck(
    settings: Settings,
    run: Runner = runner_mod.run,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    problems: list[str] = []
    acl_tools = True
    for tool in (settings.setfacl_bin, settings.getfacl_bin):
        if which(tool) is None:
            problems.append(f"未找到 {tool}，请执行 apt install acl")
            acl_tools = False
    if not settings.codex_bin.exists():
        problems.append(f"未找到 codex 可执行文件：{settings.codex_bin}")
    try:
        load_key(settings.key_file)
    except (OSError, VaultError) as e:
        problems.append(f"主密钥不可用：{e}")
    if not settings.state_dir.is_dir():
        problems.append(f"状态目录不存在：{settings.state_dir}（请先运行 codexctl init）")
        return problems
    try:
        db.connect(settings.db_path).close()
    except sqlite3.Error as e:
        problems.append(f"数据库不可用：{e}")
    if acl_tools:
        probe = settings.state_dir / ".acl-probe"
        try:
            probe.write_bytes(b"")
            run([settings.setfacl_bin, "-m", "u:root:r", str(probe)])
        except (OSError, CommandError) as e:
            problems.append(f"{settings.state_dir} 所在文件系统不支持 ACL：{e}")
        finally:
            probe.unlink(missing_ok=True)
    return problems
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_selfcheck.py -v`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/selfcheck.py tests/test_selfcheck.py
git commit -m "feat: 启动自检"
```

---

### Task 17: 定时任务（daemon.py）

**Files:**
- Create: `src/codexctl/daemon.py`
- Test: `tests/test_daemon.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_daemon.py`:

```python
from codexctl.daemon import build_scheduler, safe_job


def test_scheduler_has_expected_jobs(ctx):
    sched = build_scheduler(ctx)
    jobs = {j.id: j.trigger.interval.total_seconds() for j in sched.get_jobs()}
    assert jobs == {"refresh": 600, "quota": 60, "logscan": 60, "watch": 300}


def test_safe_job_swallows_and_logs_errors(caplog):
    def boom(ctx):
        raise RuntimeError("x")

    safe_job("boom", boom, None)()
    assert "定时任务 boom 失败" in caplog.text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_daemon.py -v`
Expected: FAIL，`No module named 'codexctl.daemon'`

- [ ] **Step 3: 实现**

`src/codexctl/daemon.py`:

```python
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from . import logscan, quota, refresher, watcher
from .context import Context

log = logging.getLogger(__name__)

# (任务名, 函数, 间隔秒数)。单线程执行器保证任务串行，同一时刻只有一个刷新在进行。
JOBS: list[tuple[str, Callable[[Context], object], int]] = [
    ("refresh", refresher.refresh_due, 600),
    ("quota", quota.poll_quotas, 60),
    ("logscan", logscan.scan_all, 60),
    ("watch", watcher.run_checks, 300),
]


def safe_job(name: str, fn: Callable[[Context], object], ctx: Context) -> Callable[[], None]:
    def job() -> None:
        try:
            fn(ctx)
        except Exception:
            log.exception("定时任务 %s 失败", name)

    return job


def build_scheduler(ctx: Context) -> BlockingScheduler:
    sched = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )
    for name, fn, seconds in JOBS:
        sched.add_job(safe_job(name, fn, ctx), "interval", seconds=seconds, id=name, next_run_time=datetime.now())
    return sched
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_daemon.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/codexctl/daemon.py tests/test_daemon.py
git commit -m "feat: 定时任务调度"
```

---

### Task 18: 命令行（cli.py）

**Files:**
- Create: `src/codexctl/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 写失败的测试**

`tests/test_cli.py`:

```python
from codexctl import cli
from fakes import make_auth_json


def run_cli(ctx, *argv):
    return cli.main(["-c", "/nonexistent/config.toml", *argv], context_factory=lambda s: ctx)


def test_account_import_and_list(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    assert run_cli(ctx, "account", "import", "acc1", str(f)) == 0
    assert run_cli(ctx, "account", "list") == 0
    out = capsys.readouterr().out
    assert "acc1" in out and "a@example.com" in out and "用户数=0" in out


def test_assign_users_and_unassign(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    ctx.test_users.add("alice")
    assert run_cli(ctx, "assign", "--account", "acc1", "alice") == 0
    assert run_cli(ctx, "users") == 0
    out = capsys.readouterr().out
    assert "alice -> acc1" in out
    assert any(line.split() == ["alice", "acc1", "symlink"] for line in out.splitlines())
    assert run_cli(ctx, "unassign", "alice") == 0
    assert "alice: 已回收" in capsys.readouterr().out


def test_check_reports_findings(ctx, tmp_path, capsys):
    f = tmp_path / "auth.json"
    f.write_bytes(make_auth_json())
    run_cli(ctx, "account", "import", "acc1", str(f))
    ctx.publisher.account_file("acc1").write_bytes(b"{}")
    assert run_cli(ctx, "check") == 1
    assert "published_mismatch acc1" in capsys.readouterr().out


def test_errors_print_message_and_return_1(ctx, capsys):
    assert run_cli(ctx, "assign", "--account", "nope", "alice") == 1
    assert "错误：" in capsys.readouterr().err


def test_window_name():
    assert cli.window_name(300) == "5小时"
    assert cli.window_name(10080) == "7天"
    assert cli.window_name(None) == "未知窗口"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL，`cannot import name 'cli'`

- [ ] **Step 3: 实现**

`src/codexctl/cli.py`:

```python
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Callable

from . import accounts, assignments, db, refresher, repo, selfcheck, watcher
from .authfile import AuthFileError
from .config import Settings, load_settings
from .context import Context, open_context
from .publisher import PublishError
from .runner import CommandError
from .sysusers import UserError
from .vault import VaultError, generate_key_file

DEFAULT_CONFIG = Path("/etc/codexctl/config.toml")
ACTOR = "cli"
_HANDLED = (
    accounts.AccountError, assignments.AssignError, UserError, AuthFileError,
    PublishError, VaultError, CommandError, OSError,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codexctl", description="Codex 账号集中管理")
    p.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="初始化目录、主密钥和数据库")
    sub.add_parser("selfcheck", help="检查运行环境")

    acc = sub.add_parser("account", help="账号管理").add_subparsers(dest="acmd", required=True)
    acc.add_parser("list", help="列出账号")
    a = acc.add_parser("add", help="通过 device-auth 登录并添加账号")
    a.add_argument("label")
    a = acc.add_parser("import", help="从 auth.json 文件导入账号")
    a.add_argument("label")
    a.add_argument("file", type=Path)
    a = acc.add_parser("relogin", help="重新登录已有账号（不改用户链接）")
    a.add_argument("label")
    a.add_argument("--file", type=Path, help="从文件导入，而不是运行 device-auth")
    for name, help_text in (
        ("refresh", "立即刷新 token"), ("disable", "停用"), ("enable", "启用"), ("republish", "按保险库重新发布凭证文件"),
    ):
        a = acc.add_parser(name, help=help_text)
        a.add_argument("label")

    a = sub.add_parser("assign", help="把账号分配给用户（已分配则切换）")
    a.add_argument("--account", required=True)
    a.add_argument("users", nargs="+")
    a = sub.add_parser("unassign", help="回收用户的账号")
    a.add_argument("users", nargs="+")
    a = sub.add_parser("fix", help="按数据库记录修复用户链接与 ACL")
    a.add_argument("users", nargs="+")
    sub.add_parser("users", help="列出系统用户及其分配")
    sub.add_parser("quota", help="显示各账号最新额度")
    a = sub.add_parser("usage", help="按用户汇总用量")
    a.add_argument("--days", type=int, default=7)
    sub.add_parser("check", help="运行一次巡检")
    sub.add_parser("daemon", help="运行后台定时任务")
    return p


def main(argv: list[str] | None = None, context_factory: Callable[[Settings], Context] = open_context) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    if args.cmd == "init":
        return cmd_init(settings)
    if args.cmd == "selfcheck":
        return report(selfcheck.selfcheck(settings))
    try:
        ctx = context_factory(settings)
        return dispatch(ctx, args)
    except _HANDLED as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def cmd_init(settings: Settings) -> int:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.state_dir.chmod(0o711)
    settings.accounts_dir.mkdir(exist_ok=True)
    settings.accounts_dir.chmod(0o711)
    settings.staging_dir.mkdir(exist_ok=True)
    settings.staging_dir.chmod(0o700)
    if not settings.key_file.exists():
        generate_key_file(settings.key_file)
        print(f"已生成主密钥 {settings.key_file}，请单独备份；丢失后所有账号需重新登录")
    db.connect(settings.db_path).close()
    settings.db_path.chmod(0o600)
    return report(selfcheck.selfcheck(settings))


def report(problems: list[str]) -> int:
    if not problems:
        print("自检通过")
        return 0
    for p in problems:
        print(f"✗ {p}", file=sys.stderr)
    return 1


def window_name(minutes: int | None) -> str:
    if minutes is None:
        return "未知窗口"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}天"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时"
    return f"{minutes}分钟"


def fmt_ts(ts: int | None) -> str:
    return "-" if ts is None else time.strftime("%m-%d %H:%M", time.localtime(ts))


def dispatch(ctx: Context, args: argparse.Namespace) -> int:
    if args.cmd == "account":
        return account_cmd(ctx, args)
    if args.cmd == "assign":
        for name in args.users:
            backup = assignments.assign(ctx, name, args.account, ACTOR)
            print(f"{name} -> {args.account}" + (f"（原 auth.json 已备份为 {backup}）" if backup else ""))
        print("提示：用户需重启正在运行的 codex 才会使用新账号")
        return 0
    if args.cmd == "unassign":
        for name in args.users:
            done = assignments.unassign(ctx, name, ACTOR)
            print(f"{name}: {'已回收' if done else '没有生效的分配'}")
        return 0
    if args.cmd == "fix":
        for name in args.users:
            assignments.reconcile_user(ctx, name, ACTOR)
            print(f"{name}: 已按记录修复")
        return 0
    if args.cmd == "users":
        for user in ctx.list_users(ctx.settings.min_uid):
            a = repo.active_assignment(ctx.conn, user.name)
            label = repo.get_account_by_pk(ctx.conn, a.account_pk).label if a else "-"
            print(f"{user.name:<20} {label:<12} {ctx.publisher.read_link(user).kind}")
        return 0
    if args.cmd == "quota":
        for acc in repo.list_accounts(ctx.conn):
            rows = repo.latest_quota(ctx.conn, acc.id)
            parts = [
                f"{window_name(r['window_minutes'])} {r['used_percent']:.0f}% 重置于 {fmt_ts(r['resets_at'])} ({r['source']})"
                for r in rows
            ] or ["暂无数据"]
            print(f"{acc.label:<12} {acc.status:<14} " + " | ".join(parts))
        return 0
    if args.cmd == "usage":
        for r in repo.usage_summary(ctx.conn, ctx.now() - args.days * 86400):
            print(
                f"{r['username']:<20} {r['label'] or '-':<12} total={r['total']} "
                f"input={r['input']} output={r['output']} turns={r['turns']}"
            )
        return 0
    if args.cmd == "check":
        findings = watcher.run_checks(ctx)
        for f in findings:
            print(f"[{f.level}] {f.kind} {f.target}: {f.message}")
        print(f"发现 {len(findings)} 个问题" if findings else "巡检正常")
        return 1 if findings else 0
    if args.cmd == "daemon":
        from .daemon import build_scheduler

        build_scheduler(ctx).start()
        return 0
    raise AssertionError(f"unhandled command {args.cmd}")


def account_cmd(ctx: Context, args: argparse.Namespace) -> int:
    c = args.acmd
    if c == "list":
        for acc in repo.list_accounts(ctx.conn):
            users = len(repo.active_usernames(ctx.conn, acc.id))
            line = (
                f"{acc.label:<12} {acc.email or '-':<28} {acc.plan_type or '-':<6} {acc.status:<14} "
                f"用户数={users} 上次刷新={fmt_ts(acc.last_refresh)} token到期={fmt_ts(acc.access_expires_at)}"
            )
            print(line + (f" 错误={acc.last_error}" if acc.last_error else ""))
        return 0
    if c == "add":
        acc = accounts.import_account(ctx, args.label, accounts.device_login(ctx, args.label), ACTOR)
        print(f"已添加 {acc.label}（{acc.email}）")
        return 0
    if c == "import":
        acc = accounts.import_account(ctx, args.label, args.file.read_bytes(), ACTOR)
        print(f"已导入 {acc.label}（{acc.email}）")
        return 0
    if c == "relogin":
        data = args.file.read_bytes() if args.file else accounts.device_login(ctx, args.label)
        accounts.relogin_account(ctx, args.label, data, ACTOR)
        print(f"{args.label} 已重新登录并发布")
        return 0
    if c == "refresh":
        ok = refresher.refresh_account(ctx, accounts.require_account(ctx, args.label), ACTOR)
        print("刷新成功" if ok else f"刷新失败：{repo.get_account(ctx.conn, args.label).last_error}")
        return 0 if ok else 1
    if c in ("disable", "enable"):
        accounts.set_disabled(ctx, args.label, c == "disable", ACTOR)
        print(f"{args.label} 已{'停用' if c == 'disable' else '启用'}")
        return 0
    if c == "republish":
        accounts.publish_account(ctx, accounts.require_account(ctx, args.label))
        print(f"{args.label} 已重新发布")
        return 0
    raise AssertionError(f"unhandled account command {c}")
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: `5 passed`

- [ ] **Step 5: 运行全部单元测试**

Run: `.venv/bin/pytest -v`
Expected: 全部通过，无失败

- [ ] **Step 6: Commit**

```bash
git add src/codexctl/cli.py tests/test_cli.py
git commit -m "feat: codexctl 命令行"
```

---

### Task 19: Docker 集成测试（真实用户 + 真实 ACL）

**Files:**
- Create: `tests/integration/Dockerfile`, `tests/integration/test_acl_integration.py`

- [ ] **Step 1: 写 Dockerfile**

`tests/integration/Dockerfile`:

```dockerfile
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv acl util-linux \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1001 alice && useradd -m -u 1002 bob
WORKDIR /src
COPY pyproject.toml /src/
COPY src /src/src
COPY tests /src/tests
RUN python3 -m venv /venv && /venv/bin/pip install -q -e '.[dev]'
CMD ["/venv/bin/pytest", "-m", "integration", "-v", "tests/integration"]
```

- [ ] **Step 2: 写集成测试**

`tests/integration/test_acl_integration.py`:

```python
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from codexctl import accounts, assignments, db, repo
from codexctl.config import Settings
from codexctl.context import Context
from codexctl.publisher import Publisher
from codexctl.vault import Vault
from fakes import make_auth_json

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.geteuid() != 0 or shutil.which("setfacl") is None, reason="需要 root 与 acl"),
]

STATE = Path("/var/lib/codexctl-it")


def as_user(user, *cmd):
    return subprocess.run(["runuser", "-u", user, "--", *cmd], capture_output=True)


@pytest.fixture
def ctx():
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir()
    STATE.chmod(0o711)
    for name in ("alice", "bob"):
        shutil.rmtree(Path("/home") / name / ".codex", ignore_errors=True)
    s = Settings(state_dir=STATE)
    c = Context(
        settings=s,
        conn=db.connect(":memory:"),
        vault=Vault(AESGCM.generate_key(bit_length=256)),
        publisher=Publisher(s.accounts_dir),
        http=httpx.Client(),
    )
    accounts.import_account(c, "acc1", make_auth_json(account_id="a1", refresh_token="rt-a1"), "it")
    accounts.import_account(c, "acc2", make_auth_json(account_id="a2", refresh_token="rt-a2"), "it")
    yield c
    shutil.rmtree(STATE, ignore_errors=True)


def test_assigned_user_can_read_but_not_write(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    r = as_user("alice", "cat", "/home/alice/.codex/auth.json")
    assert r.returncode == 0 and b"rt-a1" in r.stdout
    w = as_user("alice", "sh", "-c", "echo x > /home/alice/.codex/auth.json")
    assert w.returncode != 0
    assert as_user("bob", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_switch_moves_access(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.assign(ctx, "alice", "acc2", "it")
    r = as_user("alice", "cat", "/home/alice/.codex/auth.json")
    assert r.returncode == 0 and b"rt-a2" in r.stdout
    assert as_user("alice", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_republish_keeps_access(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.assign(ctx, "bob", "acc1", "it")
    accounts.publish_account(ctx, repo.get_account(ctx.conn, "acc1"))
    for name in ("alice", "bob"):
        assert as_user(name, "cat", f"/home/{name}/.codex/auth.json").returncode == 0
    assert ctx.publisher.read_readers("acc1") == {"alice", "bob"}


def test_unassign_revokes(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    assignments.unassign(ctx, "alice", "it")
    assert not os.path.lexists("/home/alice/.codex/auth.json")
    assert as_user("alice", "cat", str(ctx.publisher.account_file("acc1"))).returncode != 0


def test_link_owned_by_user(ctx):
    assignments.assign(ctx, "alice", "acc1", "it")
    st = os.lstat("/home/alice/.codex/auth.json")
    assert st.st_uid == 1001
    assert os.stat("/home/alice/.codex").st_uid == 1001
```

- [ ] **Step 3: 构建并运行**

Run:

```bash
docker build -f tests/integration/Dockerfile -t codexctl-it .
docker run --rm codexctl-it
```

Expected: `5 passed`。
如果出现 `Operation not supported`（容器文件系统不支持 ACL），改用 tmpfs 运行：`docker run --rm --tmpfs /var/lib/codexctl-it:mode=0711 codexctl-it`，并在 fixture 中把 `shutil.rmtree(STATE)` / `STATE.mkdir()` 改为清空目录内容。

- [ ] **Step 4: Commit**

```bash
git add tests/integration
git commit -m "test: Docker 中验证真实 ACL 与多用户读写权限"
```

---

### Task 20: 部署文件与运维手册

**Files:**
- Create: `deploy/codex-wrapper.sh`, `deploy/requirements.toml`, `deploy/codexctl.service`, `deploy/config.example.toml`, `docs/deploy.md`
- Test: `tests/test_wrapper.py`

- [ ] **Step 1: 写包装脚本测试**

`tests/test_wrapper.py`:

```python
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "deploy" / "codex-wrapper.sh"


def run_wrapper(tmp_path, *args):
    real = tmp_path / "real-codex"
    real.write_text('#!/bin/sh\necho "real $*"\n')
    real.chmod(0o755)
    return subprocess.run(
        ["sh", str(WRAPPER), *args], capture_output=True, text=True, env={"CODEX_REAL": str(real), "PATH": "/usr/bin:/bin"}
    )


def test_blocks_login_and_logout(tmp_path):
    for cmd in ("login", "logout"):
        r = run_wrapper(tmp_path, cmd)
        assert r.returncode == 1
        assert "管理员" in r.stderr


def test_allows_login_status(tmp_path):
    r = run_wrapper(tmp_path, "login", "status")
    assert r.returncode == 0 and r.stdout == "real login status\n"


def test_passes_through_other_commands(tmp_path):
    r = run_wrapper(tmp_path, "exec", "hello world")
    assert r.stdout == "real exec hello world\n"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/test_wrapper.py -v`
Expected: FAIL（脚本不存在）

- [ ] **Step 3: 写部署文件**

`deploy/codex-wrapper.sh`（安装为 `/usr/local/bin/codex`）:

```sh
#!/bin/sh
# Codex 登录状态由管理员通过 codexctl 统一管理，禁止用户自行 login / logout。
# 注意：logout 会在服务端吊销同一账号所有用户共用的 token。
REAL="${CODEX_REAL:-/opt/codex/bin/codex}"
case "$1" in
  login)
    if [ "$2" = "status" ]; then
      exec "$REAL" "$@"
    fi
    echo "Codex 账号由管理员统一分配，请勿自行 login。如需帮助请联系管理员。" >&2
    exit 1
    ;;
  logout)
    echo "Codex 账号由管理员统一分配，请勿自行 logout（会导致同账号所有用户掉线）。" >&2
    exit 1
    ;;
esac
exec "$REAL" "$@"
```

`deploy/requirements.toml`（安装为 `/etc/codex/requirements.toml`）:

```toml
# 管理员强制配置：只允许 ChatGPT 登录方式，禁止 API key。
allowed_login_methods = ["chatgpt"]
```

`deploy/codexctl.service`（安装为 `/etc/systemd/system/codexctl.service`）:

```ini
[Unit]
Description=codexctl - Codex account control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
UMask=0077
ExecStartPre=/opt/codexctl/venv/bin/codexctl -c /etc/codexctl/config.toml selfcheck
ExecStart=/opt/codexctl/venv/bin/codexctl -c /etc/codexctl/config.toml daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`deploy/config.example.toml`（复制为 `/etc/codexctl/config.toml`，只写需要改的项）:

```toml
# state_dir = "/var/lib/codexctl"
# key_file = "/etc/codexctl/master.key"
# codex_bin = "/opt/codex/bin/codex"
# min_uid = 1000
# refresh_interval_days = 4.0
# refresh_min_validity_days = 3.0
# quota_warn_percent = 80.0
# quota_crit_percent = 95.0
```

`docs/deploy.md`:

````markdown
# codexctl 部署与运维

## 前提

- Ubuntu 24.04 / Debian 12（Python ≥ 3.11），root 权限。
- 已完成 `docs/server-validation.md` 中的前置验证且全部通过。

## 安装

```bash
sudo -i
apt-get install -y acl python3-venv nodejs npm
npm install -g --prefix /opt/codex @openai/codex
install -m 755 deploy/codex-wrapper.sh /usr/local/bin/codex
install -d -m 755 /etc/codex && install -m 644 deploy/requirements.toml /etc/codex/requirements.toml
python3 -m venv /opt/codexctl/venv && /opt/codexctl/venv/bin/pip install /path/to/codex_control
install -d -m 700 /etc/codexctl && install -m 600 deploy/config.example.toml /etc/codexctl/config.toml
/opt/codexctl/venv/bin/codexctl init
install -m 644 deploy/codexctl.service /etc/systemd/system/codexctl.service
systemctl daemon-reload && systemctl enable --now codexctl
```

确认 `/usr/local/bin` 在用户 `PATH` 中排在 npm 全局目录之前：`sudo -u <user> -i which codex` 应输出 `/usr/local/bin/codex`。

## 日常操作（`codexctl` 即 `/opt/codexctl/venv/bin/codexctl`）

| 操作 | 命令 |
|---|---|
| 添加账号（终端显示验证码，在浏览器批准） | `codexctl account add acc1` |
| 查看账号 | `codexctl account list` |
| 分配 / 切换（可多个用户） | `codexctl assign --account acc1 alice bob` |
| 回收 | `codexctl unassign alice` |
| 查看用户分配与链接状态 | `codexctl users` |
| 查看额度 | `codexctl quota` |
| 查看近 7 天用量 | `codexctl usage --days 7` |
| 立即巡检 | `codexctl check` |
| 修复某用户链接 | `codexctl fix alice` |
| 账号显示 needs_relogin 时重新登录 | `codexctl account relogin acc1` |
| 发布文件与保险库不一致时 | `codexctl account republish acc1` |
| 查看服务日志 | `journalctl -u codexctl -f` |

## 重要注意事项

- **永远不要**在服务器上以任何身份对已分配账号执行 `codex logout`：会在服务端吊销 token，所有共用该账号的用户掉线，只能 `account relogin` 恢复。
- 切换账号后，用户正在运行的 codex 进程仍使用内存中的旧 token，需重启 codex。
- 用户可以读取自己被分配账号的凭证。怀疑泄露时对该账号执行 `account relogin`，使旧凭证作废。
- 多人共用一个账号会平摊额度，并可能触发 OpenAI 风控。

## 备份

- `/var/lib/codexctl/codexctl.db`（含加密凭证）与 `/etc/codexctl/master.key` **分开**备份。
- 丢失主密钥：所有账号需重新 `account add`（先删除数据库或使用新状态目录）。

## 升级 Codex 后

重新执行 `docs/server-validation.md` 中的 Step 2–4，确认软链接写入行为与额度接口格式未变。
````

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/pytest tests/test_wrapper.py -v`
Expected: `3 passed`

- [ ] **Step 5: 运行全部单元测试**

Run: `.venv/bin/pytest -v`
Expected: 全部通过

- [ ] **Step 6: Commit**

```bash
git add deploy docs/deploy.md tests/test_wrapper.py
git commit -m "feat: 部署文件（codex 包装脚本、强制配置、systemd）与运维手册"
```

---

## 完成标准

- `.venv/bin/pytest` 全部通过；`docker run --rm codexctl-it` 集成测试全部通过。
- 在目标服务器按 `docs/deploy.md` 部署后，人工验收：
  1. `codexctl account add` 添加 3 个账号；
  2. `codexctl assign` 给测试用户分配，该用户直接运行 `codex exec` 成功；
  3. `codexctl account refresh acc1` 后，用户正在使用的 codex 不中断，新会话正常；
  4. `codexctl quota` 显示额度，`codexctl usage` 显示该用户用量；
  5. 手动删除用户软链接后 5 分钟内 `journalctl` 无报错，`codexctl check` 报告 `link_mismatch`，`codexctl fix` 后恢复。
