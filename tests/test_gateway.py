from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from codexctl import accounts, daemon, db, webauth
from codexctl.gateway import storage, upstream
from codexctl.web.app import create_app
from fakes import make_auth_json

MODELS = {"models": [
    {"slug": "model-a", "service_tiers": [{"id": "priority"}], "prompt": "Keep upstream instructions"},
    {"slug": "model-b", "context_window": 90},
]}


def event(kind, **fields):
    return b"event: " + kind.encode() + b"\r\ndata: " + json.dumps({"type": kind, **fields}).encode() + b"\r\n\r\n"


@pytest.fixture
def setup(ctx):
    ctx.settings = replace(ctx.settings, deployment_mode="gateway", gateway_enabled=True, web_cookie_secure=False)
    ctx.conn.close()
    ctx.conn = db.connect(ctx.settings.db_path)
    webauth.create_admin(ctx.conn, "admin", "test gateway password", now=ctx.now())
    accs = [
        accounts.import_account(ctx, label, make_auth_json(account_id=label), "test")
        for label in ("acc-one", "acc-two")
    ]
    tokens = []
    for account, model in zip(accs, ("model-a", "model-b")):
        storage.store_catalog(ctx.conn, account.id, ctx.settings.gateway_client_version, True, MODELS, ctx.now())
        storage.verify_model(ctx.conn, account.id, model, False, True, "test", ctx.now())
        storage.enable_model(ctx.conn, account.id, model, True, "test", ctx.now())
        tokens.append(storage.save_key(
            ctx.conn, actor="test", now=ctx.now(), name=account.label, account_pk=account.id,
            model_ids=[model], default_model=model, allow_fast=False, username=account.label,
        ))
    assert ctx.test_runner.calls == []
    yield ctx, accs, tokens
    ctx.conn.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def client_for(ctx, handler):
    gateway = upstream.Gateway(ctx, transport=httpx.MockTransport(handler))
    return TestClient(create_app(ctx, gateway=gateway))


def test_permissions_versioned_catalog_and_key_lifecycle(setup):
    ctx, accs, tokens = setup
    (public_id, secret), (_, other_secret) = tokens
    requested = []

    async def upstream_request(request):
        requested.append(request)
        return httpx.Response(200, json=MODELS)

    with client_for(ctx, upstream_request) as client:
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers=bearer(secret)).json()["data"][0]["id"] == "model-a"
        result = client.get("/v1/codex/models?client_version=0.172.0", headers=bearer(secret))
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["models"] == [{
            "slug": "model-a", "service_tiers": [], "default_service_tier": None,
            "additional_speed_tiers": [], "prompt": "Keep upstream instructions",
        }]
        assert requested[0].url.params["client_version"] == "0.172.0"
        assert requested[0].headers["chatgpt-account-id"] == accs[0].account_id
        assert client.post("/v1/responses", json={"model": "model-b"}, headers=bearer(secret)).status_code == 403
        assert client.post("/v1/responses", json={"model": "model-a", "service_tier": "priority"},
                           headers=bearer(secret)).status_code == 403
        assert client.post("/v1/responses/compact", json={"model": "model-a"},
                           headers=bearer(secret)).json()["error"]["code"] == "legacy_compaction_unsupported"
        rotated = storage.rotate_key(ctx.conn, public_id, 1, "test", ctx.now())
        with pytest.raises(storage.GatewayError):
            storage.rotate_key(ctx.conn, public_id, 1, "test", ctx.now())
        assert client.get("/v1/models", headers=bearer(secret)).status_code == 401
        accounts.set_disabled(ctx, accs[0].label, True, "test")
        assert client.get("/v1/models", headers=bearer(rotated)).status_code == 503
        assert client.get("/v1/models", headers=bearer(other_secret)).json()["data"][0]["id"] == "model-b"
        accounts.set_disabled(ctx, accs[0].label, False, "test")
        assert client.get("/v1/models", headers=bearer(rotated)).status_code == 200
        storage.revoke_key(ctx.conn, public_id, "test", ctx.now())
        assert client.get("/v1/models", headers=bearer(rotated)).status_code == 401
    assert len(requested) == 1
    assert ctx.conn.execute("SELECT secret_hash FROM api_keys WHERE public_id=?", (public_id,)).fetchone()[0] != rotated


def test_stream_payload_tool_events_and_usage(setup):
    ctx, accs, tokens = setup
    received = []
    usage = {"input_tokens": 15, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 4},
             "output_tokens_details": {"reasoning_tokens": 2}}
    raw = event("response.function_call_arguments.delta", delta='{"text":"hello"}') + event(
        "response.completed", response={"usage": usage, "service_tier": "default"},
    )

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(raw), 11):
                yield raw[start:start + 11]

    async def upstream_request(request):
        received.append(request)
        return httpx.Response(200, stream=Chunks(), headers={"x-codex-turn-state": "upstream-state"})

    tool_result = {"type": "function_call_output", "call_id": "call-1", "output": "ran locally"}
    compacted = {"type": "compaction", "encrypted_content": "opaque"}
    payload = {"model": "model-a", "input": [tool_result, compacted], "stream": True, "store": True,
               "context_management": [{"type": "compaction", "compact_threshold": 1024}]}
    auth = bearer(tokens[0][1])
    with client_for(ctx, upstream_request) as client:
        first = client.post("/v1/responses", json=payload, headers={
            **auth, "Cookie": "private", "ChatGPT-Account-ID": "spoofed", "Session-Id": "native-session",
        })
        assert first.status_code == 200 and first.content == raw
        assert first.headers["content-type"].startswith("text/event-stream")
        state = first.headers["x-codex-turn-state"]
        assert state != "upstream-state"
        client.post("/v1/responses", json=payload, headers={**auth, "X-Codex-Turn-State": state})
        client.post("/v1/responses", json=dict(payload, model="model-b"),
                    headers={**bearer(tokens[1][1]), "X-Codex-Turn-State": state})
    sent = received[0]
    assert "cookie" not in sent.headers
    assert sent.headers["authorization"] != auth["Authorization"]
    assert sent.headers["chatgpt-account-id"] == accs[0].account_id
    assert sent.headers["session-id"] != "native-session"
    assert received[1].headers["x-codex-turn-state"] == "upstream-state"
    assert "x-codex-turn-state" not in received[2].headers
    data = json.loads(sent.content)
    assert data["store"] is False and data["input"] == [tool_result, compacted]
    assert data["context_management"] == payload["context_management"]
    row = ctx.conn.execute("SELECT * FROM api_usage_events WHERE request_id=?", (first.headers["x-request-id"],)).fetchone()
    assert row["status"] == "completed" and row["account_pk"] == accs[0].id
    assert row["total_tokens"] == 22 and row["cached_input_tokens"] == 4 and row["reasoning_output_tokens"] == 2
    assert row["first_output_ms"] is not None


def test_parallel_same_key_and_upstream_rate_limit(setup):
    ctx, accs, tokens = setup
    calls = 0
    both_requests_seen = asyncio.Event()

    async def upstream_request(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            both_requests_seen.set()
        await asyncio.wait_for(both_requests_seen.wait(), timeout=5)
        if json.loads(request.content)["input"][0]["content"][0]["text"] == "limit":
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, content=event("response.completed", response={"usage": {}}))

    with client_for(ctx, upstream_request) as client, ThreadPoolExecutor(max_workers=2) as pool:
        def request(text):
            return client.post("/v1/responses", headers=bearer(tokens[0][1]),
                               json={"model": "model-a", "input": text, "stream": True})
        allowed = pool.submit(request, "ok")
        limited = pool.submit(request, "limit")
        assert allowed.result().status_code == 200
        result = limited.result()
        assert result.status_code == 429 and result.headers["retry-after"] == "30"
        assert result.json()["error"]["code"] == "upstream_rate_limited"
    assert calls == 2
    assert ctx.conn.execute("SELECT count(*) FROM api_usage_events").fetchone()[0] == 2


def test_gateway_admin_requires_csrf_and_disables_local_actions(setup):
    ctx, accs, tokens = setup
    assert {job.id for job in daemon.build_scheduler(ctx).get_jobs()} == {"refresh", "quota", "api-usage"}
    with client_for(ctx, lambda _: httpx.Response(500)) as client:
        assert client.get("/api-keys", follow_redirects=False).status_code == 303
        client.get("/login")
        client.post("/login", data={"username": "admin", "password": "test gateway password",
                                   "csrf": client.cookies["codexctl_prelogin"]})
        page = client.get("/api-keys")
        assert page.status_code == 200
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        assert client.get("/users").status_code == 404
        assert client.post("/accounts/login", data={"csrf": csrf}).status_code == 404
        assert client.post("/api-keys/save", data={"account_pk": accs[0].id}).status_code == 403
        result = client.post("/api-keys/save", data={"csrf": csrf, "name": "demo", "account_pk": accs[0].id,
                                                     "username": "demo-user",
                                                     "model_ids": "model-a", "default_model": "model-a"})
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        secret = re.search(r'value="(cctl_.*?\..*?)"', result.text)[1]
        assert secret not in client.get("/api-keys").text
        assert client.get("/v1/models", headers=bearer(secret)).status_code == 200
        model_page = client.get(f"/accounts/{accs[0].label}/models")
        assert model_page.status_code == 200 and "model-a" in model_page.text


def test_disconnect_closes_only_its_upstream_response(setup):
    import socket
    import threading
    import time

    import uvicorn

    ctx, _, tokens = setup
    closed = threading.Event()

    class IdleStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event("response.output_text.delta", delta="start")
            await asyncio.sleep(60)

        async def aclose(self):
            closed.set()

    async def upstream_request(request):
        data = json.loads(request.content)
        if data["input"][0]["content"][0]["text"] == "cancel":
            return httpx.Response(200, stream=IdleStream())
        return httpx.Response(200, content=event("response.completed", response={"usage": {}}))

    gateway = upstream.Gateway(ctx, transport=httpx.MockTransport(upstream_request))
    app = create_app(ctx, gateway=gateway)
    config = uvicorn.Config(app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            auth = bearer(tokens[0][1])
            with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
                with client.stream("POST", "/v1/responses", headers=auth,
                                   json={"model": "model-a", "input": "cancel", "stream": True}) as response:
                    cancelled_id = response.headers["x-request-id"]
                    chunks = response.iter_bytes()
                    assert b"response.output_text.delta" in next(chunks)
                    other = client.post("/v1/responses", headers=auth,
                                        json={"model": "model-a", "input": "finish", "stream": True})
                    assert b"response.completed" in other.content
                    assert not closed.is_set()
                assert closed.wait(2)
        finally:
            server.should_exit = True
            thread.join(5)
    assert not thread.is_alive()
    rows = ctx.conn.execute("SELECT request_id, status FROM api_usage_events").fetchall()
    assert dict(rows) == {cancelled_id: "cancelled", other.headers["x-request-id"]: "completed"}


def test_upstream_auth_rejection_coalesces_refresh_per_account(setup):
    from fakes import make_jwt

    ctx, accs, tokens = setup
    old_access = accounts.load_bundle(ctx, accs[0]).access_token
    new_access = make_jwt({"exp": ctx.now() + 86400, "client_id": "app_test"})
    refreshes = []
    old_requests = 0
    both_old_requests_seen = asyncio.Event()

    def oauth_refresh(request):
        refreshes.append(json.loads(request.content)["refresh_token"])
        return httpx.Response(200, json={"access_token": new_access, "refresh_token": "rt-rotated"})

    ctx.test_routes[("POST", ctx.settings.oauth_token_url)] = oauth_refresh

    async def upstream_request(request):
        nonlocal old_requests
        assert request.headers["chatgpt-account-id"] == accs[0].account_id
        if request.headers["authorization"] == f"Bearer {old_access}":
            old_requests += 1
            if old_requests == 2:
                both_old_requests_seen.set()
            await asyncio.wait_for(both_old_requests_seen.wait(), 5)
            return httpx.Response(401)
        assert request.headers["authorization"] == f"Bearer {new_access}"
        return httpx.Response(200, content=event("response.completed", response={"usage": {}}))

    with client_for(ctx, upstream_request) as client, ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(client.post, "/v1/responses", headers=bearer(tokens[0][1]),
                            json={"model": "model-a", "stream": True, "input": "ping"}) for _ in range(2)]
        assert all(job.result().status_code == 200 for job in jobs)
    assert old_requests == 2
    assert refreshes == ["rt-1"]
    assert ctx.test_runner.calls == []


def test_user_selects_all_catalog_models_before_testing(setup):
    from codexctl import repo

    ctx, accs, _ = setup
    ctx.settings = replace(ctx.settings, deployment_mode="managed")
    ctx.test_users.add("alice")
    ctx.test_users.add("bob")
    account = accs[1]
    repo.start_assignment(ctx.conn, "alice", account.id, ctx.now())
    newer = {"models": [*MODELS["models"], {"slug": "private-model", "visibility": "hide"},
                        {"slug": "unsupported-model", "supported_in_api": False}]}
    storage.store_catalog(ctx.conn, account.id, ctx.settings.gateway_client_version, True, newer, ctx.now())

    with client_for(ctx, lambda _: httpx.Response(500)) as client:
        client.get("/login")
        client.post("/login", data={"username": "admin", "password": "test gateway password",
                                   "csrf": client.cookies["codexctl_prelogin"]})
        page = client.get("/api-keys", params={"username": "alice", "account_id": 0})
        assert page.status_code == 200
        assert f'name="account_pk" value="{account.id}"' in page.text
        assert 'name="expiry"' not in page.text
        default_models = re.search(r'<select name="default_model".*?</select>', page.text, re.S)[0]
        assert 'value="model-a"' in default_models and 'value="model-b"' in default_models
        assert "private-model" not in default_models and "unsupported-model" not in default_models
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        saved = client.post("/api-keys/save", data={"csrf": csrf, "name": "alice key", "username": "alice",
                                                    "account_pk": account.id, "model_ids": ["model-a", "model-b"],
                                                    "default_model": "model-a"})
        assert saved.status_code == 200 and "alice" in saved.text
        public_id, secret = re.search(r'value="cctl_([0-9a-f]+)(\.[A-Za-z0-9_-]+)"', saved.text).groups()
        token = f"cctl_{public_id}{secret}"
        key = storage.get_key(ctx.conn, public_id)
        assert key["username"] == "alice" and key["expires_at"] is None
        assert "alice key" in client.get("/api-keys?username=alice").text
        assert "alice key" not in client.get("/api-keys?username=bob").text
        list_models = lambda: [row["id"] for row in client.get("/v1/models", headers=bearer(token)).json()["data"]]
        assert list_models() == ["model-a", "model-b"]
        storage.enable_model(ctx.conn, account.id, "model-a", False, "test", ctx.now())
        storage.store_catalog(ctx.conn, account.id, ctx.settings.gateway_client_version, True, newer, ctx.now())
        assert list_models() == ["model-b"]


def test_explicit_admin_password_is_accepted_in_gateway_mode(setup):
    ctx, _, _ = setup
    ctx.conn.execute("DELETE FROM admins WHERE username = 'admin'")
    webauth.create_admin(ctx.conn, "admin", webauth.DEFAULT_ADMIN_PASSWORD, now=ctx.now(), configured=False)
    with pytest.raises(webauth.AuthError):
        create_app(ctx)
    webauth.set_password(ctx.conn, "admin", webauth.DEFAULT_ADMIN_PASSWORD, now=ctx.now())
    with client_for(ctx, lambda _: httpx.Response(500)) as client:
        client.get("/login")
        response = client.post("/login", follow_redirects=False, data={
            "username": "admin", "password": webauth.DEFAULT_ADMIN_PASSWORD,
            "csrf": client.cookies["codexctl_prelogin"],
        })
        assert response.status_code == 303


def test_harness_discovery_and_responses_output_limit(setup):
    ctx, accs, tokens = setup
    metadata = {"models": [dict(MODELS["models"][0], display_name="A", context_window=272000,
                                max_context_window=872000, max_output_tokens=128000)]}
    storage.store_catalog(ctx.conn, accs[0].id, ctx.settings.gateway_client_version, True, metadata, ctx.now())
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=event("response.completed", response={"status": "completed"}))

    headers = bearer(tokens[0][1])
    with client_for(ctx, handler) as client:
        discovered = client.get("/v1/models", headers=headers)
        assert client.get("/v1/", headers=headers).json() == discovered.json()
        info = discovered.json()["data"][0]
        assert info["id"] == "model-a"
        assert info["display_name"] == "A"
        assert info["context_window"] == info["max_context_window"] == 872000
        assert info["default_context_window"] == 272000
        assert info["max_output_tokens"] == 128000
        native = client.get("/v1/codex/models", headers=headers).json()["models"][0]
        assert native["context_window"] == 272000 and native["max_context_window"] == 872000
        response = client.post("/v1/responses", headers=headers, json={
            "model": "model-a", "input": [{"role": "developer", "content": "Be concise"},
                                         {"role": "user", "content": "Hi"}],
            "stream": True, "max_output_tokens": 1024,
        })
        assert response.status_code == 200
        assert seen[0]["max_output_tokens"] == 1024
        assert client.post("/v1/responses", headers=headers, json={
            "model": "model-a", "input": "Hi", "max_output_tokens": -1,
        }).status_code == 400


def test_fast_probe_distinguishes_upstream_downgrade_from_missing_text_delta(setup):
    ctx, accs, tokens = setup
    actual_tier = "default"

    async def handler(request):
        assert json.loads(request.content)["service_tier"] == "priority"
        return httpx.Response(200, content=event("response.completed", response={
            "service_tier": actual_tier,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
        }))

    with client_for(ctx, handler) as client:
        client.get("/login")
        client.post("/login", data={"username": "admin", "password": "test gateway password",
                                    "csrf": client.cookies["codexctl_prelogin"]})
        path = f"/accounts/{accs[0].label}/models"
        csrf = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)[1]
        data = {"csrf": csrf, "model": "model-a", "action": "test_fast"}
        result = client.post(path + "/action", data=data)
        assert "上游实际使用标准档" in result.text
        assert storage.account_models(ctx.conn, accs[0].id)[0]["fast_verified_at"] is None
        actual_tier = "priority"
        result = client.post(path + "/action", data=data)
        assert "通过测试" in result.text
        assert storage.account_models(ctx.conn, accs[0].id)[0]["fast_verified_at"] is not None

        public_id, _ = tokens[0]
        storage.save_key(ctx.conn, actor="test", now=ctx.now(), name="test", username=accs[0].label,
                         account_pk=accs[0].id, model_ids=["model-a"], default_model="model-a",
                         allow_fast=True, public_id=public_id)
        result = client.post("/v1/responses", headers=bearer(tokens[0][1]), json={
            "model": "model-a", "input": "Hi", "service_tier": "fast",
        })
        assert result.status_code == 200


def test_user_key_management_search_edit_delete_and_keep_usage(setup):
    ctx, accs, tokens = setup
    public_id, token = tokens[0]
    other_id, other_token = tokens[1]
    key = storage.authorize(ctx.conn, f"Bearer {token}", ctx.now())
    model = "model-a"
    with client_for(ctx, lambda _: httpx.Response(500)) as client:
        client.get("/login")
        client.post("/login", data={"username": "admin", "password": "test gateway password",
                                   "csrf": client.cookies["codexctl_prelogin"]})
        directory = client.get("/api-keys").raise_for_status().text
        assert "username=acc-one" in directory and "username=acc-two" in directory
        assert public_id not in directory and other_id not in directory
        assert "username=acc-two" not in client.get("/api-keys?q=acc-one").text
        page = client.get("/api-keys?username=acc-one").raise_for_status().text
        assert public_id in page and other_id not in page and token not in page
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        edit = client.post("/api-keys/save", data={"csrf": csrf, "name": "Renamed key", "public_id": public_id,
                                                   "model_ids": ["model-a", "model-b"], "default_model": "model-b"},
                           follow_redirects=False)
        assert edit.status_code == 303 and "username=acc-one" in edit.headers["location"]
        assert public_id in client.get("/api-keys?username=acc-one&q=renamed").text
        assert public_id not in client.get("/api-keys?username=acc-one&q=absent").text
        models = client.get("/v1/models", headers=bearer(token)).json()["data"]
        assert {entry["id"] for entry in models} == {"model-a", "model-b"}
        assert client.post(f"/api-keys/{public_id}/delete").status_code == 403
        delete = client.post(f"/api-keys/{public_id}/delete", data={"csrf": csrf}, follow_redirects=False)
        assert delete.status_code == 303 and "username=acc-one" in delete.headers["location"]
        assert client.get(f"/api-keys/{public_id}/config").status_code == 404
        assert client.get("/v1/models", headers=bearer(token)).status_code == 401
        assert client.get("/v1/models", headers=bearer(other_token)).status_code == 200
        assert public_id not in client.get("/api-keys?username=acc-one").text
        assert storage.key_counts_by_user(ctx.conn) == {"acc-two": (1, 1)}

        # Requests authorized before deletion can still save their usage afterward.
        storage.Usage("completed-after-delete", key, model, ctx.now(), status="completed").save(ctx.conn)
        usage = client.get("/api-keys/usage").raise_for_status().text
        assert "已删除" in usage and "Renamed key" in usage
