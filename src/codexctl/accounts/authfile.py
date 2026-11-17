from __future__ import annotations

import base64
import copy
import json
import math
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
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as e:
        raise AuthFileError(f"时间格式无效：{value!r}") from e
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
        bundle._id_claims()  # 校验 id_token 可解析
        if not isinstance(bundle.account_id, str) or not bundle.account_id:
            raise AuthFileError("无法确定 ChatGPT account_id")
        bundle.access_expires_at  # 校验 access_token 可解析且含 exp
        bundle.last_refresh  # 校验时间格式
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

    def _auth_claim(self) -> dict:
        claim = self._id_claims().get(AUTH_CLAIM)
        return claim if isinstance(claim, dict) else {}

    @property
    def account_id(self) -> str:
        return self.tokens.get("account_id") or self._auth_claim().get("chatgpt_account_id") or ""

    @property
    def email(self) -> str | None:
        return self._id_claims().get("email")

    @property
    def plan_type(self) -> str | None:
        return self._auth_claim().get("chatgpt_plan_type")

    @property
    def access_expires_at(self) -> int:
        exp = jwt_claims(self.access_token).get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
            raise AuthFileError("access_token 缺少 exp")
        return int(exp)

    @property
    def client_id(self) -> str | None:
        cid = jwt_claims(self.access_token).get("client_id")
        return cid if isinstance(cid, str) and cid else None

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
