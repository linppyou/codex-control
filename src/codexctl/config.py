from __future__ import annotations

import tomllib
import math
from dataclasses import dataclass, fields
from pathlib import Path

from codexctl.users.sysusers import valid_name

_PATH_KEYS = {
    "state_dir", "key_file", "codex_bin", "codex_wrapper", "codex_requirements", "site_ssh_ca_key",
    "dispatch_browser_python", "dispatch_chrome_binary",
}


@dataclass(frozen=True)
class Settings:
    deployment_mode: str = "managed"
    gateway_enabled: bool = False
    gateway_client_version: str = "0.167.0"
    gateway_base_url: str = "https://codex-control.ppspace.xyz/v1"
    gateway_http2: bool = True
    gateway_max_keepalive_connections: int = 128
    gateway_keepalive_expiry: float = 90.0
    gateway_transport: str = "http"
    gateway_request_compression: bool = False
    gateway_compression_min_bytes: int = 65536
    gateway_ws_idle_seconds: float = 90.0
    gateway_ws_max_idle_connections: int = 64
    gateway_credential_cache_size: int = 128
    state_dir: Path = Path("/var/lib/codexctl")
    key_file: Path = Path("/etc/codexctl/master.key")
    codex_bin: Path = Path("/opt/codex/bin/codex")
    dispatch_browser_python: Path = Path("/opt/codexctl/browser-runtime/venv/bin/python")
    dispatch_chrome_binary: Path = Path("/opt/codexctl/browser-runtime/chromium/chrome")
    setfacl_bin: str = "setfacl"
    getfacl_bin: str = "getfacl"
    min_uid: int = 1000
    refresh_interval_days: float = 4.0
    refresh_min_validity_days: float = 3.0
    quota_warn_percent: float = 80.0
    quota_crit_percent: float = 95.0
    quota_poll_seconds: int = 600
    oauth_token_url: str = "https://auth.openai.com/oauth/token"
    oauth_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    usage_url: str = "https://chatgpt.com/backend-api/wham/usage"
    http_timeout: float = 10.0
    log_lookback_days: int = 8
    web_host: str = "127.0.0.1"
    web_port: int = 8700
    web_cookie_secure: bool = True
    web_socket: str = "/run/codexctl/web.sock"
    web_socket_group: str = "www"
    site_ssh_enabled: bool = False
    site_ssh_shared: bool = False
    site_ssh_ca_key: Path | None = None
    web_system_users: tuple[str, ...] = ()
    hidden_users: tuple[str, ...] = ()
    codex_wrapper: Path = Path("/usr/local/bin/codex")
    codex_requirements: Path = Path("/etc/codex/requirements.toml")
    codex_version: str = "0.167.0"
    codex_package_url: str = (
        "https://github.com/openai/codex/releases/download/"
        "rust-v{version}/codex-package-{arch}-unknown-linux-musl.tar.gz"
    )
    codex_auto_install: bool = True
    codex_install_timeout: float = 300.0

    def __post_init__(self) -> None:
        if self.deployment_mode not in ("managed", "gateway"):
            raise ValueError("deployment_mode 必须为 managed 或 gateway")
        if self.gateway_transport not in ("http", "auto", "websocket"):
            raise ValueError("gateway_transport 必须为 http、auto 或 websocket")
        for name in ("gateway_http2", "gateway_request_compression"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} 必须为布尔值")
        for name in ("gateway_max_keepalive_connections", "gateway_ws_max_idle_connections",
                     "gateway_credential_cache_size"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须为非负整数")
        if type(self.gateway_compression_min_bytes) is not int or self.gateway_compression_min_bytes < 1:
            raise ValueError("gateway_compression_min_bytes 必须为正整数")
        for name in ("gateway_keepalive_expiry", "gateway_ws_idle_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")

    @property
    def manages_local_codex(self) -> bool:
        return self.deployment_mode == "managed"

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
    # Ignore retired settings in existing deployments.
    data.pop("wp_ssh_key", None)
    data.pop("site_ssh_trusted_hosts", None)
    known = {f.name for f in fields(Settings)}
    kwargs = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"unknown config key: {key}")
        if key in _PATH_KEYS:
            value = Path(value)
        elif key == "hidden_users":
            value = tuple(value)
        elif key == "web_system_users":
            if not isinstance(value, list) or any(
                not isinstance(name, str) or not valid_name(name) or name in {"admin", "root"}
                for name in value
            ):
                raise ValueError("web_system_users 必须是普通 Linux 用户名列表")
            value = tuple(value)
        kwargs[key] = value
    return Settings(**kwargs)
