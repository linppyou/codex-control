import pytest

from codexctl import db, webauth
from codexctl.config import load_settings
from codexctl.webauth import AuthError, LoginRateLimiter

PW = "correct-horse-battery"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_create_admin_and_verify(conn):
    webauth.create_admin(conn, "root_admin", PW, now=100)
    assert webauth.list_admins(conn) == ["root_admin"]
    assert webauth.verify_login(conn, "root_admin", PW)
    assert not webauth.verify_login(conn, "root_admin", "wrong-password-xx")
    assert not webauth.verify_login(conn, "nobody", PW)


@pytest.mark.parametrize("name", ["Admin", "1abc", "", "a" * 33, "a b"])
def test_invalid_username(conn, name):
    with pytest.raises(AuthError):
        webauth.create_admin(conn, name, PW, now=1)


def test_short_password_and_duplicate(conn):
    with pytest.raises(AuthError):
        webauth.create_admin(conn, "bob", "short", now=1)
    webauth.create_admin(conn, "bob", PW, now=1)
    with pytest.raises(AuthError):
        webauth.create_admin(conn, "bob", PW, now=2)


def test_session_roundtrip(conn):
    token, csrf = webauth.create_session(conn, "bob", now=1000)
    assert webauth.get_session(conn, token, now=1001) == ("bob", csrf)
    stored = conn.execute("SELECT token_hash FROM web_sessions").fetchone()["token_hash"]
    assert stored != token and token not in stored
    assert webauth.get_session(conn, token, now=1000 + webauth.SESSION_TTL + 1) is None
    webauth.delete_session(conn, token)
    assert webauth.get_session(conn, token, now=1001) is None


def test_create_session_purges_expired(conn):
    webauth.create_session(conn, "bob", now=0)
    webauth.create_session(conn, "bob", now=webauth.SESSION_TTL + 10)
    assert conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1


def test_set_password_clears_sessions(conn):
    webauth.create_admin(conn, "bob", PW, now=1)
    token, _ = webauth.create_session(conn, "bob", now=2)
    other, _ = webauth.create_session(conn, "carol", now=2)
    webauth.set_password(conn, "bob", "another-long-password", now=3)
    assert webauth.get_session(conn, token, now=4) is None
    assert webauth.get_session(conn, other, now=4) is not None
    assert webauth.verify_login(conn, "bob", "another-long-password")
    with pytest.raises(AuthError):
        webauth.set_password(conn, "ghost", PW, now=3)


def test_rate_limiter():
    rl = LoginRateLimiter(max_failures=5, window=900)
    for i in range(5):
        assert rl.allowed("1.1.1.1", i)
        rl.record_failure("1.1.1.1", i)
    assert not rl.allowed("1.1.1.1", 10)
    assert rl.allowed("2.2.2.2", 10)
    assert rl.allowed("1.1.1.1", 4 + 900)
    rl.reset("1.1.1.1")
    assert rl.allowed("1.1.1.1", 10)


def test_hidden_users_list_becomes_tuple(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('hidden_users = ["a", "b"]\n')
    assert load_settings(f).hidden_users == ("a", "b")


def test_limiter_caps_keys():
    rl = LoginRateLimiter(max_failures=5, window=900)
    for i in range(10_050):
        rl.record_failure(f"k{i}", 1000 + i // 100)
    assert len(rl._failures) <= 10_000
    rl2 = LoginRateLimiter(max_failures=5, window=10)
    for i in range(9_000):
        rl2.record_failure(f"old{i}", 0)
    for i in range(2_000):
        rl2.record_failure(f"new{i}", 100)
    assert len(rl2._failures) == 2_000


def test_verify_login_rejects_overlong_and_rehash_guard(conn):
    webauth.create_admin(conn, "carol", PW, now=1)
    assert not webauth.verify_login(conn, "carol", PW + "x" * 1100)
    assert not webauth.verify_login(conn, "c" * 65, PW)
    assert webauth.verify_login(conn, "carol", PW)


def test_verify_login_rehash_updates_only_matching_hash(conn):
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PW)
    conn.execute("INSERT INTO admins(username, password_hash, created_at) VALUES ('dave', ?, 1)", (weak,))
    assert webauth.verify_login(conn, "dave", PW)
    new = conn.execute("SELECT password_hash FROM admins WHERE username='dave'").fetchone()[0]
    assert new != weak and webauth.verify_login(conn, "dave", PW)


def test_limiter_evicts_non_user_keys_first(monkeypatch):
    monkeypatch.setattr(webauth, "MAX_LIMITER_KEYS", 10)
    rl = LoginRateLimiter(max_failures=5, window=900)
    for i in range(5):
        rl.record_failure(f"user:u{i}", 100 + i)
    for i in range(6):
        rl.record_failure(f"ip{i}", 200 + i)
    assert len(rl._failures) == 10
    assert all(f"user:u{i}" in rl._failures for i in range(5))
    assert "ip0" not in rl._failures


def test_check_password_pure():
    h = webauth._hasher.hash(PW)
    assert webauth.check_password(h, PW)
    assert not webauth.check_password(h, "nope-nope-nope")
    assert not webauth.check_password(None, PW)
    assert not webauth.check_password(h, PW + "x" * 1100)
