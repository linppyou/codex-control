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
    hidden_users: tuple[str, ...] = ()

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
        kwargs[key] = value
    return Settings(**kwargs)
