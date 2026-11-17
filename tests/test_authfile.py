import json

import pytest
from fakes import T0, make_auth_json, make_jwt

from codexctl.accounts.authfile import (
    AuthBundle,
    AuthFileError,
    format_rfc3339,
    parse_rfc3339,
)


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


def _mutated(mutate):
    raw = json.loads(make_auth_json())
    mutate(raw)
    return json.dumps(raw).encode()


def test_rejects_string_auth_claim():
    tok = make_jwt({"email": "a@example.com", "https://api.openai.com/auth": "oops", "exp": T0})

    def m(raw):
        raw["tokens"]["id_token"] = tok
        del raw["tokens"]["account_id"]

    with pytest.raises(AuthFileError):
        AuthBundle.from_bytes(_mutated(m))


def test_rejects_non_string_account_id():
    with pytest.raises(AuthFileError):
        AuthBundle.from_bytes(_mutated(lambda r: r["tokens"].__setitem__("account_id", 5)))


def test_rejects_garbage_id_token():
    with pytest.raises(AuthFileError):
        AuthBundle.from_bytes(_mutated(lambda r: r["tokens"].__setitem__("id_token", "garbage")))


def test_rejects_infinite_exp():
    tok = make_jwt({"client_id": "app_test", "exp": float("inf")})
    with pytest.raises(AuthFileError):
        AuthBundle.from_bytes(_mutated(lambda r: r["tokens"].__setitem__("access_token", tok)))


def test_rejects_bool_exp():
    tok = make_jwt({"client_id": "app_test", "exp": True})
    with pytest.raises(AuthFileError):
        AuthBundle.from_bytes(_mutated(lambda r: r["tokens"].__setitem__("access_token", tok)))


def test_rejects_bad_last_refresh():
    with pytest.raises(AuthFileError, match="时间格式"):
        AuthBundle.from_bytes(_mutated(lambda r: r.__setitem__("last_refresh", "bad")))


def test_client_id_none_unless_nonempty_str():
    tok = make_jwt({"client_id": 7, "exp": T0 + 100})
    b = AuthBundle.from_bytes(_mutated(lambda r: r["tokens"].__setitem__("access_token", tok)))
    assert b.client_id is None
