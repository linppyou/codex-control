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
