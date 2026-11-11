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
