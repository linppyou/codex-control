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
