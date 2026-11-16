from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .sysusers import valid_name

_PATH_KEYS = {"state_dir", "key_file", "codex_bin", "codex_wrapper", "codex_requirements"}


@dataclass(frozen=True)
class Settings:
    deployment_mode: str = "managed"
    gateway_enabled: bool = False
    gateway_client_version: str = "0.167.0"
    gateway_base_url: str = "https://codex-control.ppspace.xyz/v1"
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
